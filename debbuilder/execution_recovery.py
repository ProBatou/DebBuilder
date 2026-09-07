"""Fail-closed startup reconciliation for interrupted Build Runs."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime

from .build_models import RUN_STATUSES, utc_now
from .build_store import BuildStore
from .command_containment import loaded_command_units, recover_systemd_containment
from .command_identity import (
    CommandIdentityError,
    IdentityRecorder,
    VerificationStatus,
    clear_identity,
    current_boot_id,
    read_persisted_identity,
    terminate_verified_process_group,
    update_identity,
    validated_identity,
    verify_identity,
)
from .recipe_schema import require_safe_name
from .workspace_cleanup import WorkspaceBusyError, directory_fd, read_run


NON_TERMINAL_STATUSES = frozenset({"pending", "queued", "running", "cancelling"})
RECOVERY_ERROR_CODE = "execution_interrupted"
BLOCKER_CODE = "execution_recovery_unresolved"


@dataclass
class StartupRecoveryResult:
    """Complete startup outcome used to establish execution admission state."""

    processed_run_ids: list[str] = field(default_factory=list)
    recovered_run_ids: list[str] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    stray_units: list[str] = field(default_factory=list)
    unit_scan_error: str = ""

    @property
    def admission_blocker(self) -> dict | None:
        if not self.blockers and not self.stray_units:
            return None
        run_ids = sorted({row["run_id"] for row in self.blockers if row.get("run_id")})
        return {
            "code": BLOCKER_CODE,
            "message": "Build/Test admission is blocked until interrupted workload recovery is resolved",
            "details": {
                "unresolved_run_ids": run_ids,
                "unresolved_count": len(run_ids),
                "stray_unit_count": len(self.stray_units),
            },
        }

    def as_dict(self) -> dict:
        return {
            "processed_run_ids": list(self.processed_run_ids),
            "recovered_run_ids": list(self.recovered_run_ids),
            "blockers": [dict(row) for row in self.blockers],
            "stray_units": list(self.stray_units),
            "unit_scan_error": self.unit_scan_error,
            "admission_blocked": self.admission_blocker is not None,
        }


def _persisted_run_ids(store: BuildStore) -> tuple[list[str], list[tuple[str, str]]]:
    """Enumerate Runs and retain every unsafe/partial root entry as evidence."""
    try:
        with directory_fd(store.root) as root_fd:
            candidates = []
            errors = []
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        errors.append(("", f"Run inventory entry {entry.name!r} is unverifiable: {exc}"))
                        continue
                    if not stat.S_ISDIR(info.st_mode):
                        errors.append(("", f"unexpected non-directory entry in the Runs root: {entry.name!r}"))
                        continue
                    try:
                        require_safe_name(entry.name, "build run id")
                    except (TypeError, ValueError) as exc:
                        errors.append(("", f"unsafe Run directory {entry.name!r}: {exc}"))
                        continue
                    try:
                        child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    except OSError as exc:
                        errors.append((entry.name, f"Run directory is unverifiable: {exc}"))
                        continue
                    try:
                        try:
                            os.stat("run.json", dir_fd=child_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            errors.append((entry.name, "Run directory is incomplete: run.json is absent"))
                            continue
                        except OSError as exc:
                            errors.append((entry.name, f"Run metadata is unverifiable: {exc}"))
                            continue
                        # Unsafe types/hardlinks remain candidates so the locked,
                        # bounded reader can turn them into an admission blocker.
                        candidates.append(entry.name)
                    finally:
                        os.close(child_fd)
            return sorted(candidates), errors
    except FileNotFoundError:
        return [], []


def _elapsed(started_at: str | None, finished_at: str) -> float:
    try:
        return round(max(0.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(str(started_at))).total_seconds()), 6)
    except (TypeError, ValueError):
        return 0.0


def _command_history_exists(workspace_fd: int) -> bool:
    logs_fd = commands_fd = -1
    try:
        try:
            logs_fd = os.open("logs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=workspace_fd)
            commands_fd = os.open("commands", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=logs_fd)
        except FileNotFoundError:
            return False
        with os.scandir(commands_fd) as entries:
            for _entry in entries:
                # Any directory entry is execution evidence.  Its type cannot
                # make a pending/queued Run safer to terminalize.
                return True
        return False
    except OSError:
        # Inaccessible evidence is not evidence of absence.
        return True
    finally:
        if commands_fd >= 0:
            os.close(commands_fd)
        if logs_fd >= 0:
            os.close(logs_fd)


def _no_command_could_have_started(run: dict, workspace_fd: int) -> bool:
    if run.get("status") not in {"pending", "queued"}:
        return False
    if run.get("started_at") or run.get("finished_at") or run.get("duration") is not None:
        return False
    if run.get("events") or run.get("error") or _command_history_exists(workspace_fd):
        return False
    return all(
        step.get("status") == "pending"
        and step.get("started_at") is None
        and step.get("finished_at") is None
        and step.get("duration") is None
        for step in run.get("steps", [])
    )


def _recovery_record(*, status: str, reason: str, backend: str, previous: dict | None = None) -> dict:
    previous = previous if isinstance(previous, dict) else {}
    record = {
        "status": status,
        "code": BLOCKER_CODE if status == "blocked" else RECOVERY_ERROR_CODE,
        "backend": backend,
        "reason": reason,
        "recorded_at": previous.get("recorded_at") or utc_now(),
    }
    if status == "resolved":
        record["resolved_at"] = previous.get("resolved_at") or utc_now()
    return record


def _record_blocker(store: BuildStore, run: dict, *, backend: str, reason: str) -> dict:
    previous = run.get("recovery") if isinstance(run.get("recovery"), dict) else {}
    if (
        previous.get("status") == "blocked"
        and previous.get("code") == BLOCKER_CODE
        and previous.get("backend") == backend
    ):
        recovery = dict(previous)
    else:
        recovery = _recovery_record(
            status="blocked", reason=reason, backend=backend, previous=previous,
        )
    if run.get("recovery") != recovery:
        run["recovery"] = recovery
        store.save(run)
    return {
        "run_id": str(run["id"]),
        "code": BLOCKER_CODE,
        "backend": backend,
        "reason": str(recovery.get("reason") or reason),
    }


def _terminalize_interrupted(store: BuildStore, run: dict, *, backend: str, reason: str) -> None:
    finished_at = utc_now()
    active_step = next((step for step in run["steps"] if step.get("status") == "running"), None)
    stage = str(active_step.get("name")) if active_step else "recovery"
    error = {
        "stage": stage,
        "code": RECOVERY_ERROR_CODE,
        "message": "Execution was interrupted before startup recovery completed",
        "details": {"recovery_backend": backend, "reason": reason},
    }
    if active_step is not None:
        active_step.update({
            "status": "failed",
            "finished_at": finished_at,
            "duration": _elapsed(active_step.get("started_at"), finished_at),
            "summary": error["message"],
            "error": error,
        })
    run.update({
        "status": "failed",
        "finished_at": finished_at,
        "duration": _elapsed(run.get("started_at"), finished_at),
        "error": error,
        "recovery": _recovery_record(
            status="resolved", reason=reason, backend=backend, previous=run.get("recovery"),
        ),
    })
    run.setdefault("events", []).append({
        "at": finished_at,
        "level": "error",
        "message": "Interrupted execution reconciled during startup; Run marked failed.",
    })
    store.save(run)
    try:
        store.append_log_line(str(run["id"]), "Interrupted execution reconciled during startup; Run marked failed.", level="error")
    except OSError:
        pass


def _terminalization_ambiguity(run: dict) -> str:
    active_steps = [step for step in run["steps"] if step.get("status") == "running"]
    if len(active_steps) > 1:
        return "persisted Run has multiple running steps; historical lifecycle mutation is ambiguous"
    return ""


def _clear_resolved_identity(workspace_fd: int, run_id: str) -> None:
    try:
        current = read_persisted_identity(workspace_fd)
        if current is not None:
            current = validated_identity(current, expected_run_id=run_id)
            clear_identity(workspace_fd, current)
    except (CommandIdentityError, OSError, TypeError, ValueError):
        pass


def _record_terminal_resolution(store: BuildStore, run: dict, *, backend: str, reason: str) -> None:
    """Resolve a blocker without rewriting an already-terminal Run lifecycle."""
    previous = run.get("recovery") if isinstance(run.get("recovery"), dict) else {}
    if previous.get("status") != "blocked":
        return
    run["recovery"] = _recovery_record(
        status="resolved", reason=reason, backend=backend, previous=previous,
    )
    store.save(run)


def _reconcile_current_boot_identity(
    identity: dict, *, run_id: str, workspace_fd: int,
) -> tuple[bool, str, str]:
    """Return (absence_proved, backend, reason) for one durable identity."""
    backend = str(identity.get("backend") or "process_group")
    if backend == "systemd_cgroup":
        recorder = IdentityRecorder(
            run_id,
            record=lambda _value: None,
            clear=lambda value: clear_identity(workspace_fd, value),
            update=lambda expected, updated: update_identity(workspace_fd, expected, updated),
        )
        recovered = recover_systemd_containment(
            identity, expected_run_id=run_id, recorder=recorder,
        )
        if recovered.verification.status is VerificationStatus.NOT_RUNNING and recovered.gone:
            return True, backend, recovered.verification.reason
        return False, backend, f"{recovered.verification.status.value}: {recovered.verification.reason}"

    verification = verify_identity(identity, expected_run_id=run_id)
    termination = None
    if verification.status is VerificationStatus.MATCH:
        termination = terminate_verified_process_group(identity, expected_run_id=run_id)
    if termination and termination.error:
        reason = f"fallback process group was addressed but remains unverifiable: {termination.error}"
    elif termination and termination.group_gone:
        reason = "fallback process group ended, but escaped descendants cannot be excluded on the current boot"
    else:
        reason = f"{verification.status.value}: {verification.reason}; fallback descendants cannot be excluded on the current boot"
    return False, "process_group", reason


def recover_startup(store: BuildStore) -> StartupRecoveryResult:
    """Reconcile every persisted interrupted Run before execution admission."""
    result = StartupRecoveryResult()
    unit_bindings: dict[str, str] = {}
    blocked_ids: set[str] = set()

    try:
        boot_id = current_boot_id()
    except (CommandIdentityError, OSError) as exc:
        boot_id = ""
        result.blockers.append({
            "run_id": "", "code": BLOCKER_CODE, "backend": "unknown",
            "reason": f"current boot identity is unavailable: {exc}",
        })

    try:
        run_ids, inventory_errors = _persisted_run_ids(store)
        for run_id, reason in inventory_errors:
            result.blockers.append({
                "run_id": run_id, "code": BLOCKER_CODE, "backend": "unknown",
                "reason": reason,
            })
            if run_id:
                blocked_ids.add(run_id)
    except (OSError, TypeError, ValueError) as exc:
        run_ids = []
        result.blockers.append({
            "run_id": "", "code": BLOCKER_CODE, "backend": "unknown",
            "reason": f"persisted Run inventory is unverifiable: {exc}",
        })

    for run_id in run_ids:
        try:
            with store.locked_run(run_id, blocking=False) as workspace_fd:
                run = read_run(workspace_fd, store.root, run_id)
                status = str(run.get("status") or "")
                if status not in RUN_STATUSES:
                    raise ValueError(f"invalid run status: {status}")

                try:
                    identity = read_persisted_identity(workspace_fd)
                    identity = validated_identity(identity, expected_run_id=run_id) if identity is not None else None
                except (CommandIdentityError, OSError, TypeError, ValueError) as exc:
                    blocker = _record_blocker(store, run, backend="unknown", reason=f"active command metadata is unverifiable: {exc}")
                    result.blockers.append(blocker)
                    blocked_ids.add(run_id)
                    result.processed_run_ids.append(run_id)
                    continue

                if identity and identity.get("backend") == "systemd_cgroup" and identity.get("boot_id") == boot_id:
                    unit_bindings[str(identity["unit_name"])] = run_id

                if status not in NON_TERMINAL_STATUSES:
                    if (run.get("recovery") or {}).get("status") == "resolved":
                        _clear_resolved_identity(workspace_fd, run_id)
                    elif identity is None and (run.get("recovery") or {}).get("status") == "blocked":
                        recovery = run["recovery"]
                        result.processed_run_ids.append(run_id)
                        result.blockers.append({
                            "run_id": run_id,
                            "code": BLOCKER_CODE,
                            "backend": str(recovery.get("backend") or "unknown"),
                            "reason": str(recovery.get("reason") or "terminal Run recovery remains unresolved"),
                        })
                        blocked_ids.add(run_id)
                    elif identity is not None:
                        result.processed_run_ids.append(run_id)
                        backend = str(identity.get("backend") or "process_group")
                        if identity["boot_id"] != boot_id and boot_id:
                            reason = "durable command identity belongs to a previous boot"
                            _clear_resolved_identity(workspace_fd, run_id)
                            _record_terminal_resolution(store, run, backend=backend, reason=reason)
                        else:
                            resolved, backend, reason = _reconcile_current_boot_identity(
                                identity, run_id=run_id, workspace_fd=workspace_fd,
                            )
                            if resolved:
                                _clear_resolved_identity(workspace_fd, run_id)
                                _record_terminal_resolution(store, run, backend=backend, reason=reason)
                            else:
                                blocker = _record_blocker(store, run, backend=backend, reason=reason)
                                result.blockers.append(blocker)
                                blocked_ids.add(run_id)
                    continue

                result.processed_run_ids.append(run_id)
                if identity is None:
                    if _no_command_could_have_started(run, workspace_fd):
                        _terminalize_interrupted(
                            store, run, backend="none",
                            reason="persisted lifecycle proves that command execution never began",
                        )
                        result.recovered_run_ids.append(run_id)
                    else:
                        blocker = _record_blocker(
                            store, run, backend="none",
                            reason="no durable command identity can prove historical workload absence",
                        )
                        result.blockers.append(blocker)
                        blocked_ids.add(run_id)
                    continue

                backend = str(identity.get("backend") or "process_group")
                if identity["boot_id"] != boot_id and boot_id:
                    ambiguity = _terminalization_ambiguity(run)
                    if ambiguity:
                        blocker = _record_blocker(store, run, backend=backend, reason=ambiguity)
                        result.blockers.append(blocker)
                        blocked_ids.add(run_id)
                        continue
                    _terminalize_interrupted(
                        store, run, backend=backend,
                        reason="durable command identity belongs to a previous boot",
                    )
                    _clear_resolved_identity(workspace_fd, run_id)
                    result.recovered_run_ids.append(run_id)
                    continue

                resolved, backend, reason = _reconcile_current_boot_identity(
                    identity, run_id=run_id, workspace_fd=workspace_fd,
                )
                if resolved:
                    ambiguity = _terminalization_ambiguity(run)
                    if ambiguity:
                        blocker = _record_blocker(store, run, backend=backend, reason=ambiguity)
                        result.blockers.append(blocker)
                        blocked_ids.add(run_id)
                    else:
                        _terminalize_interrupted(store, run, backend=backend, reason=reason)
                        _clear_resolved_identity(workspace_fd, run_id)
                        result.recovered_run_ids.append(run_id)
                else:
                    blocker = _record_blocker(store, run, backend=backend, reason=reason)
                    result.blockers.append(blocker)
                    blocked_ids.add(run_id)
        except WorkspaceBusyError as exc:
            result.processed_run_ids.append(run_id)
            result.blockers.append({
                "run_id": run_id, "code": BLOCKER_CODE, "backend": "unknown",
                "reason": f"Run recovery lock is unavailable: {exc}",
            })
            blocked_ids.add(run_id)
        except (CommandIdentityError, OSError, TypeError, ValueError) as exc:
            result.processed_run_ids.append(run_id)
            result.blockers.append({
                "run_id": run_id, "code": BLOCKER_CODE, "backend": "unknown",
                "reason": f"persisted Run is unverifiable: {exc}",
            })
            blocked_ids.add(run_id)

    try:
        loaded_units = loaded_command_units()
    except Exception as exc:
        result.unit_scan_error = str(exc)
        loaded_units = set()
        result.blockers.append({
            "run_id": "", "code": BLOCKER_CODE, "backend": "systemd_cgroup",
            "reason": f"DebBuilder command cgroup inventory is unverifiable: {exc}",
        })
    for unit_name in sorted(loaded_units):
        bound_run_id = unit_bindings.get(unit_name)
        if bound_run_id in blocked_ids:
            continue
        result.stray_units.append(unit_name)
        if bound_run_id:
            result.blockers.append({
                "run_id": bound_run_id,
                "code": BLOCKER_CODE,
                "backend": "systemd_cgroup",
                "reason": "a current-boot bound transient unit remains outside non-terminal recovery",
            })
            blocked_ids.add(bound_run_id)

    return result
