"""Durable coordination across DebBuilder's canonical package lifecycle owners."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import artifact_publication, automation_service, recipe_store, validation_service
from .automation_ledger import AutomationLedger, AutomationLedgerError
from .build_store import BuildStore, canonical_recipe_sha256
from .recipe_schema import runtime_recipe_for_storage, validate_recipe_metadata


LOGGER = logging.getLogger(__name__)
TERMINAL_RUN_STATUSES = frozenset({"prepared", "success", "failed", "cancelled"})
VALIDATION_TRANSIENT_CODES = frozenset({
    "dependency_preparation_failed", "dependency_download_failed",
    "dependency_repository_unavailable", "apt_update_failed",
    "validation_preparation_failed", "validation_preparation_interrupted",
    "validation_image_unavailable", "validation_worker_error",
})
PUBLICATION_TRANSIENT_CODES = frozenset({
    "repository_mutation_busy", "publication_execution_failed",
    "publication_reconciliation_failed", "publication_interrupted",
    "reprepro_include_failed",
})
PUBLICATION_BLOCKED_CODES = frozenset({
    "downgrade_refused", "publication_identity_conflict", "publication_proof_failed",
    "publication_proof_invalid", "artifact_not_ready", "artifact_mutated",
    "artifact_identity_mismatch", "artifact_not_available",
    "publication_confirmation_required", "component_not_configured",
    "repository_configuration_unsupported",
})
POLICY_RANK = {"manual": 0, "detect": 1, "test": 2, "build": 3, "build_validate": 4, "full": 5}
MAX_VALIDATION_RETRIES = 2
MAX_PUBLICATION_RETRIES = 3
MIN_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 15 * 60
RUN_ADMISSION_TRANSIENT_CODES = frozenset({
    "execution_queue_full", "execution_manager_unavailable",
    "execution_recovery_unresolved",
})


class AutomationOrchestrator:
    """Advance claims by asking the existing managers to own each stage."""

    def __init__(
        self,
        recipe_directory: str | Path,
        ledger: AutomationLedger,
        store: BuildStore,
        *,
        execution_manager: Callable[[], object | None],
        enqueue_run: Callable[..., dict],
        validation_manager: Callable[[], object | None],
        publish: Callable[[str, dict], dict],
        notify_completion: Callable[[dict], object],
        admission_open: Callable[[], bool],
        mutation_lease: Callable[[], object] = nullcontext,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.recipe_directory = Path(recipe_directory)
        self.ledger = ledger
        self.store = store
        self.execution_manager = execution_manager
        self.enqueue_run = enqueue_run
        self.validation_manager = validation_manager
        self.publish = publish
        self.notify_completion = notify_completion
        self.admission_open = admission_open
        self.mutation_lease = mutation_lease
        self.wall_clock = wall_clock
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple[str, int], threading.Lock] = {}

    def _lock(self, key: str, generation: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((key, generation), threading.Lock())

    @staticmethod
    def _row(document: dict, key: str, generation: int) -> tuple[dict, dict] | tuple[None, None]:
        entry = document.get("attempts", {}).get(key)
        if not entry or not 0 <= generation < len(entry.get("generations") or []):
            return None, None
        return entry, entry["generations"][generation]

    def _recipe(self, recipe_id: str) -> dict | None:
        try:
            value = recipe_store.load_recipe(self.recipe_directory / f"{recipe_id}.json")
            recipe = validate_recipe_metadata(value)
        except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError):
            return None
        return recipe if recipe.get("name") == recipe_id else None

    def _initial_authorized(self, entry: dict, row: dict, recipe: dict | None) -> bool:
        automation = (recipe or {}).get("automation") or {}
        return bool(
            recipe
            and recipe.get("active")
            and automation.get("enabled")
            and automation.get("policy") == row["desired_policy"]
            and canonical_recipe_sha256(recipe) == entry["recipe_sha256"]
        )

    @staticmethod
    def _downstream_authorized(recipe: dict | None, required_policy: str) -> bool:
        automation = (recipe or {}).get("automation") or {}
        current = str(automation.get("policy") or "manual")
        return bool(
            recipe and recipe.get("active") and automation.get("enabled")
            and POLICY_RANK.get(current, 0) >= POLICY_RANK[required_policy]
        )

    def _is_recipe_turn(self, document: dict, key: str, generation: int, recipe_id: str) -> bool:
        active = []
        for candidate_key, entry in document["attempts"].items():
            if entry["recipe_id"] != recipe_id:
                continue
            for row in entry["generations"]:
                if row["state"] not in {"terminal", "blocked"}:
                    active.append((row["created_at"], candidate_key, row["generation"]))
        return not active or min(active) == (self._row(document, key, generation)[1]["created_at"], key, generation)

    def _retry_due(self, row: dict) -> bool:
        if row["state"] != "retry_delayed":
            return True
        try:
            return datetime.fromisoformat(row["not_before"]).timestamp() <= self.wall_clock()
        except (TypeError, ValueError):
            return False

    def _delay(self, count: int) -> str:
        seconds = min(MAX_RETRY_SECONDS, MIN_RETRY_SECONDS * (2 ** max(0, count - 1)))
        return (datetime.fromtimestamp(self.wall_clock(), timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def _schedule_retry(self, entry: dict, row: dict, code: str, diagnostic: str) -> None:
        field = {
            "run_admission": "run_retry_count",
            "validation_admission": "validation_retry_count",
            "validation": "validation_retry_count",
            "publication": "publication_retry_count",
        }.get(row["stage"], "retry_count")
        count = row[field] + 1
        self.ledger.finish(
            entry["key"], row["generation"], "transient_retryable",
            retry_class="transient", retry_count=count, not_before=self._delay(count),
            last_error_code=code, diagnostic=diagnostic,
        )

    def _finish(self, entry: dict, row: dict, classification: str, code: str, diagnostic: str) -> None:
        finished = self.ledger.finish(
            entry["key"], row["generation"], classification,
            retry_class="operator" if classification == "blocked_manual" else "none",
            retry_count=row["retry_count"], last_error_code=code or None, diagnostic=diagnostic,
        )
        if finished["completion_notified"]:
            return
        if classification not in {"cancelled", "disabled"}:
            try:
                self.notify_completion({
                    "run_id": row.get("run_id") or "",
                    "status": "success" if classification == "success" else "failed",
                    "automation_lifecycle": {
                        "policy": row["desired_policy"], "stage": row["stage"],
                        "classification": classification, "code": code or None,
                    },
                })
            except Exception:
                LOGGER.exception("Automation completion notification failed for %s", entry["key"])
        self.ledger.mark_completion_notified(entry["key"], row["generation"])

    def _reconcile_validation_link(self, entry: dict, row: dict) -> dict:
        for attempt in reversed(validation_service.list_attempts(self.store, row["run_id"], strict=False)):
            try:
                metadata = validation_service.load_automation(self.store, row["run_id"], attempt["id"])
            except (OSError, ValueError):
                continue
            if (
                metadata.get("attempt_key") == entry["key"]
                and metadata.get("generation") == row["generation"]
                and metadata.get("policy") == row["desired_policy"]
            ):
                if attempt["id"] == row["validation_attempt_id"]:
                    return row
                return self.ledger.advance_lifecycle(
                    entry["key"], row["generation"], "validation",
                    validation_attempt_id=attempt["id"],
                )
        return row

    def _admit_validation(self, entry: dict, row: dict) -> None:
        if not self.admission_open():
            return
        reconciled = self._reconcile_validation_link(entry, row)
        if reconciled is not row:
            self._handle_validation(entry, reconciled)
            return
        required = "full" if row["desired_policy"] == "full" else "build_validate"
        if not self._downstream_authorized(self._recipe(entry["recipe_id"]), required):
            if row["validation_attempt_id"]:
                try:
                    validation_service.cancel_publication_intent(
                        self.store, row["run_id"], row["validation_attempt_id"],
                    )
                except (OSError, ValueError):
                    pass
            self._finish(entry, row, "disabled", "automation_downstream_disabled", "downstream_admission_disabled")
            return
        manager = self.validation_manager()
        if manager is None:
            try:
                with self.mutation_lease():
                    row = self.ledger.advance_lifecycle(entry["key"], row["generation"], "validation_admission")
            except Exception as exc:
                if str(getattr(exc, "code", "")) == "application_shutting_down" or not self.admission_open():
                    return
                raise
            self._schedule_retry(entry, row, "validation_manager_unavailable", "validation_admission_delayed")
            return
        try:
            with self.mutation_lease():
                row = self.ledger.advance_lifecycle(entry["key"], row["generation"], "validation_admission")
                admitted = manager.admit(
                    row["run_id"], {}, automatic=True,
                    publish_after_success=row["desired_policy"] == "full",
                    automation_context={
                        "attempt_key": entry["key"], "generation": row["generation"],
                        "policy": row["desired_policy"],
                    },
                )
        except validation_service.ValidationAdmissionError as exc:
            if exc.code in {"validation_queue_full", "validation_manager_shutting_down"}:
                if self.admission_open():
                    self._schedule_retry(entry, row, exc.code, "validation_admission_delayed")
                return
            self._finish(entry, row, "terminal_failure", exc.code, "validation_admission_failed")
            return
        except Exception as exc:
            code = str(getattr(exc, "code", "validation_admission_failed"))
            if code == "application_shutting_down" or not self.admission_open():
                return
            self._finish(entry, row, "terminal_failure", code, "validation_admission_failed")
            return
        linked = self.ledger.advance_lifecycle(
            entry["key"], row["generation"], "validation",
            validation_attempt_id=admitted["attempt_id"],
        )
        try:
            current = validation_service.load_attempt(self.store, row["run_id"], admitted["attempt_id"])
        except validation_service.ValidationAdmissionError:
            return
        if current["status"] in validation_service.TERMINAL_STATUSES:
            self._handle_validation(entry, linked)

    def _handle_validation(self, entry: dict, row: dict) -> None:
        row = self._reconcile_validation_link(entry, row)
        attempt_id = row["validation_attempt_id"]
        if not attempt_id:
            self._admit_validation(entry, row)
            return
        try:
            attempt = validation_service.load_attempt(self.store, row["run_id"], attempt_id)
        except validation_service.ValidationAdmissionError as exc:
            self._finish(entry, row, "blocked_manual", exc.code, "validation_state_unavailable")
            return
        status = attempt["status"]
        if status in validation_service.ACTIVE_STATUSES:
            return
        if status == "cancelled":
            self._finish(entry, row, "cancelled", str((attempt.get("error") or {}).get("code") or "validation_cancelled"), "validation_cancelled")
            return
        if status == "failed":
            code = str((attempt.get("error") or {}).get("code") or "validation_failed")
            phase = validation_service.public_attempt(attempt, run=self.store.load(row["run_id"]), store=self.store).get("phase")
            if phase == "preparation" and code in VALIDATION_TRANSIENT_CODES and row["validation_retry_count"] < MAX_VALIDATION_RETRIES:
                self._schedule_retry(entry, row, code, "validation_transient_retry")
            else:
                self._finish(entry, row, "terminal_failure", code, "validation_failed")
            return
        if row["desired_policy"] == "build_validate":
            self._finish(entry, row, "success", "", "validation_succeeded")
            return
        self._publish(entry, row, attempt_id)

    def _last_publication(self, run_id: str) -> dict | None:
        run = self.store.load(run_id) or {}
        rows = run.get("publications") or []
        return rows[-1] if rows else None

    def _publish(self, entry: dict, row: dict, attempt_id: str) -> None:
        if not self.admission_open():
            return
        if not self._downstream_authorized(self._recipe(entry["recipe_id"]), "full"):
            validation_service.cancel_publication_intent(self.store, row["run_id"], attempt_id)
            self._finish(entry, row, "disabled", "automation_publication_disabled", "publication_admission_disabled")
            return
        previous = self._last_publication(row["run_id"])
        if previous and previous.get("status") == "failed" and row["state"] != "retry_delayed":
            row = self.ledger.advance_lifecycle(
                entry["key"], row["generation"], "publication",
                publication_attempt_id=previous["id"],
            )
            code = str((previous.get("error") or {}).get("code") or "publication_execution_failed")
            if code in PUBLICATION_TRANSIENT_CODES and row["publication_retry_count"] < MAX_PUBLICATION_RETRIES:
                self._schedule_retry(entry, row, code, "publication_transient_retry")
            else:
                validation_service.cancel_publication_intent(self.store, row["run_id"], attempt_id)
                classification = "blocked_manual" if code in PUBLICATION_BLOCKED_CODES else "terminal_failure"
                self._finish(entry, row, classification, code, "publication_failed")
            return
        run = self.store.load(row["run_id"])
        if not run:
            self._finish(entry, row, "blocked_manual", "build_run_not_found", "publication_state_unavailable")
            return
        try:
            with self.mutation_lease():
                row = self.ledger.advance_lifecycle(entry["key"], row["generation"], "publication")
                result = self.publish(row["run_id"], {"confirm": automation_service.publication_confirmation(run)})
        except Exception as exc:
            code = str(getattr(exc, "code", "publication_execution_failed"))
            if code == "application_shutting_down" or not self.admission_open():
                return
            if code in PUBLICATION_TRANSIENT_CODES and row["publication_retry_count"] < MAX_PUBLICATION_RETRIES:
                self._schedule_retry(entry, row, code, "publication_transient_retry")
            else:
                validation_service.cancel_publication_intent(self.store, row["run_id"], attempt_id)
                classification = "blocked_manual" if code in PUBLICATION_BLOCKED_CODES else "terminal_failure"
                self._finish(entry, row, classification, code, "publication_failed")
            return
        if result.get("id"):
            row = self.ledger.advance_lifecycle(
                entry["key"], row["generation"], "publication",
                publication_attempt_id=result["id"],
            )
        persisted_run = self.store.load(row["run_id"]) or {}
        persisted_attempt = next((
            attempt for attempt in persisted_run.get("publications") or []
            if isinstance(attempt, dict) and attempt.get("id") == result.get("id")
        ), None)
        if artifact_publication.successful_publication_proof(
            persisted_attempt, run=persisted_run,
        ) is not None:
            validation_service.complete_publication_intent(self.store, row["run_id"], attempt_id)
            self._finish(entry, row, "success", "", "publication_succeeded")
            return
        code = str(
            (result.get("error") or {}).get("code")
            or ("publication_proof_invalid" if result.get("status") == "success" else "publication_execution_failed")
        )
        if code in PUBLICATION_TRANSIENT_CODES and row["publication_retry_count"] < MAX_PUBLICATION_RETRIES:
            self._schedule_retry(entry, row, code, "publication_transient_retry")
            return
        validation_service.cancel_publication_intent(self.store, row["run_id"], attempt_id)
        classification = "blocked_manual" if code in PUBLICATION_BLOCKED_CODES else "terminal_failure"
        self._finish(entry, row, classification, code, "publication_failed")

    def _handle_run(self, entry: dict, row: dict) -> None:
        run = self.store.load(row["run_id"])
        if not run:
            self._finish(entry, row, "blocked_manual", "automation_run_not_found", "run_state_unavailable")
            return
        if run["status"] == "pending":
            self._admit_run(entry, row, self._recipe(entry["recipe_id"]), existing=True)
            return
        if run["status"] not in TERMINAL_RUN_STATUSES:
            return
        if run["status"] == "cancelled":
            self._finish(entry, row, "cancelled", str((run.get("error") or {}).get("code") or "execution_cancelled"), "run_cancelled")
            return
        success = run["status"] == ("prepared" if row["desired_policy"] == "test" else "success")
        if not success:
            self._finish(entry, row, "terminal_failure", str((run.get("error") or {}).get("code") or "execution_failed"), "run_failed")
            return
        if row["desired_policy"] in {"test", "build"}:
            self._finish(entry, row, "success", "", "test_succeeded" if row["desired_policy"] == "test" else "build_succeeded")
            return
        self._admit_validation(entry, row)

    def _admit_run(self, entry: dict, row: dict, recipe: dict | None, *, existing: bool = False) -> None:
        if not self.admission_open():
            return
        if not existing and not self._initial_authorized(entry, row, recipe):
            self._finish(entry, row, "disabled", "recipe_changed_before_run", "stale_claim_suppressed")
            return
        if existing and not self._downstream_authorized(recipe, row["desired_policy"]):
            self._finish(
                entry, row, "disabled", "automation_run_admission_disabled",
                "pending_run_admission_disabled",
            )
            return
        if existing and row.get("run_id"):
            try:
                recipe = validate_recipe_metadata(json.loads(
                    (self.store.run_dir(row["run_id"]) / "recipe.json").read_text()
                ))
            except (OSError, TypeError, ValueError):
                self._finish(entry, row, "blocked_manual", "build_run_snapshot_invalid", "run_state_unavailable")
                return
        if recipe is None:
            self._finish(entry, row, "disabled", "recipe_unavailable", "stale_claim_suppressed")
            return
        stale_claim = False
        stale_code = "recipe_changed_before_run"
        try:
            with self.mutation_lease():
                if not row["preallocated_run_id"]:
                    row = self.ledger.preallocate_run(entry["key"], row["generation"])
                else:
                    reconciled = self.ledger.reconcile_preallocated_run(
                        entry["key"], row["generation"], store=self.store,
                    )
                    row = reconciled["record"]
                live_recipe = self._recipe(entry["recipe_id"])
                if existing:
                    if not self._downstream_authorized(live_recipe, row["desired_policy"]):
                        stale_claim = True
                        stale_code = "automation_run_admission_disabled"
                elif not self._initial_authorized(entry, row, live_recipe):
                    stale_claim = True
                else:
                    recipe = live_recipe
                if not stale_claim:
                    metadata = {
                        "attempt_key": entry["key"], "generation": row["generation"],
                        "policy": row["desired_policy"], "expected_upstream_identity": entry["upstream_identity"],
                    }
                    origin = {"kind": "automation", "trigger": "upstream_change", "reason": self._reason(entry)}
                    self.enqueue_run(
                        self.execution_manager(), runtime_recipe_for_storage(recipe),
                        run_id=row["preallocated_run_id"], dry_run=row["desired_policy"] == "test",
                        origin=origin, automation=metadata,
                        created_callback=lambda run: self.ledger.link_run(
                            entry["key"], row["generation"], run["id"], store=self.store,
                        ),
                    )
        except Exception as exc:
            code = str(getattr(exc, "code", "execution_admission_failed"))
            if code == "application_shutting_down" or not self.admission_open():
                return
            if code in RUN_ADMISSION_TRANSIENT_CODES:
                if self.admission_open():
                    self._schedule_retry(entry, row, code, "run_admission_delayed")
                return
            self._finish(entry, row, "terminal_failure", code, "run_admission_failed")
            return
        if stale_claim:
            self._finish(
                entry, row, "disabled", stale_code,
                "pending_run_admission_disabled" if existing else "stale_claim_suppressed",
            )
            return
        linked = self.ledger.read()["attempts"][entry["key"]]["generations"][row["generation"]]
        self.ledger.advance_lifecycle(entry["key"], row["generation"], "run")
        current = self.store.load(linked["run_id"])
        if current and current["status"] in TERMINAL_RUN_STATUSES:
            self._handle_run(entry, self.ledger.read()["attempts"][entry["key"]]["generations"][row["generation"]])

    def _reason(self, entry: dict) -> str:
        identity = entry["upstream_identity"]
        created_at = entry["generations"][0]["created_at"]
        candidates = [
            candidate for key, candidate in self.ledger.read()["attempts"].items()
            if key != entry["key"] and candidate["recipe_id"] == entry["recipe_id"]
            and candidate["generations"][0]["created_at"] < created_at
        ]
        if candidates:
            previous = max(
                candidates,
                key=lambda candidate: candidate["generations"][0]["created_at"],
            )
            prior_identity = previous["upstream_identity"]
            if prior_identity == identity and previous["recipe_sha256"] != entry["recipe_sha256"]:
                return "recipe_revision_changed"
            if (
                identity.get("tracking") == "latest_release"
                and identity.get("source_type") == "release_asset"
                and prior_identity.get("release_id") == identity.get("release_id")
                and prior_identity != identity
            ):
                return "release_asset_changed"
        return "new_release" if identity.get("tracking") == "latest_release" else "ref_advanced"

    def advance(self, attempt_key: str, generation: int) -> None:
        lock = self._lock(attempt_key, generation)
        if not lock.acquire(blocking=False):
            return
        try:
            document = self.ledger.read()
            entry, row = self._row(document, attempt_key, generation)
            if not entry or row["state"] in {"terminal", "blocked"} or not self._retry_due(row):
                return
            if not self._is_recipe_turn(document, attempt_key, generation, entry["recipe_id"]):
                return
            if row["run_id"]:
                if row["stage"] in {"validation_admission", "validation"} or (
                    row["state"] == "retry_delayed" and row["stage"] == "validation"
                ):
                    # An admission-stage row can still point at the prior failed
                    # Validation after a crash before manager.admit. Reconcile a
                    # newly created attempt first; otherwise replay admission.
                    if row["state"] == "retry_delayed" or row["stage"] == "validation_admission":
                        self._admit_validation(entry, row)
                    else:
                        self._handle_validation(entry, row)
                elif row["stage"] == "publication":
                    self._handle_validation(entry, row)
                else:
                    self._handle_run(entry, row)
            else:
                self._admit_run(entry, row, self._recipe(entry["recipe_id"]))
        except AutomationLedgerError:
            LOGGER.exception("Automation coordination failed for %s/%s", attempt_key, generation)
        finally:
            lock.release()

    def advance_all(self) -> None:
        document = self.ledger.read()
        candidates = []
        for key, entry in document["attempts"].items():
            for row in entry["generations"]:
                if row["state"] not in {"terminal", "blocked"}:
                    candidates.append((row["created_at"], key, row["generation"]))
        for _created, key, generation in sorted(candidates):
            self.advance(key, generation)

    def on_detection(self, result: dict) -> None:
        key, generation = result.get("attempt_key"), result.get("generation")
        if isinstance(key, str) and isinstance(generation, int):
            self.advance(key, generation)

    def on_run_terminal(self, run_id: str) -> None:
        run = self.store.load(run_id)
        metadata = (run or {}).get("automation")
        if isinstance(metadata, dict):
            self.advance(metadata["attempt_key"], metadata["generation"])
            self.advance_all()

    def on_validation_terminal(self, run_id: str, attempt_id: str) -> None:
        try:
            metadata = validation_service.load_automation(self.store, run_id, attempt_id)
        except (OSError, ValueError, validation_service.ValidationAdmissionError):
            return
        if metadata.get("attempt_key"):
            self.advance(metadata["attempt_key"], metadata["generation"])
            self.advance_all()

    def next_retry_delay(self) -> float | None:
        delays = []
        for entry in self.ledger.read()["attempts"].values():
            for row in entry["generations"]:
                if row["state"] == "retry_delayed" and row.get("not_before"):
                    delays.append(max(0.0, datetime.fromisoformat(row["not_before"]).timestamp() - self.wall_clock()))
        return min(delays) if delays else None
