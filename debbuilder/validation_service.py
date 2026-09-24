"""Durable asynchronous admission and observation for artifact validation."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import artifact_validation, dependency_preparation, storage
from .build_models import utc_now
from .build_store import BuildStore
from .recipe_schema import require_safe_name, validate_recipe_metadata
from .execution_projection import public_validation
from .validation_contracts import (
    PREPARED_DEPENDENCIES_CONTRACT_VERSION,
    VALIDATION_ATTEMPT_CONTRACT_VERSION,
    VALIDATION_RESULT_CONTRACT_VERSION,
    ValidationContractError,
    normalize_artifact_identity,
    normalize_prepared_runtime_dependencies,
    normalize_validation_attempt,
    normalize_validation_result,
)
from .validation_automation import (
    PUBLICATION_CANCELLED,
    PUBLICATION_COMPLETE,
    PUBLICATION_NOT_REQUESTED,
    PUBLICATION_PENDING,
    normalize_validation_automation as _normalize_automation,
)
from .validation_oci import OciOwnershipError, PodmanRuntime
from .validation_profiles import resolve_profile
from .validation_images import admitted_image


LOGGER = logging.getLogger(__name__)
ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATUSES = frozenset({"cancelled", "success", "failed"})
MAX_ATTEMPTS_PER_RUN = 512
AUTOMATION_FILE = "automation.json"
PREPARED_FILE = "prepared.json"
RESULT_FILE = "result.json"


class ValidationAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class ValidationCancellationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


def attempt_root(store: BuildStore, run_id: str, attempt_id: str) -> Path:
    require_safe_name(run_id, "build run id")
    require_safe_name(attempt_id, "validation attempt id")
    return store.run_dir(run_id) / "manifests" / "validation-attempts" / attempt_id


def _save_attempt(path: Path, attempt: dict) -> dict:
    normalized = normalize_validation_attempt(attempt)
    storage.save_json(path, normalized)
    path.chmod(0o600)
    return normalized


def load_attempt(store: BuildStore, run_id: str, attempt_id: str) -> dict:
    path = attempt_root(store, run_id, attempt_id) / "attempt.json"
    try:
        value = dependency_preparation._load_recovery_json(path, 256 * 1024)
        attempt = normalize_validation_attempt(value)
    except FileNotFoundError as exc:
        raise ValidationAdmissionError(
            "validation_attempt_not_found", "Validation attempt was not found", status=404,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, ValidationContractError) as exc:
        code = getattr(exc, "code", "validation_attempt_unreadable")
        status = 409 if code in {"future_contract_version", "unsupported_contract_version"} else 500
        raise ValidationAdmissionError(
            str(code), "Validation attempt cannot be read safely", status=status,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    if attempt.get("build_run_id") != run_id or attempt.get("id") != attempt_id:
        raise ValidationAdmissionError(
            "validation_attempt_identity_mismatch", "Validation attempt identity is inconsistent", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        )
    automation_path = attempt_root(store, run_id, attempt_id) / AUTOMATION_FILE
    try:
        _normalize_automation(dependency_preparation._load_recovery_json(automation_path, 16 * 1024))
    except FileNotFoundError as exc:
        raise ValidationAdmissionError(
            "validation_automation_missing", "Validation automation metadata is required", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationAdmissionError(
            "validation_automation_unreadable", "Validation automation metadata cannot be read safely", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    prepared_path = attempt_root(store, run_id, attempt_id) / PREPARED_FILE
    if prepared_path.exists() or prepared_path.is_symlink():
        prepared = load_prepared(store, run_id, attempt_id, attempt=attempt)
    else:
        prepared = None
    result_path = attempt_root(store, run_id, attempt_id) / RESULT_FILE
    if attempt.get("result") is not None:
        load_result(store, run_id, attempt_id, attempt=attempt, prepared=prepared)
    elif result_path.exists() or result_path.is_symlink():
        raise ValidationAdmissionError(
            "validation_result_state_mismatch", "Validation result exists before terminal attempt state", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        )
    if attempt["status"] == "success" and prepared is None:
        raise ValidationAdmissionError(
            "validation_prepared_missing", "Successful Validation lacks prepared dependency evidence", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        )
    return attempt


def load_prepared(
    store: BuildStore, run_id: str, attempt_id: str, *, attempt: dict | None = None,
) -> dict:
    path = attempt_root(store, run_id, attempt_id) / PREPARED_FILE
    try:
        prepared = normalize_prepared_runtime_dependencies(
            dependency_preparation._load_recovery_json(path, 1024 * 1024),
        )
    except FileNotFoundError as exc:
        raise ValidationAdmissionError(
            "validation_prepared_missing", "Prepared dependency evidence was not found", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, ValidationContractError) as exc:
        raise ValidationAdmissionError(
            str(getattr(exc, "code", "validation_prepared_unreadable")),
            "Prepared dependency evidence cannot be read safely", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    if attempt is not None:
        if (
            prepared["contract_version"] != PREPARED_DEPENDENCIES_CONTRACT_VERSION
            or prepared["profile_name"] != attempt["inputs"]["profile"]
            or prepared["artifacts"] != {
                "current": attempt["inputs"]["artifact"],
                "previous": attempt["inputs"]["previous_artifact"],
            }
            or prepared["image"] != attempt["selected_profile"]["image"]
        ):
            raise ValidationAdmissionError(
                "validation_prepared_identity_mismatch",
                "Prepared dependency evidence does not match Validation admission", status=409,
                details={"run_id": run_id, "attempt_id": attempt_id},
            )
        try:
            started = datetime.fromisoformat(str(attempt["started_at"]))
            prepared_started = datetime.fromisoformat(prepared["started_at"])
            prepared_finished = datetime.fromisoformat(prepared["finished_at"])
            finished = datetime.fromisoformat(str(attempt["finished_at"])) if attempt.get("finished_at") else None
        except (TypeError, ValueError) as exc:
            raise ValidationAdmissionError(
                "validation_prepared_time_mismatch", "Prepared dependency timestamps are inconsistent", status=409,
            ) from exc
        if prepared_started < started or (finished is not None and prepared_finished > finished):
            raise ValidationAdmissionError(
                "validation_prepared_time_mismatch", "Prepared dependency timestamps fall outside the attempt", status=409,
            )
    return prepared


def load_result(
    store: BuildStore,
    run_id: str,
    attempt_id: str,
    *,
    attempt: dict | None = None,
    prepared: dict | None = None,
) -> dict:
    path = attempt_root(store, run_id, attempt_id) / RESULT_FILE
    try:
        result = normalize_validation_result(
            dependency_preparation._load_recovery_json(path, 1024 * 1024),
        )
    except FileNotFoundError as exc:
        raise ValidationAdmissionError(
            "validation_result_missing", "Validation result evidence was not found", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, ValidationContractError) as exc:
        raise ValidationAdmissionError(
            str(getattr(exc, "code", "validation_result_unreadable")),
            "Validation result evidence cannot be read safely", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    if result["attempt_id"] != attempt_id or result["build_run_id"] != run_id:
        raise ValidationAdmissionError(
            "validation_result_identity_mismatch", "Validation result path identity is inconsistent", status=409,
        )
    if attempt is not None:
        try:
            result_started = datetime.fromisoformat(result["started_at"])
            result_finished = datetime.fromisoformat(result["finished_at"])
            attempt_started = datetime.fromisoformat(str(attempt.get("started_at") or ""))
            attempt_finished = datetime.fromisoformat(str(attempt.get("finished_at") or ""))
        except (TypeError, ValueError) as exc:
            raise ValidationAdmissionError(
                "validation_result_time_mismatch", "Validation result timestamps are inconsistent", status=409,
            ) from exc
        if (
            attempt.get("result") != {"status": result["status"], "reference": RESULT_FILE}
            or result["artifact"] != attempt["inputs"]["artifact"]
            or result["profile"] != attempt["selected_profile"]
            or result_started < attempt_started
            or result_finished > attempt_finished
        ):
            raise ValidationAdmissionError(
                "validation_result_identity_mismatch", "Validation result does not match its terminal attempt", status=409,
            )
    if prepared is not None:
        try:
            result_started = datetime.fromisoformat(result["started_at"])
            prepared_finished = datetime.fromisoformat(prepared["finished_at"])
        except (TypeError, ValueError) as exc:
            raise ValidationAdmissionError(
                "validation_result_time_mismatch", "Validation result timestamps are inconsistent", status=409,
            ) from exc
        if (
            result["artifact"] != prepared["artifacts"]["current"]
            or result["profile"] != {"name": prepared["profile_name"], "image": prepared["image"]}
            or result_started < prepared_finished
        ):
            raise ValidationAdmissionError(
                "validation_result_prepared_mismatch", "Validation result does not match prepared evidence", status=409,
            )
    return result


def list_attempts(store: BuildStore, run_id: str, *, strict: bool = True) -> list[dict]:
    require_safe_name(run_id, "build run id")
    root = store.run_dir(run_id) / "manifests" / "validation-attempts"
    if not root.exists():
        if root.is_symlink():
            if strict:
                raise OSError("validation attempt inventory is unsafe")
            return []
        return []
    try:
        if root.is_symlink() or not root.is_dir():
            raise OSError("validation attempt inventory is unsafe")
        entries = sorted(root.iterdir(), key=lambda path: path.name)
        if len(entries) > MAX_ATTEMPTS_PER_RUN:
            raise OSError("validation attempt inventory exceeds its bound")
        attempts = []
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                raise OSError("validation attempt inventory contains an unsafe entry")
            require_safe_name(entry.name, "validation attempt id")
            attempts.append(load_attempt(store, run_id, entry.name))
        return sorted(attempts, key=lambda row: (str(row.get("created_at") or ""), row["id"]))
    except (OSError, ValueError, ValidationAdmissionError):
        if strict:
            raise
        return []


def _publication_artifact_identity(run: dict) -> dict | None:
    """Return the immutable Run artifact identity used by Validation v1."""
    artifact = run.get("artifact") or {}
    inspection = artifact.get("inspection") or {}
    try:
        return normalize_artifact_identity({
            "package": inspection.get("package"),
            "version": inspection.get("version"),
            "architecture": inspection.get("architecture"),
            "size": artifact.get("size"),
            "sha256": artifact.get("sha256"),
        })
    except (TypeError, ValueError, ValidationContractError):
        return None


def publication_insertion_eligibility(
    run: dict,
    store: BuildStore,
    *,
    recovery_blocker: dict | None = None,
) -> dict:
    """Decide whether canonical Validation evidence authorizes a new insert.

    This consumes durable attempt manifests and their referenced lifecycle
    results.  Public Validation projections are deliberately not an
    authorization input.
    """
    reasons: list[str] = []
    artifact = run.get("artifact") or {}
    identity = _publication_artifact_identity(run)
    if run.get("status") != "success":
        reasons.append("build_not_successful")
    if not artifact.get("path") or artifact.get("pruning") is not None or identity is None:
        reasons.append("artifact_unavailable")

    try:
        attempts = list_attempts(store, str(run.get("id") or ""), strict=True)
    except (OSError, ValueError, ValidationAdmissionError, ValidationContractError):
        attempts = []
        reasons.append("validation_state_unverifiable")

    if recovery_blocker and "validation_state_unverifiable" not in reasons:
        reasons.append("validation_state_unverifiable")
    if any(attempt.get("status") in ACTIVE_STATUSES for attempt in attempts):
        reasons.append("validation_in_progress")

    successful: list[dict] = []
    referenced_state_unverifiable = False
    if identity is not None:
        for attempt in attempts:
            if attempt.get("status") != "success" or attempt.get("inputs", {}).get("artifact") != identity:
                continue
            try:
                prepared = load_prepared(store, str(run["id"]), attempt["id"], attempt=attempt)
                result = load_result(
                    store, str(run["id"]), attempt["id"], attempt=attempt, prepared=prepared,
                )
            except (OSError, ValueError, ValidationAdmissionError, ValidationContractError):
                referenced_state_unverifiable = True
                continue
            if result["status"] != "success":
                referenced_state_unverifiable = True
                continue
            successful.append(attempt)

    if referenced_state_unverifiable and "validation_state_unverifiable" not in reasons:
        reasons.append("validation_state_unverifiable")
    if not successful and "validation_state_unverifiable" not in reasons:
        reasons.append("current_validation_required")

    # Preserve stable ordering while avoiding duplicate reasons from combined
    # build/artifact and Validation failures.
    reasons = list(dict.fromkeys(reasons))
    eligible = not reasons
    return {
        "eligible": eligible,
        "reasons": reasons,
        "validation_id": successful[-1]["id"] if eligible else "",
    }


def _public_checks(result: dict) -> list[dict]:
    checks = []
    for row in result.get("checks") or []:
        if not isinstance(row, dict):
            continue
        checks.append({
            "name": str(row.get("name") or "")[:128],
            "status": str(row.get("status") or "")[:32],
            "error": "Validation check failed" if row.get("error") else "",
        })
        if len(checks) >= 100:
            break
    return checks


def _public_blocker(blocker: dict | None) -> dict | None:
    if not blocker:
        return None
    return {
        "code": str(blocker.get("code") or "validation_recovery_required")[:128],
        "message": "Operator attention required",
    }


def public_attempt(
    attempt: dict,
    *,
    run: dict | None = None,
    store: BuildStore | None = None,
    blocker: dict | None = None,
) -> dict:
    """Return the bounded observer contract; never expose command or temporary-path data."""
    attempt = normalize_validation_attempt(attempt)
    if store is None and run and run.get("workspace"):
        store = BuildStore(Path(str(run["workspace"])).parent)
    prepared = {}
    result = {}
    if store is not None:
        root = attempt_root(store, attempt["build_run_id"], attempt["id"])
        if (root / PREPARED_FILE).exists() or (root / PREPARED_FILE).is_symlink():
            prepared = load_prepared(store, attempt["build_run_id"], attempt["id"], attempt=attempt)
        if attempt.get("result") is not None:
            result = load_result(
                store, attempt["build_run_id"], attempt["id"], attempt=attempt,
                prepared=prepared or None,
            )
    checks = _public_checks(result)
    if attempt["status"] == "queued":
        phase = "queued"
    elif attempt["status"] == "cancelling":
        phase = "cancelling"
    elif attempt["status"] == "running":
        phase = "lifecycle" if prepared else "preparation"
    elif (attempt.get("result") or {}).get("reference"):
        phase = "lifecycle"
    else:
        phase = "lifecycle" if prepared else "preparation"
    # The durable manifest is the canonical state. Never combine its running
    # status with an earlier raw Run lifecycle error.
    result_error = result.get("error") if isinstance(result.get("error"), dict) else None
    error = ({
        "code": result_error["code"],
        "message": result_error["message"],
        "details": {
            "failed_checks": list(result_error.get("failed_checks") or []),
            "cleanup_code": result_error.get("cleanup_code") or "",
        },
    } if result_error else attempt.get("error"))
    response = {
        "contract_version": attempt["contract_version"],
        "id": attempt["id"],
        "attempt_id": attempt["id"],
        "build_run_id": attempt["build_run_id"],
        "status": attempt["status"],
        "phase": phase,
        "created_at": attempt.get("created_at"),
        "started_at": attempt.get("started_at"),
        "finished_at": attempt.get("finished_at"),
        "profile": {"name": attempt.get("inputs", {}).get("profile", "")},
        "result": attempt.get("result"),
        "error": error,
        "checks": checks,
        "diagnostics": list(prepared.get("diagnostics") or []),
        "dependency_preparation": {
            "status": "success" if prepared else ("running" if phase == "preparation" and attempt["status"] == "running" else "not_started"),
            "package_count": len(prepared.get("packages") or []),
            "profile": prepared.get("profile_name") or attempt.get("inputs", {}).get("profile", ""),
        },
        "cancellable": attempt["status"] in ACTIVE_STATUSES,
        "artifact_matches_run": bool(
            run
            and attempt.get("inputs", {}).get("artifact", {}).get("sha256")
            and attempt.get("inputs", {}).get("artifact", {}).get("sha256") == (run.get("artifact") or {}).get("sha256")
        ),
        "status_url": f"/api/executions/{attempt['build_run_id']}/validations/{attempt['id']}",
        "cancel_url": f"/api/executions/{attempt['build_run_id']}/validations/{attempt['id']}/cancel",
    }
    safe_blocker = _public_blocker(blocker)
    if safe_blocker and attempt["status"] in ACTIVE_STATUSES:
        response["recovery_blocker"] = safe_blocker
    return public_validation(response)


def project_run(run: dict, store: BuildStore, *, blocker: dict | None = None) -> dict:
    """Project canonical Validation attempts without consulting Run copies."""
    projected = dict(run)
    projected.pop("validations", None)
    if isinstance(projected.get("artifact"), dict):
        projected["artifact"] = dict(projected["artifact"])
        projected["artifact"].pop("validations", None)
    run_id = str(run["id"])
    try:
        attempts = list_attempts(store, run_id, strict=True)
        inventory_blocker = None
    except (OSError, ValueError, ValidationAdmissionError):
        attempts = []
        inventory_blocker = {
            "code": "validation_attempt_recovery_unverifiable",
            "message": "Operator attention required",
        }
    rows = []
    for attempt in attempts:
        canonical = public_attempt(attempt, run=run, store=store, blocker=blocker)
        if attempt.get("result") is not None:
            prepared = load_prepared(store, run_id, attempt["id"], attempt=attempt)
            detailed = load_result(
                store, run_id, attempt["id"], attempt=attempt, prepared=prepared,
            )
            canonical["commands"] = list(detailed["commands"])
            if detailed.get("error"):
                canonical["error"] = {
                    "code": detailed["error"]["code"],
                    "message": detailed["error"]["message"],
                    "details": {
                        "failed_checks": list(detailed["error"].get("failed_checks") or []),
                        "cleanup_code": detailed["error"].get("cleanup_code") or "",
                    },
                }
        rows.append(canonical)
    if inventory_blocker:
        rows.append(public_validation({
            "id": "recovery-blocked",
            "attempt_id": "recovery-blocked",
            "build_run_id": run_id,
            "status": "cancelling",
            "phase": "recovery",
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "profile": {"name": ""},
            "result": None,
            "error": None,
            "checks": [],
            "diagnostics": [],
            "dependency_preparation": {"status": "unknown", "package_count": 0, "profile": ""},
            "cancellable": False,
            "artifact_matches_run": False,
            "status_url": "",
            "cancel_url": "",
            "recovery_blocker": _public_blocker(inventory_blocker),
        }))
    projected["_validation_attempts"] = rows
    projected["publication_insertion_eligibility"] = publication_insertion_eligibility(
        run,
        store,
        recovery_blocker=blocker or inventory_blocker,
    )
    return projected


def _terminal_cancelled(attempt: dict, *, code: str, message: str) -> dict:
    attempt = dict(attempt)
    attempt.update({
        "status": "cancelled",
        "finished_at": utc_now(),
        "result": None,
        "error": {"code": code, "message": message},
    })
    return normalize_validation_attempt(attempt)


def _terminal_failed(attempt: dict, *, code: str, message: str) -> dict:
    attempt = dict(attempt)
    attempt.update({
        "status": "failed",
        "started_at": attempt.get("started_at") or attempt.get("created_at"),
        "finished_at": utc_now(),
        "result": None,
        "error": {"code": code, "message": message},
    })
    return normalize_validation_attempt(attempt)


def load_automation(store: BuildStore, run_id: str, attempt_id: str) -> dict:
    path = attempt_root(store, run_id, attempt_id) / AUTOMATION_FILE
    try:
        value = dependency_preparation._load_recovery_json(path, 16 * 1024)
    except FileNotFoundError as exc:
        raise ValidationAdmissionError(
            "validation_automation_missing", "Validation automation metadata is required", status=409,
            details={"run_id": run_id, "attempt_id": attempt_id},
        ) from exc
    return _normalize_automation(value)


def complete_publication_intent(store: BuildStore, run_id: str, attempt_id: str) -> dict:
    """Durably consume one automatic-publication request after exact success."""
    path = attempt_root(store, run_id, attempt_id) / AUTOMATION_FILE
    with storage.locked_path(path):
        automation = _normalize_automation(load_automation(store, run_id, attempt_id))
        if automation["publication_state"] == PUBLICATION_PENDING:
            automation["publication_state"] = PUBLICATION_COMPLETE
            storage.save_json(path, automation)
            path.chmod(0o600)
        return automation


def cancel_publication_intent(store: BuildStore, run_id: str, attempt_id: str) -> dict:
    """Fail-safe cancellation when policy/Recipe admission closes before publish."""
    path = attempt_root(store, run_id, attempt_id) / AUTOMATION_FILE
    with storage.locked_path(path):
        automation = _normalize_automation(load_automation(store, run_id, attempt_id))
        if automation["publication_state"] == PUBLICATION_PENDING:
            automation["publication_state"] = PUBLICATION_CANCELLED
            storage.save_json(path, automation)
            path.chmod(0o600)
        return automation


class ValidationManager:
    """One FIFO validation worker with durable admission and exact cancellation."""

    def __init__(
        self,
        store: BuildStore,
        *,
        execute,
        registry_root: str | Path,
        workspace_root: str | Path,
        allowed_previous_roots: tuple[str | Path, ...] = (),
        queue_capacity: int = 8,
        runner=dependency_preparation.run_command,
        resume_publication=None,
        on_terminal=None,
    ):
        self.store = store
        self.execute = execute
        self.registry_root = Path(registry_root)
        self.workspace_root = Path(workspace_root)
        self.allowed_previous_roots = tuple(Path(root) for root in allowed_previous_roots)
        self.queue_capacity = queue_capacity
        self.runner = runner
        self.resume_publication = resume_publication
        self.on_terminal = on_terminal
        self._condition = threading.Condition()
        self._queue: deque[tuple[str, str]] = deque()
        self._reservations = 0
        self._active: tuple[str, str, threading.Event] | None = None
        self._accepting = False
        self._stopping = False
        self._blocker: dict | None = None
        self.worker: threading.Thread | None = None

    @property
    def blocker(self) -> dict | None:
        with self._condition:
            return dict(self._blocker) if self._blocker else None

    def start(self, *, admission_blocker: dict | None = None) -> None:
        pending_publications = []
        with self._condition:
            if self.worker is not None:
                raise RuntimeError("Validation manager is already started")
            self._blocker = dict(admission_blocker) if admission_blocker else None
            self._accepting = admission_blocker is None
            self._stopping = False
            if admission_blocker is None:
                for run_id, attempt_id, _root in dependency_preparation._inventory_attempt_records(self.store):
                    try:
                        attempt = load_attempt(self.store, run_id, attempt_id)
                        if attempt["status"] == "queued":
                            self._queue.append((run_id, attempt_id))
                        elif (
                            attempt["status"] == "success"
                            and self.resume_publication is not None
                            and load_automation(self.store, run_id, attempt_id)["publication_state"] == PUBLICATION_PENDING
                        ):
                            pending_publications.append((run_id, attempt_id))
                    except ValidationAdmissionError:
                        continue
            self.worker = threading.Thread(target=self._work, name="validation-worker", daemon=False)
            self.worker.start()
        for run_id, attempt_id in pending_publications:
            try:
                self.resume_publication(run_id, attempt_id)
            except BaseException:
                LOGGER.exception("Could not resume automatic publication for %s/%s", run_id, attempt_id)

    def _automation(self, run_id: str, attempt_id: str) -> dict:
        return load_automation(self.store, run_id, attempt_id)

    def _notify_terminal_best_effort(self, run_id: str, attempt_id: str, attempt: dict | None = None) -> None:
        if self.on_terminal is None:
            return
        try:
            current = attempt or load_attempt(self.store, run_id, attempt_id)
            if current["status"] in TERMINAL_STATUSES:
                self.on_terminal(run_id, attempt_id)
        except BaseException:
            LOGGER.exception("Validation terminal continuation failed for %s/%s", run_id, attempt_id)

    def _block_for_recovery(self, run_id: str, attempt_id: str, reason: str) -> None:
        blocker = {
            "code": "validation_container_recovery_required",
            "message": "Validation admission is blocked until container cleanup is resolved",
            "details": {"run_id": run_id, "attempt_id": attempt_id},
        }
        with self._condition:
            if self._blocker is None:
                self._blocker = blocker
            self._accepting = False
            queued = list(self._queue)
            self._queue.clear()
            self._condition.notify_all()
        LOGGER.error("Validation manager blocked after unresolved cleanup: %s", reason)
        for queued_run_id, queued_attempt_id in queued:
            try:
                self._cancel_durable(
                    queued_run_id,
                    queued_attempt_id,
                    code="validation_recovery_cancelled",
                    message="Validation was cancelled because earlier container cleanup is unresolved",
                )
                self._notify_terminal_best_effort(queued_run_id, queued_attempt_id)
            except BaseException:
                LOGGER.exception("Could not cancel validation queued behind unresolved cleanup")
    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if not self._queue:
                    return
                run_id, attempt_id = self._queue.popleft()
                event = threading.Event()
                self._active = (run_id, attempt_id, event)
            try:
                self.execute(run_id, attempt_id, event, self._automation(run_id, attempt_id))
            except BaseException as exc:
                LOGGER.exception("Unexpected validation worker failure for %s/%s", run_id, attempt_id)
                self._fail_if_active(run_id, attempt_id, exc)
            finally:
                supervisor_blocker = dependency_preparation.SUPERVISOR.blocker
                if supervisor_blocker:
                    self._block_for_recovery(run_id, attempt_id, supervisor_blocker)
                with self._condition:
                    if self._active and self._active[:2] == (run_id, attempt_id):
                        self._active = None
                    self._condition.notify_all()
                self._notify_terminal_best_effort(run_id, attempt_id)

    def _fail_if_active(self, run_id: str, attempt_id: str, exc: BaseException) -> None:
        try:
            with self.store.locked_run(run_id):
                path = attempt_root(self.store, run_id, attempt_id) / "attempt.json"
                with storage.locked_path(path):
                    attempt = load_attempt(self.store, run_id, attempt_id)
                    if attempt["status"] not in ACTIVE_STATUSES or attempt["status"] == "cancelling":
                        return
                    code = str(getattr(exc, "code", "validation_worker_error"))
                    if not code or len(code) > 128:
                        code = "validation_worker_error"
                    _save_attempt(
                        path,
                        _terminal_failed(attempt, code=code, message="Validation execution failed unexpectedly"),
                    )
        except BaseException:
            LOGGER.exception("Could not terminalize validation worker failure for %s/%s", run_id, attempt_id)

    def _reserve_locked(self) -> None:
        self._ensure_accepting_locked()
        if len(self._queue) + self._reservations >= self.queue_capacity:
            raise ValidationAdmissionError(
                "validation_queue_full", "Validation queue is full", status=429,
                details={"queued": len(self._queue), "capacity": self.queue_capacity},
            )
        self._reservations += 1

    def _ensure_accepting_locked(self) -> None:
        if not self._accepting:
            blocker = self._blocker or {}
            raise ValidationAdmissionError(
                str(blocker.get("code") or "validation_manager_shutting_down"),
                str(blocker.get("message") or "Validation admission is closed"),
                status=503,
                details=dict(blocker.get("details") or {}),
            )

    def _reuse_active(
        self,
        run_id: str,
        *,
        automatic: bool,
        publish_after_success: bool,
        automation_context: dict | None = None,
        run: dict | None = None,
    ) -> dict | None:
        active = [row for row in list_attempts(self.store, run_id) if row["status"] in ACTIVE_STATUSES]
        if not active:
            return None
        selected = active[-1]
        attempt_path = attempt_root(self.store, run_id, selected["id"]) / "attempt.json"
        with storage.locked_path(attempt_path):
            selected = load_attempt(self.store, run_id, selected["id"])
            if selected["status"] not in ACTIVE_STATUSES:
                return None
            if automatic:
                automation_path = attempt_root(self.store, run_id, selected["id"]) / AUTOMATION_FILE
                with storage.locked_path(automation_path):
                    current = load_automation(self.store, run_id, selected["id"])
                    upgraded = {
                        "automatic": True,
                        "publish_after_success": current["publish_after_success"] or bool(publish_after_success),
                        "publication_state": (
                            current["publication_state"]
                            if current["publish_after_success"]
                            else PUBLICATION_PENDING if publish_after_success else PUBLICATION_NOT_REQUESTED
                        ),
                    }
                    if automation_context:
                        for key in ("attempt_key", "generation", "policy"):
                            if key in current and current[key] != automation_context[key]:
                                raise ValidationAdmissionError(
                                    "validation_automation_conflict",
                                    "Active Validation belongs to different automation coordination",
                                    status=409,
                                )
                        upgraded.update(automation_context)
                    elif "attempt_key" in current:
                        upgraded.update({key: current[key] for key in ("attempt_key", "generation", "policy")})
                    upgraded = _normalize_automation(upgraded)
                    storage.save_json(automation_path, upgraded)
                    automation_path.chmod(0o600)
        observed_run = run if run is not None else self.store.load(run_id)
        with self._condition:
            self._ensure_accepting_locked()
        return {**public_attempt(selected, run=observed_run, store=self.store, blocker=self.blocker), "duplicate": True}

    def admit(
        self,
        run_id: str,
        payload: dict | None = None,
        *,
        automatic: bool = False,
        publish_after_success: bool = False,
        automation_context: dict | None = None,
    ) -> dict:
        require_safe_name(run_id, "build run id")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict) or set(payload) - {"profile", "previous_artifact"}:
            raise ValidationAdmissionError(
                "invalid_validation_request", "Validation accepts only profile and previous_artifact", status=400,
                details={"run_id": run_id},
            )
        profile_name = str(payload.get("profile") or "bookworm")
        previous_artifact = str(payload.get("previous_artifact") or "")
        if len(profile_name) > 128 or len(previous_artifact) > 4096:
            raise ValidationAdmissionError("invalid_validation_request", "Validation request is too large", status=400)
        reserved = False
        admitted = None
        try:
            # A production validation owns the Run lease through slow OCI
            # work. Reuse its durable manifest before waiting on that lease so
            # automatic publication intent can be upgraded in time.
            with self._condition:
                self._ensure_accepting_locked()
            duplicate = self._reuse_active(
                run_id,
                automatic=automatic,
                publish_after_success=publish_after_success,
                automation_context=automation_context,
            )
            if duplicate is not None:
                return duplicate
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                if not run:
                    raise ValidationAdmissionError("build_run_not_found", "Build Run was not found", status=404)
                with self._condition:
                    self._ensure_accepting_locked()
                duplicate = self._reuse_active(
                    run_id,
                    automatic=automatic,
                    publish_after_success=publish_after_success,
                    automation_context=automation_context,
                    run=run,
                )
                if duplicate is not None:
                    return duplicate
                with self._condition:
                    self._reserve_locked()
                    reserved = True
                admitted = self._admit_locked(
                    run_id,
                    run,
                    profile_name=profile_name,
                    previous_artifact=previous_artifact,
                    automatic=automatic,
                    publish_after_success=publish_after_success,
                    automation_context=automation_context,
                )
            with self._condition:
                self._reservations -= 1
                reserved = False
                if not self._accepting:
                    self._condition.notify_all()
                else:
                    self._queue.append((run_id, admitted["id"]))
                    self._condition.notify_all()
                    return {**public_attempt(admitted, run=run, store=self.store), "duplicate": False}
            cancelled = self._cancel_durable(
                run_id, admitted["id"], code="validation_shutdown_cancelled",
                message="Validation was cancelled during server shutdown",
            )
            self._notify_terminal_best_effort(run_id, admitted["id"], cancelled)
            raise ValidationAdmissionError("validation_manager_shutting_down", "Validation admission closed during submission", status=503)
        except ValidationAdmissionError:
            raise
        except (artifact_validation.ValidationError, dependency_preparation.DependencyPreparationError, OciOwnershipError, OSError, ValueError) as exc:
            raise ValidationAdmissionError(
                str(getattr(exc, "code", "validation_admission_failed")), str(exc), status=422,
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc
        finally:
            if reserved:
                with self._condition:
                    self._reservations -= 1
                    self._condition.notify_all()

    def _admit_locked(
        self,
        run_id: str,
        run: dict,
        *,
        profile_name: str,
        previous_artifact: str,
        automatic: bool,
        publish_after_success: bool,
        automation_context: dict | None = None,
    ) -> dict:
        artifact = Path(str((run.get("artifact") or {}).get("path") or ""))
        if run.get("status") != "success" or not artifact.is_file() or (run.get("artifact") or {}).get("pruning") is not None:
            raise ValidationAdmissionError("artifact_not_available", "A successful Build Run with an artifact is required", status=409)
        workspace = Path(run["workspace"]).resolve(strict=True)
        try:
            artifact.resolve(strict=True).relative_to(workspace)
        except ValueError as exc:
            raise ValidationAdmissionError("artifact_outside_workspace", "Artifact must belong to the selected Build Run", status=409) from exc
        recipe = validate_recipe_metadata(json.loads((workspace / "recipe.json").read_text()))
        profile = resolve_profile(profile_name)
        image = admitted_image(profile_name)
        current = dependency_preparation.inspect_artifact(artifact, workspace=workspace, runner=self.runner)
        attempt_id = self.store.allocate_run_id()
        root = attempt_root(self.store, run_id, attempt_id)
        validation_root = workspace / "validation" / attempt_id
        root_created = False
        validation_created = False
        try:
            root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.mkdir(mode=0o700)
            root_created = True
            previous = None
            if previous_artifact:
                previous_path = artifact_validation.snapshot_previous_for_preparation_locked(
                    run_id,
                    previous_artifact,
                    store=self.store,
                    run=run,
                    attempt_id=attempt_id,
                    allowed_previous_roots=self.allowed_previous_roots,
                )
                validation_created = True
                previous = dependency_preparation.inspect_artifact(previous_path, workspace=workspace, runner=self.runner)
            created = utc_now()
            attempt = normalize_validation_attempt({
                "contract_version": VALIDATION_ATTEMPT_CONTRACT_VERSION,
                "id": attempt_id,
                "build_run_id": run_id,
                "inputs": {
                    "profile": profile_name,
                    "artifact": current.identity(),
                    "previous_artifact": previous.identity() if previous else None,
                },
                "selected_profile": {"name": profile_name, "image": image},
                "created_at": created,
                "started_at": None,
                "finished_at": None,
                "status": "queued",
                "result": None,
                "error": None,
            })
            automation = {
                "automatic": bool(automatic),
                "publish_after_success": bool(automatic and publish_after_success),
                "publication_state": (
                    PUBLICATION_PENDING
                    if automatic and publish_after_success
                    else PUBLICATION_NOT_REQUESTED
                ),
            }
            if automation_context:
                automation.update(automation_context)
            automation = _normalize_automation(automation)
            storage.save_json(root / AUTOMATION_FILE, automation)
            (root / AUTOMATION_FILE).chmod(0o600)
            _save_attempt(root / "attempt.json", attempt)
            return attempt
        except BaseException:
            if root_created:
                shutil.rmtree(root)
            if validation_root.exists() and not validation_root.is_symlink():
                shutil.rmtree(validation_root)
            raise

    def _cancel_durable(self, run_id: str, attempt_id: str, *, code: str, message: str) -> dict:
        path = attempt_root(self.store, run_id, attempt_id) / "attempt.json"
        # Validation preparation and lifecycle deliberately retain the Run
        # lease while they own OCI cleanup.  Cancellation must not wait for
        # that lease: the attempt manifest has its own atomic path lock and is
        # the canonical cancellation state.
        with storage.locked_path(path):
            attempt = load_attempt(self.store, run_id, attempt_id)
            if attempt["status"] == "cancelled":
                return attempt
            if attempt["status"] not in ACTIVE_STATUSES:
                return attempt
            if attempt["status"] == "queued":
                attempt = _terminal_cancelled(attempt, code=code, message=message)
            else:
                attempt = dict(attempt)
                attempt["status"] = "cancelling"
                attempt = normalize_validation_attempt(attempt)
            return _save_attempt(path, attempt)

    def cancel(self, run_id: str, attempt_id: str) -> dict:
        require_safe_name(run_id, "build run id")
        require_safe_name(attempt_id, "validation attempt id")
        event = None
        with self._condition:
            queued = (run_id, attempt_id) in self._queue
            active = self._active is not None and self._active[:2] == (run_id, attempt_id)
            if queued:
                self._queue.remove((run_id, attempt_id))
            if active:
                event = self._active[2]
            self._condition.notify_all()
        # Signal first so a command currently running under the Run lease is
        # interrupted while the durable attempt transition is serialized.
        if event is not None:
            event.set()
        attempt = self._cancel_durable(
            run_id,
            attempt_id,
            code="validation_cancelled",
            message="Validation was cancelled by the user",
        )
        self._notify_terminal_best_effort(run_id, attempt_id, attempt)
        if attempt["status"] in {"success", "failed"}:
            return {"accepted": False, "validation": public_attempt(attempt, run=self.store.load(run_id), store=self.store)}
        if attempt["status"] == "queued" or (attempt["status"] == "cancelling" and not active):
            raise ValidationCancellationError(
                "validation_recovery_required", "Validation cancellation ownership is unavailable", status=409,
                details={"run_id": run_id, "attempt_id": attempt_id},
            )
        return {"accepted": attempt["status"] in {"cancelling", "cancelled"}, "validation": public_attempt(attempt, run=self.store.load(run_id), store=self.store)}

    def begin_shutdown(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._accepting = False
            self._stopping = True
            queued = list(self._queue)
            self._queue.clear()
            active = self._active
            if self._active is not None:
                _run_id, _attempt_id, event = self._active
                event.set()
            self._condition.notify_all()
        for run_id, attempt_id in queued:
            try:
                cancelled = self._cancel_durable(
                    run_id,
                    attempt_id,
                    code="validation_shutdown_cancelled",
                    message="Validation was cancelled during server shutdown",
                )
                self._notify_terminal_best_effort(run_id, attempt_id, cancelled)
            except BaseException:
                LOGGER.exception("Could not cancel queued validation during shutdown")
        if active is not None:
            run_id, attempt_id, _event = active
            try:
                self._cancel_durable(
                    run_id,
                    attempt_id,
                    code="validation_shutdown_cancelled",
                    message="Validation was cancelled during server shutdown",
                )
            except BaseException:
                LOGGER.exception("Could not persist active validation cancellation during shutdown")

    def shutdown(self, timeout: float | None = None) -> bool:
        self.begin_shutdown()
        worker = self.worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()
