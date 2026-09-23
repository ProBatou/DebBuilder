"""Data contracts for isolated DebBuilder build runs."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from .resource_limits import default_contract, validate_contract
from .automation_identity import automation_attempt_key, normalize_upstream_identity

CURRENT_RUN_SCHEMA_VERSION = 4


class RunDocumentError(ValueError):
    def __init__(self, code: str, message: str, *, source_version=None, target_version=CURRENT_RUN_SCHEMA_VERSION):
        super().__init__(message)
        self.code = code
        self.source_version = source_version
        self.target_version = target_version


def _current_run_document(document: dict) -> dict:
    if not isinstance(document, dict):
        raise RunDocumentError("invalid_run", "Run document must be an object")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RunDocumentError("invalid_run_schema_version", "Run schema_version must be a positive integer", source_version=version)
    if version > CURRENT_RUN_SCHEMA_VERSION:
        raise RunDocumentError(
            "future_run_schema_version",
            f"Run schema v{version} is newer than supported v{CURRENT_RUN_SCHEMA_VERSION}",
            source_version=version,
        )
    if version != CURRENT_RUN_SCHEMA_VERSION:
        raise RunDocumentError(
            "unsupported_run_schema_version",
            f"Run schema v{version} is unsupported; expected v{CURRENT_RUN_SCHEMA_VERSION}",
            source_version=version,
        )
    return copy.deepcopy(document)


def admission_metadata_sha256(
    recipe_id: str,
    recipe_sha256: str,
    mode: str,
    origin: dict,
    automation: dict | None,
    manual_source_provenance: dict | None,
) -> str:
    """Seal immutable Run admission metadata with deterministic JSON."""
    payload = {
        "schema_version": 1,
        "recipe_id": recipe_id,
        "recipe_sha256": recipe_sha256,
        "mode": mode,
        "origin": origin,
        "automation": automation,
        "manual_source_provenance": manual_source_provenance,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


STEP_NAMES = (
    "source", "detection", "dependencies", "source_changes", "build",
    "staging", "debian_metadata", "systemd", "package", "artifact",
)
STEP_STATUSES = frozenset({"pending", "running", "success", "failed", "skipped", "cancelled"})
RUN_STATUSES = frozenset({
    "pending", "queued", "running", "cancelling",
    "prepared", "success", "failed", "cancelled",
})
RUN_ORIGINS = frozenset({"manual", "automation", "automation_check_now"})
RUN_TRIGGERS = frozenset({"manual", "upstream_change", "operator_check_now"})
RUN_REASONS = frozenset({"new_release", "release_asset_changed", "ref_advanced", "recipe_revision_changed", "operator_check_now"})
AUTOMATION_POLICIES = frozenset({"manual", "detect", "test", "build", "build_validate", "full"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_step(name: str) -> dict:
    if name not in STEP_NAMES:
        raise ValueError(f"unknown build step: {name}")
    return {
        "name": name,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "summary": "",
        "error": None,
        "details": {},
    }


def _bounded_optional_text(value, field: str, maximum: int = 240) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must be a bounded string or null")
    return value


def normalize_run_origin(value: dict | None, automation: dict | None) -> tuple[dict, dict | None]:
    """Validate immutable admission metadata without exposing coordination policy."""
    value = {"kind": "manual", "trigger": "manual", "reason": None} if value is None else value
    if not isinstance(value, dict) or set(value) != {"kind", "trigger", "reason"}:
        raise ValueError("Run origin has an invalid shape")
    kind = value.get("kind")
    if kind not in RUN_ORIGINS:
        raise ValueError("Run origin kind is unsupported")
    trigger = _bounded_optional_text(value.get("trigger"), "origin.trigger", 80)
    reason = _bounded_optional_text(value.get("reason"), "origin.reason")
    if trigger not in RUN_TRIGGERS:
        raise ValueError("origin.trigger is unsupported")
    if reason is not None and reason not in RUN_REASONS:
        raise ValueError("origin.reason is unsupported")
    if kind == "manual" and trigger != "manual":
        raise ValueError("Manual Run trigger must be manual")
    if kind == "manual" and reason is not None:
        raise ValueError("Manual Run cannot carry an automation reason")
    if kind == "automation" and trigger != "upstream_change":
        raise ValueError("Automation Run trigger must be upstream_change")
    if kind == "automation" and reason == "operator_check_now":
        raise ValueError("Automation Run reason must describe an upstream change")
    if kind == "automation_check_now" and trigger != "operator_check_now":
        raise ValueError("Check-now Run trigger must be operator_check_now")
    if kind == "automation_check_now" and reason not in {None, "operator_check_now"}:
        raise ValueError("Check-now Run reason must describe the operator request")
    normalized_origin = {"kind": kind, "trigger": trigger, "reason": reason}
    if kind == "manual":
        if automation is not None:
            raise ValueError("Manual Runs cannot carry automation coordination metadata")
        return normalized_origin, None
    if not isinstance(automation, dict) or set(automation) != {"attempt_key", "generation", "policy", "expected_upstream_identity"}:
        raise ValueError("Automated Runs require exact automation metadata")
    attempt_key = automation.get("attempt_key")
    generation = automation.get("generation")
    policy = automation.get("policy")
    if not isinstance(attempt_key, str) or not re.fullmatch(r"automation-v1-[0-9a-f]{64}", attempt_key):
        raise ValueError("automation.attempt_key is invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation < 8:
        raise ValueError("automation.generation is invalid")
    if policy not in AUTOMATION_POLICIES or policy in {"manual", "detect"}:
        raise ValueError("automation.policy is unsupported")
    identity = normalize_upstream_identity(automation.get("expected_upstream_identity"))
    if identity["completeness"] != "complete":
        raise ValueError("Automated Run expected upstream identity must be complete")
    return normalized_origin, {
        "attempt_key": attempt_key,
        "generation": generation,
        "policy": policy,
        "expected_upstream_identity": identity,
    }


def require_automation_mode(policy: str, mode: str) -> None:
    """Keep an automated Run within the exact Recipe policy it captured."""
    required = "dry_run" if policy == "test" else "build"
    if mode != required:
        raise ValueError(f"Automation policy {policy} requires Run mode {required}")


def _normalize_manual_source_provenance(value: dict | None, automation: dict | None) -> dict | None:
    if automation is not None:
        if value is not None:
            raise ValueError("Automated Runs cannot duplicate manual source provenance")
        return None
    if value is None:
        return None
    identity = normalize_upstream_identity(value)
    if identity["completeness"] != "complete":
        raise ValueError("Manual Run source provenance must be complete")
    return identity


def new_run(run_id: str, recipe_id: str, mode: str, workspace: str, recipe_sha256: str, *, resource_contract: dict | None = None, origin: dict | None = None, automation: dict | None = None, manual_source_provenance: dict | None = None) -> dict:
    if mode not in {"dry_run", "build"}:
        raise ValueError("build mode must be dry_run or build")
    now = utc_now()
    normalized_origin, normalized_automation = normalize_run_origin(origin, automation)
    normalized_manual_provenance = _normalize_manual_source_provenance(
        manual_source_provenance, normalized_automation,
    )
    if normalized_automation is not None and automation_attempt_key(
        recipe_id, normalized_automation["expected_upstream_identity"], recipe_sha256,
    ) != normalized_automation["attempt_key"]:
        raise ValueError("Run automation attempt key does not match its immutable inputs")
    if normalized_automation is not None:
        require_automation_mode(normalized_automation["policy"], mode)
    admission_sha256 = admission_metadata_sha256(
        recipe_id, recipe_sha256, mode, normalized_origin, normalized_automation,
        normalized_manual_provenance,
    )
    return {
        "schema_version": CURRENT_RUN_SCHEMA_VERSION,
        "id": run_id,
        "recipe_id": recipe_id,
        "recipe_sha256": recipe_sha256,
        "mode": mode,
        "status": "pending",
        "created_at": now,
        "created_at_epoch": time.time(),
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "workspace": workspace,
        "version": {"upstream": "", "debian": ""},
        "steps": [new_step(name) for name in STEP_NAMES],
        "artifact": None,
        "error": None,
        "events": [],
        "resource_limits": validate_contract(resource_contract or default_contract()),
        "origin": normalized_origin,
        "automation": normalized_automation,
        "manual_source_provenance": normalized_manual_provenance,
        "admission_sha256": admission_sha256,
    }


def validate_run(run: dict) -> dict:
    current = _current_run_document(run)
    if "validations" in current:
        raise ValueError("Current Run schema must not persist Validation lifecycle state")
    artifact = current.get("artifact")
    if isinstance(artifact, dict) and "validations" in artifact:
        raise ValueError("Current artifact schema must not persist Validation lifecycle state")
    if isinstance(artifact, dict) and "publications" in artifact:
        raise ValueError("Current artifact schema must not persist Publication lifecycle state")
    if any(field not in current for field in (
        "origin", "automation", "manual_source_provenance", "admission_sha256",
    )):
        raise ValueError("Current Run is missing immutable admission metadata")
    validate_contract(current.get("resource_limits"))
    persisted_origin = current["origin"]
    persisted_automation = current["automation"]
    persisted_manual_provenance = current["manual_source_provenance"]
    origin, automation = normalize_run_origin(persisted_origin, persisted_automation)
    current["origin"] = origin
    current["automation"] = automation
    manual_source_provenance = _normalize_manual_source_provenance(
        persisted_manual_provenance, automation,
    )
    current["manual_source_provenance"] = manual_source_provenance
    if (
        persisted_origin != origin
        or persisted_automation != automation
        or persisted_manual_provenance != manual_source_provenance
    ):
        raise ValueError("Current Run admission metadata must be canonical")
    if automation is not None and automation_attempt_key(
        str(current.get("recipe_id") or ""), automation["expected_upstream_identity"],
        str(current.get("recipe_sha256") or ""),
    ) != automation["attempt_key"]:
        raise ValueError("Run automation attempt key does not match its immutable inputs")
    if automation is not None:
        require_automation_mode(automation["policy"], str(current.get("mode") or ""))
    expected_admission_sha256 = admission_metadata_sha256(
        str(current.get("recipe_id") or ""), str(current.get("recipe_sha256") or ""),
        str(current.get("mode") or ""), origin, automation, manual_source_provenance,
    )
    if current.get("admission_sha256") != expected_admission_sha256:
        raise ValueError("Run immutable admission metadata seal is invalid")
    if current.get("status") not in RUN_STATUSES:
        raise ValueError(f"invalid run status: {current.get('status')}")
    steps = current.get("steps")
    if not isinstance(steps, list) or [step.get("name") for step in steps] != list(STEP_NAMES):
        raise ValueError("build run has an invalid step sequence")
    for step in steps:
        if step.get("status") not in STEP_STATUSES:
            raise ValueError(f"invalid status for step {step.get('name')}")
    return current
