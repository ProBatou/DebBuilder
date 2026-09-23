"""Bounded public automation status and operator actions.

The automation ledger supplies only coordination/linkage.  Linked Run,
Validation, and Publication stores remain authoritative for their stages.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from . import artifact_publication, recipe_store, validation_service
from .automation_ledger import AutomationLedger, AutomationLedgerError, MAX_GENERATIONS
from .automation_scheduler import AutomationRetryStore, AutomationSchedulerError
from .build_store import BuildStore, canonical_recipe_sha256
from .recipe_schema import BUILTIN_RECIPE_ID, require_safe_name, validate_recipe_metadata


ACTIVE_STATES = frozenset({
    "checking", "detected", "test_queued", "testing", "build_queued", "building",
    "validation_pending", "validating", "publication_pending", "publishing", "retry_scheduled",
})
RETRYABLE_CLASSIFICATIONS = frozenset({"terminal_failure", "cancelled", "blocked_manual"})
NON_RETRYABLE_DIAGNOSTICS = frozenset({
    "automation_downstream_disabled", "downstream_admission_disabled",
    "automation_publication_disabled", "publication_admission_disabled",
    "recipe_changed_during_detection", "automation_recipe_stale",
    "ambiguous_asset", "ambiguous_release_asset", "downgrade_refused",
    "publication_identity_conflict",
    "unsupported_source", "invalid_automation_configuration", "invalid_configuration",
    "incomplete_upstream_identity",
})
PUBLIC_MESSAGES = {
    "ambiguous_asset": "More than one upstream asset matches this Recipe.",
    "ambiguous_release_asset": "More than one upstream asset matches this Recipe.",
    "automation_admission_blocked": "Automation admission is temporarily unavailable.",
    "automation_ledger_bounds_exceeded": "Automation history capacity is exhausted.",
    "automation_ledger_capacity_exhausted": "Automation history capacity is exhausted.",
    "automation_ledger_future_version": "Automation state was written by a newer application version.",
    "automation_ledger_malformed": "Automation state requires operator attention.",
    "automation_ledger_unavailable": "Automation state is unavailable.",
    "automation_recipe_capacity": "Recipe automation capacity is exhausted.",
    "automation_run_unavailable": "The linked Build Run is unavailable.",
    "automation_scheduler_disabled": "Automatic upstream checks are disabled globally.",
    "automation_scheduler_internal_error": "The automation scheduler requires operator attention.",
    "automation_scheduler_state_capacity": "Automation retry-state capacity is exhausted.",
    "automation_scheduler_state_future_version": "Automation retry state was written by a newer application version.",
    "automation_scheduler_state_malformed": "Automation retry state requires operator attention.",
    "automation_scheduler_state_unavailable": "Automation retry state is unavailable.",
    "automation_scheduler_unavailable": "The automation scheduler is unavailable.",
    "downgrade_refused": "Publication would downgrade the repository package.",
    "incomplete_upstream_identity": "Upstream did not provide a complete immutable identity.",
    "invalid_automation_configuration": "Recipe automation configuration is invalid.",
    "invalid_configuration": "Recipe automation configuration is invalid.",
    "manual_action_required": "This Recipe requires a manual source version.",
    "publication_identity_conflict": "Published repository state conflicts with this artifact.",
    "publication_state_unavailable": "The linked publication state is unavailable.",
    "source_not_found": "The configured upstream source was not found.",
    "unsupported_source": "This Recipe source mode cannot be automated.",
    "upstream_unavailable": "Upstream metadata is temporarily unavailable.",
    "validation_state_unavailable": "The linked validation state is unavailable.",
}
MAX_PUBLICATION_ATTEMPTS = 512


class AutomationActionError(RuntimeError):
    """Stable bounded HTTP-facing automation action failure."""

    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": {}}


def _safe_code(value, fallback: str = "automation_blocked") -> str:
    text = str(value or fallback)
    return text[:80] if text.replace("_", "").replace("-", "").replace(".", "").isalnum() else fallback


def _blocker(code) -> dict | None:
    if not code:
        return None
    safe = _safe_code(code)
    return {"code": safe, "message": PUBLIC_MESSAGES.get(safe, "Automation requires operator attention.")}


def _public_timestamp(value) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _latest_entry(document: dict, recipe_id: str) -> dict | None:
    rows = [entry for entry in document.get("attempts", {}).values() if entry.get("recipe_id") == recipe_id]
    if not rows:
        return None
    return max(rows, key=lambda entry: (
        entry["generations"][0]["detected_at"],
        entry["generations"][0]["created_at"],
        entry["key"],
    ))


def _public_revision(entry: dict | None, row: dict | None) -> str:
    """Bind operator actions to one exact private attempt without exposing it."""
    if not entry or not row:
        return ""
    material = "|".join((
        str(entry.get("key") or ""),
        str(row.get("generation", -1)),
        str(row.get("updated_at") or ""),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AutomationStatusService:
    """Read one bounded public projection and admit explicit operator actions."""

    def __init__(
        self,
        recipe_directory: str | Path,
        ledger: AutomationLedger,
        retry_store: AutomationRetryStore,
        build_store: BuildStore,
        *,
        scheduler: Callable[[], object | None] = lambda: None,
    ):
        self.recipe_directory = Path(recipe_directory)
        self.ledger = ledger
        self.retry_store = retry_store
        self.build_store = build_store
        self.scheduler = scheduler

    def _recipe_path(self, recipe_id: str) -> Path:
        require_safe_name(recipe_id, "Recipe ID")
        return self.recipe_directory / f"{recipe_id}.json"

    def _recipe(self, recipe_id: str) -> dict:
        path = self._recipe_path(recipe_id)
        try:
            path.lstat()
        except FileNotFoundError as exc:
            raise AutomationActionError("recipe_not_found", "Recipe was not found", status=404) from exc
        except OSError as exc:
            raise AutomationActionError(
                "invalid_automation_configuration", "Recipe automation configuration is invalid", status=422,
            ) from exc
        try:
            recipe = validate_recipe_metadata(
                recipe_store.load_recipe(path)
            )
        except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError) as exc:
            raise AutomationActionError(
                "invalid_automation_configuration", "Recipe automation configuration is invalid", status=422,
            ) from exc
        if recipe.get("name") != recipe_id:
            raise AutomationActionError(
                "invalid_automation_configuration", "Recipe automation configuration is invalid", status=422,
            )
        return recipe

    @staticmethod
    def _scheduler_public(scheduler) -> dict:
        if scheduler is None:
            return {
                "state": "stopped", "checks_enabled": False, "admission_open": False, "last_pass_started": None,
                "last_pass_finished": None, "next_scheduled_check": None,
                "active_recipe_checks": 0, "blocker": _blocker("automation_scheduler_unavailable"),
            }
        raw = scheduler.status()
        blocker = raw.get("global_blocker") or {}
        checks_enabled = raw.get("checks_enabled", True) is True
        return {
            "state": "running" if raw.get("state") == "running" else "stopped",
            "checks_enabled": checks_enabled,
            "admission_open": raw.get("admission_open") is True,
            "last_pass_started": raw.get("last_pass_started"),
            "last_pass_finished": raw.get("last_pass_finished"),
            "next_scheduled_check": raw.get("next_scheduled_check"),
            "active_recipe_checks": min(8, max(0, int(raw.get("active_recipe_checks") or 0))),
            "blocker": _blocker(blocker.get("code") or (None if checks_enabled else "automation_scheduler_disabled")),
        }

    @staticmethod
    def _activity(scheduler, recipe_id: str) -> dict:
        if scheduler is None:
            return {"queued": False, "checking": False}
        try:
            return scheduler.recipe_activity(recipe_id)
        except (AutomationSchedulerError, ValueError):
            return {"queued": False, "checking": False}

    def _canonical_links(self, entry: dict, row: dict) -> tuple[dict | None, dict | None, dict | None, dict | None]:
        try:
            run = self.build_store.load(row["run_id"]) if row.get("run_id") else None
        except (OSError, TypeError, ValueError):
            run = None
        run_public = None
        validation = None
        publication = None
        link_blocker = None
        if row.get("run_id"):
            if run is None:
                link_blocker = _blocker("automation_run_unavailable")
            else:
                run_public = {
                    "id": str(run["id"])[:128],
                    "status": str(run.get("status") or "unknown")[:32],
                    "mode": str(run.get("mode") or "")[:32],
                    "url": f"/#logs/{run['id']}",
                }
        attempt_id = row.get("validation_attempt_id")
        if run is not None and attempt_id:
            try:
                attempt = validation_service.load_attempt(self.build_store, row["run_id"], attempt_id)
                public = validation_service.public_attempt(attempt, run=run, store=self.build_store)
                validation = {
                    "attempt_id": str(public["attempt_id"])[:128],
                    "status": str(public.get("status") or "unknown")[:32],
                    "phase": str(public.get("phase") or "")[:32],
                    "url": str(public.get("status_url") or "")[:300],
                    "error_code": _safe_code((public.get("error") or {}).get("code"), "validation_failed") if public.get("error") else None,
                }
            except (OSError, TypeError, ValueError, validation_service.ValidationAdmissionError):
                link_blocker = _blocker("validation_state_unavailable")
        publication_id = row.get("publication_attempt_id")
        if run is not None and publication_id:
            rows = run.get("publications") or []
            found = None
            if isinstance(rows, list) and len(rows) <= MAX_PUBLICATION_ATTEMPTS:
                found = next((item for item in rows if isinstance(item, dict) and item.get("id") == publication_id), None)
            if found is None:
                link_blocker = _blocker("publication_state_unavailable")
            else:
                error = found.get("error") if isinstance(found.get("error"), dict) else None
                status = artifact_publication.publication_attempt_status(found, run=run)
                if found.get("status") == "success" and status != "success":
                    error = {"code": "publication_proof_invalid"}
                publication = {
                    "attempt_id": str(found.get("id") or "")[:128],
                    "status": status[:32],
                    "requested_at": _public_timestamp(found.get("requested_at")),
                    "finished_at": _public_timestamp(found.get("finished_at")),
                    "error_code": _safe_code(error.get("code"), "publication_failed") if error else None,
                }
        return run_public, validation, publication, link_blocker

    @staticmethod
    def _state(row: dict | None, run: dict | None, validation: dict | None, publication: dict | None) -> tuple[str, str]:
        # Durable coordination wins over an earlier canonical stage: a failed
        # Validation can have a scheduled retry, and a successful Build can be
        # followed by a disabled or blocked downstream admission.
        if row and row["state"] == "retry_delayed":
            return "retry_scheduled", "retry_scheduled"
        if row and row["state"] == "blocked":
            return "blocked", "blocked"
        if row and row["state"] == "terminal":
            classification = row.get("terminal_classification")
            if classification == "success":
                if row.get("desired_policy") == "full" and (
                    not publication or publication.get("status") != "success"
                ):
                    return "failed", "failed"
                return "up_to_date", "success"
            if classification == "cancelled":
                return "cancelled", "cancelled"
            if classification == "disabled":
                return "manual", "disabled"
            return "failed", "failed"
        if publication:
            if publication["status"] == "running":
                return "publishing", "active"
            if publication["status"] == "failed":
                return "failed", "failed"
            if publication["status"] == "success":
                return "up_to_date", "success"
        if validation:
            if validation["status"] in {"queued", "running", "cancelling"}:
                return "validating", "active"
            if validation["status"] in {"failed", "cancelled"}:
                return "failed" if validation["status"] == "failed" else "cancelled", validation["status"]
            if validation["status"] == "success" and row and row["desired_policy"] == "full":
                return "publication_pending", "active"
            if validation["status"] == "success":
                return "up_to_date", "success"
        if run:
            status = run["status"]
            test = run.get("mode") == "dry_run"
            if status in {"pending", "queued"}:
                return ("test_queued" if test else "build_queued"), "active"
            if status in {"running", "cancelling"}:
                return ("testing" if test else "building"), "active"
            if status == "failed":
                return "failed", "failed"
            if status == "cancelled":
                return "cancelled", "cancelled"
            if row and row["desired_policy"] in {"build_validate", "full"}:
                return "validation_pending", "active"
            return "up_to_date", "success"
        if not row:
            return "watching", "never_checked"
        return "detected", "active"

    def status(self, recipe_id: str, *, documents: tuple | None = None) -> dict:
        recipe = self._recipe(recipe_id)
        automation = recipe["automation"]
        scheduler = self.scheduler()
        scheduler_public = self._scheduler_public(scheduler)
        activity = self._activity(scheduler, recipe_id)
        ledger_blocker = None
        retry_blocker = None
        if documents is None:
            try:
                ledger_document = self.ledger.read()
            except AutomationLedgerError as exc:
                ledger_document = {"attempts": {}}
                ledger_blocker = _blocker(exc.code)
            try:
                retry_document = self.retry_store.read()
            except AutomationSchedulerError as exc:
                retry_document = {"recipes": {}}
                retry_blocker = _blocker(exc.code)
        else:
            ledger_document, retry_document = documents[:2]
            if len(documents) > 2:
                ledger_blocker, retry_blocker = documents[2:4]
        entry = _latest_entry(ledger_document, recipe_id)
        row = entry["generations"][-1] if entry else None
        current_recipe_sha256 = canonical_recipe_sha256(recipe)
        attempt_current = bool(entry and entry.get("recipe_sha256") == current_recipe_sha256)
        retry = retry_document.get("recipes", {}).get(recipe_id)
        if retry and retry.get("recipe_sha256") != current_recipe_sha256:
            retry = None
        run, validation, publication, link_blocker = self._canonical_links(entry, row) if row else (None, None, None, None)
        state, result = self._state(row, run, validation, publication)
        eligible = bool(
            recipe_id != BUILTIN_RECIPE_ID and recipe.get("active")
            and automation.get("enabled") and automation.get("policy") != "manual"
        )
        if not recipe.get("active"):
            state, result = "inactive", "disabled"
        elif not eligible:
            state, result = "manual", "disabled"
        elif activity.get("queued") or activity.get("checking"):
            state, result = "checking", "active"
        blocker = ledger_blocker or retry_blocker or link_blocker or scheduler_public.get("blocker")
        if retry:
            retry_kind = retry.get("retry_class")
            if retry_kind == "operator":
                blocker = _blocker(retry.get("diagnostic") or retry.get("classification"))
                if eligible:
                    state, result = "blocked", "blocked"
            elif retry.get("not_before") and eligible and row is None:
                state, result = "retry_scheduled", "retry_scheduled"
        if link_blocker and eligible and state in {
            "watching", "detected", "validation_pending", "publication_pending",
        }:
            state, result = "blocked", "blocked"
        elif blocker and eligible and state in {"watching", "detected"}:
            state, result = "blocked", "blocked"
        diagnostic = _safe_code(
            (row or {}).get("last_error_code") or (row or {}).get("diagnostic")
            or (retry or {}).get("diagnostic") or (blocker or {}).get("code"),
            "automation_blocked",
        ) if (row and (row.get("last_error_code") or row.get("diagnostic"))) or retry or blocker else None
        can_retry = bool(
            eligible and attempt_current and row and row.get("terminal_classification") in RETRYABLE_CLASSIFICATIONS
            and row.get("diagnostic") not in NON_RETRYABLE_DIAGNOSTICS
            and row.get("last_error_code") not in NON_RETRYABLE_DIAGNOSTICS
            and len(entry["generations"]) < MAX_GENERATIONS
            and state not in ACTIVE_STATES
            and scheduler_public["state"] == "running" and scheduler_public["admission_open"]
        )
        identity = (entry or {}).get("upstream_identity") or {}
        observation = activity.get("observation") or {}
        if observation.get("recipe_sha256") != current_recipe_sha256:
            observation = {}
        last_check_at = max(
            [value for value in (
                observation.get("checked_at"), (row or {}).get("detected_at"), (retry or {}).get("updated_at"),
            ) if value],
            default=None,
        )
        revision = _public_revision(entry, row)
        return {
            "recipe_id": recipe_id,
            "recipe_active": recipe.get("active") is True,
            "automation": {
                "enabled": automation.get("enabled") is True,
                "policy": automation.get("policy"),
                "managed": recipe_id == BUILTIN_RECIPE_ID,
            },
            "eligible": eligible,
            "state": state,
            "stage": str((row or {}).get("stage") or "detection")[:32],
            "result": result,
            "last_check_at": last_check_at,
            "detected": {
                "version": str(observation.get("display_version") or identity.get("resolved_version") or "")[:200],
                "ref": str(observation.get("display_ref") or identity.get("resolved_ref") or "")[:200],
            },
            "attempt_policy": (row or {}).get("desired_policy"),
            "run": run,
            "validation": validation,
            "publication": publication,
            "retry": {
                "scheduled": state == "retry_scheduled",
                "not_before": (row or {}).get("not_before") or (retry or {}).get("not_before"),
            },
            "blocked": _blocker(diagnostic) if state == "blocked" else None,
            "diagnostic_code": diagnostic,
            "can_check_now": bool(
                eligible and scheduler_public["checks_enabled"]
                and not activity.get("queued") and not activity.get("checking")
                and scheduler_public["state"] == "running" and scheduler_public["admission_open"]
            ),
            "can_retry": can_retry,
            "generation": (row or {}).get("generation"),
            "revision": revision,
            "state_active": state in ACTIVE_STATES,
            "scheduler": scheduler_public,
        }

    def statuses(self, recipe_ids: list[str]) -> dict[str, dict]:
        """Project many Recipes with one bounded ledger/retry-state read."""
        ledger_blocker = None
        retry_blocker = None
        try:
            ledger_document = self.ledger.read()
        except AutomationLedgerError as exc:
            ledger_document = {"attempts": {}}
            ledger_blocker = _blocker(exc.code)
        try:
            retry_document = self.retry_store.read()
        except AutomationSchedulerError as exc:
            retry_document = {"recipes": {}}
            retry_blocker = _blocker(exc.code)
        documents = (ledger_document, retry_document, ledger_blocker, retry_blocker)
        result = {}
        for recipe_id in recipe_ids[:256]:
            try:
                result[recipe_id] = self.status(recipe_id, documents=documents)
            except AutomationActionError:
                continue
        return result

    def check_now(self, recipe_id: str) -> dict:
        recipe = self._recipe(recipe_id)
        automation = recipe["automation"]
        if recipe_id == BUILTIN_RECIPE_ID:
            raise AutomationActionError("automation_managed", "Built-in Recipe automation is application-managed", status=403)
        if not recipe.get("active"):
            raise AutomationActionError("recipe_inactive", "Enable the Recipe before checking upstream")
        if not automation.get("enabled") or automation.get("policy") == "manual":
            raise AutomationActionError(
                "automation_disabled", "Enable an automatic policy before checking upstream",
            )
        scheduler = self.scheduler()
        if scheduler is None:
            raise AutomationActionError("automation_scheduler_unavailable", "Automation scheduler is unavailable", status=503)
        try:
            accepted = scheduler.request_recipe(recipe_id)
        except AutomationSchedulerError as exc:
            raise AutomationActionError(exc.code, PUBLIC_MESSAGES.get(exc.code, str(exc)), status=503) from exc
        return {**accepted, "status": self.status(recipe_id)}

    def retry(self, recipe_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"generation", "revision"}:
            raise AutomationActionError("invalid_automation_retry_request", "Retry requires the observed generation and revision", status=400)
        generation = payload.get("generation")
        revision = payload.get("revision")
        if isinstance(generation, bool) or not isinstance(generation, int) or not isinstance(revision, str) or len(revision) > 200:
            raise AutomationActionError("invalid_automation_retry_request", "Retry request is invalid", status=400)
        current = self.status(recipe_id)
        if not current["can_retry"]:
            code = "automation_disabled" if not current["eligible"] else "automation_retry_not_allowed"
            raise AutomationActionError(code, "This automation attempt cannot be retried")
        if generation != current["generation"] or revision != current["revision"]:
            raise AutomationActionError("automation_retry_stale", "Automation status changed; refresh before retrying")
        try:
            document = self.ledger.read()
        except AutomationLedgerError as exc:
            raise AutomationActionError(
                exc.code, PUBLIC_MESSAGES.get(exc.code, "Automation state is unavailable"), status=409,
            ) from exc
        entry = _latest_entry(document, recipe_id)
        if entry is None:
            raise AutomationActionError("automation_attempt_not_found", "Automation attempt was not found", status=404)
        row = entry["generations"][-1]
        if _public_revision(entry, row) != revision or row.get("generation") != generation:
            raise AutomationActionError("automation_retry_stale", "Automation status changed; refresh before retrying")
        try:
            claim = self.ledger.explicit_retry_current_recipe(
                self._recipe_path(recipe_id), recipe_id, entry["key"], generation,
                expected_updated_at=row.get("updated_at"),
            )
        except AutomationLedgerError as exc:
            statuses = {
                "automation_attempt_not_found": 404,
                "automation_ledger_bounds_exceeded": 409,
                "automation_recipe_stale": 409,
                "automation_retry_not_terminal": 409,
                "automation_retry_stale": 409,
            }
            raise AutomationActionError(
                exc.code, PUBLIC_MESSAGES.get(exc.code, str(exc)), status=statuses.get(exc.code, 409),
            ) from exc
        scheduler = self.scheduler()
        if scheduler is not None:
            scheduler.request_orchestration()
        return {
            "accepted": True,
            "created": claim.created,
            "generation": claim.generation,
            "status": self.status(recipe_id),
        }
