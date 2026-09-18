"""Durable automation claims; downstream lifecycle truth remains in its stores.

Lock ordering: callers may acquire this ledger lease only when they hold no
Recipe, Run, validation, publication, or repository lease.  The guarded claim
methods alone acquire ledger then Recipe, and perform no network or downstream
lifecycle work while those leases are held.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import recipe_store, storage
from .automation_identity import automation_attempt_key, normalize_upstream_identity
from .build_models import utc_now
from .build_store import BuildStore, canonical_recipe_sha256


LEDGER_SCHEMA_VERSION = 1
LEDGER_FILENAME = "automation-ledger.json"
LOCK_FILENAME = ".automation-ledger.lock"
MAX_RECIPES = 256
MAX_ATTEMPTS = 2048
MAX_ATTEMPTS_PER_RECIPE = 128
MAX_GENERATIONS = 8
MAX_DIAGNOSTIC = 80
MAX_ERROR_CODE = 80
MAX_LEDGER_BYTES = 16 * 1024 * 1024
STATES = {"detected", "claimed", "run_preallocated", "admitted", "terminal", "retry_delayed", "blocked"}
TERMINAL_CLASSIFICATIONS = {"success", "terminal_failure", "transient_retryable", "blocked_manual", "cancelled", "disabled"}
RETRY_CLASSES = {"none", "transient", "operator"}
POLICIES = {"manual", "detect", "test", "build", "build_validate", "full"}
ENTRY_FIELDS = {"key", "recipe_id", "recipe_sha256", "upstream_identity", "generations"}
GENERATION_FIELDS = {
    "generation", "state", "desired_policy", "detected_at", "created_at", "updated_at",
    "preallocated_run_id", "run_id", "terminal_classification", "retry_class",
    "retry_count", "not_before", "last_error_code", "diagnostic", "stage",
    "validation_attempt_id", "publication_attempt_id", "completion_notified",
    "run_retry_count", "validation_retry_count", "publication_retry_count",
}
CP3_GENERATION_FIELDS = GENERATION_FIELDS - {
    "stage", "validation_attempt_id", "publication_attempt_id", "completion_notified",
    "run_retry_count", "validation_retry_count", "publication_retry_count",
}
STAGES = {
    "detection", "run_admission", "run", "validation_admission", "validation",
    "publication", "terminal",
}


class AutomationLedgerError(RuntimeError):
    def __init__(self, code: str, message: str, *, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ClaimResult:
    created: bool
    attempt_key: str
    generation: int
    record: dict


def _timestamp(value, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AutomationLedgerError("automation_ledger_invalid", f"{field} must be a bounded timestamp", path=field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AutomationLedgerError("automation_ledger_invalid", f"{field} is not an ISO timestamp", path=field) from exc
    if parsed.tzinfo is None:
        raise AutomationLedgerError("automation_ledger_invalid", f"{field} must include a timezone", path=field)
    return value


def _optional_text(value, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise AutomationLedgerError("automation_ledger_invalid", f"{field} is invalid or unbounded", path=field)
    return value


def _diagnostic(value) -> str | None:
    result = _optional_text(value, "$.diagnostic", MAX_DIAGNOSTIC)
    if result and not re.fullmatch(r"[a-z0-9_.-]+", result):
        raise AutomationLedgerError(
            "automation_ledger_invalid", "diagnostic must be a stable non-sensitive code", path="$.diagnostic",
        )
    return result


def _validate_generation(value: dict, *, expected: int) -> dict:
    path = f"$.generations[{expected}]"
    if isinstance(value, dict) and set(value) == CP3_GENERATION_FIELDS:
        value = {
            **value,
            "stage": "terminal" if value.get("state") in {"terminal", "blocked"} else (
                "run" if value.get("run_id") else "run_admission" if value.get("preallocated_run_id") else "detection"
            ),
            "validation_attempt_id": None,
            "publication_attempt_id": None,
            "completion_notified": False,
            "run_retry_count": value.get("retry_count", 0) if value.get("state") == "retry_delayed" else 0,
            "validation_retry_count": 0,
            "publication_retry_count": 0,
        }
    if not isinstance(value, dict) or set(value) != GENERATION_FIELDS:
        raise AutomationLedgerError("automation_ledger_invalid", "Automation generation has an invalid shape", path=path)
    generation = value["generation"]
    if isinstance(generation, bool) or generation != expected:
        raise AutomationLedgerError("automation_ledger_invalid", "Automation generations must be contiguous", path=f"{path}.generation")
    state = value["state"]
    policy = value["desired_policy"]
    terminal = value["terminal_classification"]
    retry_class = value["retry_class"]
    if state not in STATES or policy not in POLICIES or policy == "manual" or (terminal is not None and terminal not in TERMINAL_CLASSIFICATIONS) or retry_class not in RETRY_CLASSES:
        raise AutomationLedgerError("automation_ledger_invalid", "Automation generation contains an unsupported enum", path=path)
    retry_count = value["retry_count"]
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or not 0 <= retry_count <= 1000:
        raise AutomationLedgerError("automation_ledger_invalid", "retry_count is invalid", path=f"{path}.retry_count")
    for field in ("run_retry_count", "validation_retry_count", "publication_retry_count"):
        count = value[field]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
            raise AutomationLedgerError("automation_ledger_invalid", f"{field} is invalid", path=f"{path}.{field}")
    run_id = _optional_text(value["run_id"], f"{path}.run_id", 128)
    preallocated = _optional_text(value["preallocated_run_id"], f"{path}.preallocated_run_id", 128)
    validation_attempt_id = _optional_text(value["validation_attempt_id"], f"{path}.validation_attempt_id", 128)
    publication_attempt_id = _optional_text(value["publication_attempt_id"], f"{path}.publication_attempt_id", 128)
    for run_value, field in ((run_id, "run_id"), (preallocated, "preallocated_run_id")):
        if run_value and not re.fullmatch(r"[A-Za-z0-9_.+-]+", run_value):
            raise AutomationLedgerError("automation_ledger_invalid", f"{field} is unsafe", path=f"{path}.{field}")
    if run_id and run_id != preallocated:
        raise AutomationLedgerError("automation_ledger_invalid", "Linked Run must match the preallocated Run ID", path=f"{path}.run_id")
    if state in {"run_preallocated", "admitted"} and not preallocated:
        raise AutomationLedgerError("automation_ledger_invalid", "State requires a preallocated Run ID", path=path)
    if policy == "detect" and (preallocated or run_id or state in {"run_preallocated", "admitted"}):
        raise AutomationLedgerError("automation_ledger_invalid", "Detect-only policy cannot own a Run", path=path)
    if state == "admitted" and not run_id:
        raise AutomationLedgerError("automation_ledger_invalid", "Admitted state requires a linked Run", path=path)
    if state in {"terminal", "retry_delayed", "blocked"} and terminal is None:
        raise AutomationLedgerError("automation_ledger_invalid", "Terminal state requires a classification", path=path)
    if state == "retry_delayed" and terminal != "transient_retryable":
        raise AutomationLedgerError("automation_ledger_invalid", "retry_delayed requires transient classification", path=path)
    if state == "blocked" and terminal != "blocked_manual":
        raise AutomationLedgerError("automation_ledger_invalid", "blocked requires manual-action classification", path=path)
    if state == "blocked" and retry_class != "operator":
        raise AutomationLedgerError("automation_ledger_invalid", "blocked state requires operator retry classification", path=path)
    if state == "terminal" and terminal in {"transient_retryable", "blocked_manual"}:
        raise AutomationLedgerError("automation_ledger_invalid", "Terminal state/classification mismatch", path=path)
    if state == "terminal" and retry_class != "none":
        raise AutomationLedgerError("automation_ledger_invalid", "Terminal state cannot carry retry eligibility", path=path)
    if state not in {"terminal", "retry_delayed", "blocked"} and terminal is not None:
        raise AutomationLedgerError("automation_ledger_invalid", "Active state cannot be terminally classified", path=path)
    if state not in {"terminal", "retry_delayed", "blocked"} and retry_class != "none":
        raise AutomationLedgerError("automation_ledger_invalid", "Active state cannot carry retry eligibility", path=path)
    if state == "retry_delayed" and (retry_class != "transient" or value["not_before"] is None):
        raise AutomationLedgerError("automation_ledger_invalid", "Retry-delayed state requires transient retry metadata", path=path)
    if state != "retry_delayed" and value["not_before"] is not None:
        raise AutomationLedgerError("automation_ledger_invalid", "Only retry-delayed state can carry not_before", path=path)
    stage = value["stage"]
    if stage not in STAGES:
        raise AutomationLedgerError("automation_ledger_invalid", "Automation lifecycle stage is invalid", path=f"{path}.stage")
    if state in {"terminal", "blocked"} and stage != "terminal":
        raise AutomationLedgerError("automation_ledger_invalid", "Stopped automation must use the terminal stage", path=path)
    if state not in {"terminal", "blocked"} and stage == "terminal":
        raise AutomationLedgerError("automation_ledger_invalid", "Active automation cannot use the terminal stage", path=path)
    for attempt_id, field in ((validation_attempt_id, "validation_attempt_id"), (publication_attempt_id, "publication_attempt_id")):
        if attempt_id and not re.fullmatch(r"[A-Za-z0-9_.:+-]+", attempt_id):
            raise AutomationLedgerError("automation_ledger_invalid", f"{field} is unsafe", path=f"{path}.{field}")
    if publication_attempt_id and not validation_attempt_id:
        raise AutomationLedgerError("automation_ledger_invalid", "Publication linkage requires Validation linkage", path=path)
    if not isinstance(value["completion_notified"], bool):
        raise AutomationLedgerError("automation_ledger_invalid", "completion_notified must be boolean", path=path)
    if value["completion_notified"] and state not in {"terminal", "blocked"}:
        raise AutomationLedgerError("automation_ledger_invalid", "Only stopped automation can be notified", path=path)
    last_error_code = _optional_text(value["last_error_code"], f"{path}.last_error_code", MAX_ERROR_CODE)
    if last_error_code and not re.fullmatch(r"[a-z0-9_.-]+", last_error_code):
        raise AutomationLedgerError("automation_ledger_invalid", "last_error_code must be a stable code", path=f"{path}.last_error_code")
    return {
        "generation": generation,
        "state": state,
        "desired_policy": policy,
        "detected_at": _timestamp(value["detected_at"], f"{path}.detected_at"),
        "created_at": _timestamp(value["created_at"], f"{path}.created_at"),
        "updated_at": _timestamp(value["updated_at"], f"{path}.updated_at"),
        "preallocated_run_id": preallocated,
        "run_id": run_id,
        "terminal_classification": terminal,
        "retry_class": retry_class,
        "retry_count": retry_count,
        "not_before": _timestamp(value["not_before"], f"{path}.not_before", optional=True),
        "last_error_code": last_error_code,
        "diagnostic": _diagnostic(value["diagnostic"]),
        "stage": stage,
        "validation_attempt_id": validation_attempt_id,
        "publication_attempt_id": publication_attempt_id,
        "completion_notified": value["completion_notified"],
        "run_retry_count": value["run_retry_count"],
        "validation_retry_count": value["validation_retry_count"],
        "publication_retry_count": value["publication_retry_count"],
    }


def _empty_document() -> dict:
    return {"schema_version": LEDGER_SCHEMA_VERSION, "attempts": {}}


class AutomationLedger:
    """One bounded, process-safe automation coordination ledger.

    Attempt keys are durable deduplication tombstones.  CP1 never prunes them:
    this ledger cannot by itself prove that deletion would not re-admit work.
    Capacity exhaustion therefore fails automation closed while leaving manual
    operation and authoritative downstream stores untouched.
    """

    def __init__(self, data_dir: str | Path, *, max_attempts: int = MAX_ATTEMPTS, max_attempts_per_recipe: int = MAX_ATTEMPTS_PER_RECIPE):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / LEDGER_FILENAME
        self.lock_path = self.data_dir / LOCK_FILENAME
        self.max_attempts = min(max(1, int(max_attempts)), MAX_ATTEMPTS)
        self.max_attempts_per_recipe = min(max(1, int(max_attempts_per_recipe)), MAX_ATTEMPTS_PER_RECIPE)

    def _validate(self, document) -> dict:
        if not isinstance(document, dict) or set(document) != {"schema_version", "attempts"}:
            raise AutomationLedgerError("automation_ledger_malformed", "Automation ledger has an invalid root")
        version = document["schema_version"]
        if version != LEDGER_SCHEMA_VERSION or isinstance(version, bool):
            code = "automation_ledger_future_version" if isinstance(version, int) and version > LEDGER_SCHEMA_VERSION else "automation_ledger_malformed"
            raise AutomationLedgerError(code, "Unsupported automation ledger schema version", path="$.schema_version")
        attempts = document["attempts"]
        if not isinstance(attempts, dict) or len(attempts) > self.max_attempts:
            raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation attempt inventory exceeds its bound", path="$.attempts")
        normalized: dict[str, dict] = {}
        recipes: dict[str, int] = {}
        for key, entry in attempts.items():
            path = f"$.attempts.{key}"
            if not isinstance(key, str) or not re.fullmatch(r"automation-v1-[0-9a-f]{64}", key):
                raise AutomationLedgerError("automation_ledger_invalid", "Automation attempt key is invalid", path=path)
            if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS or entry.get("key") != key:
                raise AutomationLedgerError("automation_ledger_invalid", "Automation attempt has an invalid shape", path=path)
            recipe_id = entry["recipe_id"]
            recipe_sha = entry["recipe_sha256"]
            if not isinstance(recipe_id, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", recipe_id):
                raise AutomationLedgerError("automation_ledger_invalid", "Recipe ID is invalid", path=f"{path}.recipe_id")
            if not isinstance(recipe_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recipe_sha):
                raise AutomationLedgerError("automation_ledger_invalid", "Recipe SHA-256 is invalid", path=f"{path}.recipe_sha256")
            identity = normalize_upstream_identity(entry["upstream_identity"])
            if identity["completeness"] != "complete" or automation_attempt_key(recipe_id, identity, recipe_sha) != key:
                raise AutomationLedgerError("automation_ledger_invalid", "Attempt key does not match its exact inputs", path=path)
            generations = entry["generations"]
            if not isinstance(generations, list) or not 1 <= len(generations) <= MAX_GENERATIONS:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation generation inventory exceeds its bound", path=f"{path}.generations")
            recipes[recipe_id] = recipes.get(recipe_id, 0) + 1
            if recipes[recipe_id] > self.max_attempts_per_recipe:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe attempt inventory exceeds its bound", path="$.attempts")
            normalized[key] = {
                "key": key,
                "recipe_id": recipe_id,
                "recipe_sha256": recipe_sha,
                "upstream_identity": identity,
                "generations": [_validate_generation(row, expected=index) for index, row in enumerate(generations)],
            }
        if len(recipes) > MAX_RECIPES:
            raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe inventory exceeds its bound", path="$.attempts")
        return {"schema_version": LEDGER_SCHEMA_VERSION, "attempts": normalized}

    def _load_unlocked(self) -> dict:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return _empty_document()
        except OSError as exc:
            raise AutomationLedgerError("automation_ledger_unavailable", "Automation ledger cannot be read") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_LEDGER_BYTES:
                raise AutomationLedgerError("automation_ledger_malformed", "Automation ledger file is unsafe or unbounded")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                raw = handle.read(MAX_LEDGER_BYTES + 1)
        except (OSError, UnicodeError) as exc:
            raise AutomationLedgerError("automation_ledger_malformed", "Automation ledger cannot be decoded") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            document = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AutomationLedgerError("automation_ledger_malformed", "Automation ledger JSON is malformed") from exc
        return self._validate(document)

    def read(self) -> dict:
        """Read without repair, migration, directory creation, or hidden writes."""
        return copy.deepcopy(self._load_unlocked())

    @contextmanager
    def _locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with storage.locked_path(self.path):
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise AutomationLedgerError("automation_ledger_lock_invalid", "Automation ledger lock is unsafe") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise AutomationLedgerError("automation_ledger_lock_invalid", "Automation ledger lock is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _save_unlocked(self, document: dict) -> None:
        canonical = self._validate(document)
        encoded = json.dumps(canonical, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_LEDGER_BYTES:
            raise AutomationLedgerError(
                "automation_ledger_bounds_exceeded", "Automation ledger serialization exceeds its byte bound",
            )
        storage.atomic_write_text(self.path, encoded)
        self.path.chmod(0o600)

    @staticmethod
    def _generation(policy: str, detected_at: str, generation: int, *, state: str = "claimed") -> dict:
        now = utc_now()
        return {
            "generation": generation,
            "state": state,
            "desired_policy": policy,
            "detected_at": detected_at,
            "created_at": now,
            "updated_at": now,
            "preallocated_run_id": None,
            "run_id": None,
            "terminal_classification": None,
            "retry_class": "none",
            "retry_count": 0,
            "not_before": None,
            "last_error_code": None,
            "diagnostic": None,
            "stage": "detection",
            "validation_attempt_id": None,
            "publication_attempt_id": None,
            "completion_notified": False,
            "run_retry_count": 0,
            "validation_retry_count": 0,
            "publication_retry_count": 0,
        }

    @staticmethod
    def _new_entry(key: str, recipe_id: str, recipe_sha256: str, identity: dict, policy: str, detected_at: str, *, state: str) -> dict:
        return {
            "key": key,
            "recipe_id": recipe_id,
            "recipe_sha256": recipe_sha256.lower(),
            "upstream_identity": identity,
            "generations": [AutomationLedger._generation(policy, detected_at, 0, state=state)],
        }

    def record_detection(self, recipe_id: str, identity: dict, recipe_sha256: str, desired_policy: str, *, detected_at: str | None = None) -> ClaimResult:
        """Persist a complete detection without admitting or claiming work."""
        if desired_policy not in POLICIES or desired_policy == "manual":
            raise AutomationLedgerError("automation_policy_ineligible", "A manual/unsupported policy cannot be detected")
        normalized_identity = normalize_upstream_identity(identity)
        key = automation_attempt_key(recipe_id, normalized_identity, recipe_sha256)
        detected = _timestamp(detected_at or utc_now(), "$.detected_at")
        with self._locked():
            document = self._load_unlocked()
            existing = document["attempts"].get(key)
            if existing is not None:
                latest = existing["generations"][-1]
                if latest["desired_policy"] != desired_policy:
                    raise AutomationLedgerError("automation_policy_mismatch", "Attempt policy does not match its Recipe revision")
                return ClaimResult(False, key, latest["generation"], copy.deepcopy(existing))
            if len(document["attempts"]) >= self.max_attempts:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation ledger is full")
            if sum(entry["recipe_id"] == recipe_id for entry in document["attempts"].values()) >= self.max_attempts_per_recipe:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe automation history is full")
            entry = self._new_entry(
                key, recipe_id, recipe_sha256, normalized_identity, desired_policy, detected, state="detected",
            )
            document["attempts"][key] = entry
            self._save_unlocked(document)
            return ClaimResult(True, key, 0, copy.deepcopy(entry))

    def record_handled_detection(self, recipe_id: str, identity: dict, recipe_sha256: str, *, detected_at: str | None = None) -> ClaimResult:
        """Atomically persist a detect-only identity as durably handled."""
        normalized_identity = normalize_upstream_identity(identity)
        key = automation_attempt_key(recipe_id, normalized_identity, recipe_sha256)
        detected = _timestamp(detected_at or utc_now(), "$.detected_at")
        with self._locked():
            document = self._load_unlocked()
            existing = document["attempts"].get(key)
            if existing is not None:
                latest = existing["generations"][-1]
                if latest["desired_policy"] != "detect":
                    raise AutomationLedgerError("automation_policy_mismatch", "Attempt policy does not match its Recipe revision")
                return ClaimResult(False, key, latest["generation"], copy.deepcopy(existing))
            if len(document["attempts"]) >= self.max_attempts:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation ledger is full")
            if sum(entry["recipe_id"] == recipe_id for entry in document["attempts"].values()) >= self.max_attempts_per_recipe:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe automation history is full")
            entry = self._new_entry(
                key, recipe_id, recipe_sha256, normalized_identity, "detect", detected, state="detected",
            )
            entry["generations"][0].update({
                "state": "terminal",
                "terminal_classification": "success",
                "diagnostic": "detect_only_handled",
                "stage": "terminal",
                "updated_at": utc_now(),
            })
            document["attempts"][key] = entry
            self._save_unlocked(document)
            return ClaimResult(True, key, 0, copy.deepcopy(entry))

    def claim(self, recipe_id: str, identity: dict, recipe_sha256: str, desired_policy: str, *, detected_at: str | None = None) -> ClaimResult:
        if desired_policy not in POLICIES or desired_policy == "manual":
            raise AutomationLedgerError("automation_policy_ineligible", "A manual/unsupported policy cannot be claimed")
        normalized_identity = normalize_upstream_identity(identity)
        key = automation_attempt_key(recipe_id, normalized_identity, recipe_sha256)
        detected = _timestamp(detected_at or utc_now(), "$.detected_at")
        with self._locked():
            document = self._load_unlocked()
            existing = document["attempts"].get(key)
            if existing is not None:
                latest = existing["generations"][-1]
                if latest["desired_policy"] != desired_policy:
                    raise AutomationLedgerError("automation_policy_mismatch", "Claim policy does not match its Recipe revision")
                if latest["state"] == "detected":
                    latest.update({"state": "claimed", "updated_at": utc_now()})
                    self._save_unlocked(document)
                    return ClaimResult(True, key, latest["generation"], copy.deepcopy(existing))
                return ClaimResult(False, key, latest["generation"], copy.deepcopy(existing))
            if len(document["attempts"]) >= self.max_attempts:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation ledger is full")
            if sum(entry["recipe_id"] == recipe_id for entry in document["attempts"].values()) >= self.max_attempts_per_recipe:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe automation history is full")
            entry = self._new_entry(
                key, recipe_id, recipe_sha256, normalized_identity, desired_policy, detected, state="claimed",
            )
            document["attempts"][key] = entry
            self._save_unlocked(document)
            return ClaimResult(True, key, 0, copy.deepcopy(entry))

    def claim_current_recipe(
        self, recipe_path: str | Path, recipe_id: str, identity: dict,
        recipe_sha256: str, desired_policy: str, *, detect_only: bool = False,
        detected_at: str | None = None,
    ) -> ClaimResult:
        """CAS a detection against the live Recipe and ledger in one lease.

        Detection and all network access must have completed before entry.
        Holding the Recipe lease through the ledger write prevents an edit,
        disable, or policy change from landing between validation and claim.
        """
        if desired_policy not in POLICIES or desired_policy == "manual" or detect_only != (desired_policy == "detect"):
            raise AutomationLedgerError("automation_policy_ineligible", "Automation policy cannot be guarded and claimed")
        normalized_identity = normalize_upstream_identity(identity)
        key = automation_attempt_key(recipe_id, normalized_identity, recipe_sha256)
        detected = _timestamp(detected_at or utc_now(), "$.detected_at")
        with self._locked():
            try:
                with recipe_store.locked_recipe(Path(recipe_path)) as current:
                    automation = current.get("automation") if isinstance(current, dict) else None
                    if (
                        current.get("name") != recipe_id
                        or canonical_recipe_sha256(current) != recipe_sha256.lower()
                        or not current.get("active")
                        or not isinstance(automation, dict)
                        or not automation.get("enabled")
                        or automation.get("policy") != desired_policy
                    ):
                        raise AutomationLedgerError(
                            "recipe_changed_during_detection",
                            "Recipe changed or became ineligible during upstream detection",
                        )
                    document = self._load_unlocked()
                    existing = document["attempts"].get(key)
                    if existing is not None:
                        latest = existing["generations"][-1]
                        if latest["desired_policy"] != desired_policy:
                            raise AutomationLedgerError("automation_policy_mismatch", "Claim policy does not match its Recipe revision")
                        return ClaimResult(False, key, latest["generation"], copy.deepcopy(existing))
                    if len(document["attempts"]) >= self.max_attempts:
                        raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation ledger is full")
                    if sum(entry["recipe_id"] == recipe_id for entry in document["attempts"].values()) >= self.max_attempts_per_recipe:
                        raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Recipe automation history is full")
                    state = "detected" if detect_only else "claimed"
                    entry = self._new_entry(key, recipe_id, recipe_sha256, normalized_identity, desired_policy, detected, state=state)
                    if detect_only:
                        entry["generations"][0].update({
                            "state": "terminal", "terminal_classification": "success",
                            "diagnostic": "detect_only_handled", "updated_at": utc_now(),
                            "stage": "terminal",
                        })
                    document["attempts"][key] = entry
                    self._save_unlocked(document)
                    return ClaimResult(True, key, 0, copy.deepcopy(entry))
            except recipe_store.RecipeStoreError as exc:
                raise AutomationLedgerError(
                    "recipe_changed_during_detection", "Recipe became unavailable during upstream detection",
                ) from exc

    def explicit_retry(
        self,
        attempt_key: str,
        desired_policy: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> ClaimResult:
        with self._locked():
            document = self._load_unlocked()
            entry = document["attempts"].get(attempt_key)
            if entry is None:
                raise AutomationLedgerError("automation_attempt_not_found", "Automation attempt was not found")
            latest = entry["generations"][-1]
            if expected_generation is not None:
                if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
                    raise AutomationLedgerError("automation_retry_stale", "Automation retry generation is stale")
                if latest["generation"] == expected_generation + 1:
                    return ClaimResult(False, attempt_key, latest["generation"], copy.deepcopy(entry))
                if latest["generation"] != expected_generation:
                    raise AutomationLedgerError("automation_retry_stale", "Automation retry generation is stale")
            if latest["state"] not in {"terminal", "retry_delayed", "blocked"}:
                raise AutomationLedgerError("automation_retry_not_terminal", "Only a stopped generation can be explicitly retried")
            if len(entry["generations"]) >= MAX_GENERATIONS:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation retry generation limit reached")
            policy = desired_policy or latest["desired_policy"]
            if policy not in POLICIES or policy == "manual":
                raise AutomationLedgerError("automation_policy_ineligible", "Retry policy is ineligible")
            if policy != latest["desired_policy"]:
                raise AutomationLedgerError("automation_policy_mismatch", "Retry cannot change the captured Recipe policy")
            generation = len(entry["generations"])
            entry["generations"].append(self._generation(policy, latest["detected_at"], generation))
            self._save_unlocked(document)
            return ClaimResult(True, attempt_key, generation, copy.deepcopy(entry))

    def explicit_retry_current_recipe(
        self,
        recipe_path: str | Path,
        recipe_id: str,
        attempt_key: str,
        expected_generation: int,
        expected_updated_at: str | None = None,
    ) -> ClaimResult:
        """Create or reuse one operator generation under immutable admission.

        The ledger and current Recipe are checked under the established
        ledger-before-Recipe lock order.  A concurrent duplicate request
        observes and reuses the one generation it raced with.
        """
        with self._locked():
            document = self._load_unlocked()
            entry = document["attempts"].get(attempt_key)
            if entry is None or entry.get("recipe_id") != recipe_id:
                raise AutomationLedgerError("automation_attempt_not_found", "Automation attempt was not found")
            candidates = [
                candidate for candidate in document["attempts"].values()
                if candidate["recipe_id"] == recipe_id
            ]
            newest = max(
                candidates,
                key=lambda candidate: (
                    candidate["generations"][0]["detected_at"],
                    candidate["generations"][0]["created_at"],
                    candidate["key"],
                ),
            )
            if newest["key"] != attempt_key:
                raise AutomationLedgerError("automation_retry_stale", "A newer upstream attempt is available")
            latest = entry["generations"][-1]
            if (
                isinstance(expected_generation, bool)
                or not isinstance(expected_generation, int)
                or expected_generation < 0
            ):
                raise AutomationLedgerError("automation_retry_stale", "Automation retry generation is stale")
            duplicate = latest["generation"] == expected_generation + 1
            if not duplicate and latest["generation"] != expected_generation:
                raise AutomationLedgerError("automation_retry_stale", "Automation retry generation is stale")
            observed = entry["generations"][expected_generation] if duplicate else latest
            if expected_updated_at is not None and observed.get("updated_at") != expected_updated_at:
                raise AutomationLedgerError("automation_retry_stale", "Automation retry status is stale")
            if not duplicate and latest["state"] not in {"terminal", "retry_delayed", "blocked"}:
                raise AutomationLedgerError("automation_retry_not_terminal", "Only a stopped generation can be explicitly retried")
            if not duplicate and len(entry["generations"]) >= MAX_GENERATIONS:
                raise AutomationLedgerError("automation_ledger_bounds_exceeded", "Automation retry generation limit reached")
            try:
                with recipe_store.locked_recipe(Path(recipe_path)) as current:
                    automation = current.get("automation") if isinstance(current, dict) else None
                    if current.get("name") != recipe_id:
                        raise AutomationLedgerError("automation_recipe_stale", "Recipe identity changed")
                    if (
                        canonical_recipe_sha256(current) != entry["recipe_sha256"]
                        or not current.get("active")
                        or not isinstance(automation, dict)
                        or not automation.get("enabled")
                        or automation.get("policy") != latest["desired_policy"]
                        or automation.get("policy") == "manual"
                    ):
                        raise AutomationLedgerError(
                            "automation_recipe_stale",
                            "Recipe changed or is no longer eligible for this retry",
                        )
                    if duplicate:
                        return ClaimResult(False, attempt_key, latest["generation"], copy.deepcopy(entry))
                    generation = len(entry["generations"])
                    entry["generations"].append(
                        self._generation(latest["desired_policy"], latest["detected_at"], generation)
                    )
                    self._save_unlocked(document)
                    return ClaimResult(True, attempt_key, generation, copy.deepcopy(entry))
            except recipe_store.RecipeStoreError as exc:
                raise AutomationLedgerError(
                    "automation_recipe_stale", "Recipe is unavailable for this retry",
                ) from exc

    def preallocate_run(self, attempt_key: str, generation: int) -> dict:
        candidate = BuildStore.allocate_run_id()
        with self._locked():
            document = self._load_unlocked()
            row = self._row(document, attempt_key, generation)
            if row["preallocated_run_id"]:
                return copy.deepcopy(row)
            if row["state"] not in {"claimed", "detected"}:
                raise AutomationLedgerError("automation_state_conflict", "Generation cannot preallocate a Run from its current state")
            if row["desired_policy"] == "detect":
                raise AutomationLedgerError("automation_policy_ineligible", "Detect-only policy cannot preallocate a Run")
            row.update({
                "preallocated_run_id": candidate, "state": "run_preallocated",
                "stage": "run_admission", "updated_at": utc_now(),
            })
            self._save_unlocked(document)
            return copy.deepcopy(row)

    @staticmethod
    def _verify_run_binding(entry: dict, row: dict, run: dict | None) -> None:
        automation = (run or {}).get("automation")
        if not run:
            raise AutomationLedgerError("automation_run_not_found", "Preallocated Run does not exist")
        if (
            run.get("id") != row["preallocated_run_id"]
            or run.get("recipe_id") != entry["recipe_id"]
            or run.get("recipe_sha256") != entry["recipe_sha256"]
            or not isinstance(automation, dict)
            or automation.get("attempt_key") != entry["key"]
            or automation.get("generation") != row["generation"]
            or automation.get("policy") != row["desired_policy"]
            or automation.get("expected_upstream_identity") != entry["upstream_identity"]
        ):
            raise AutomationLedgerError("automation_run_binding_invalid", "Run does not match its durable automation claim")

    def link_run(self, attempt_key: str, generation: int, run_id: str, *, store: BuildStore | None = None) -> dict:
        selected_store = store or BuildStore(self.data_dir / "builds")
        with self._locked():
            document = self._load_unlocked()
            row = self._row(document, attempt_key, generation)
            if row["run_id"]:
                if row["run_id"] != run_id:
                    raise AutomationLedgerError("automation_run_mismatch", "Generation is already linked to another Run")
                return copy.deepcopy(row)
            if not row["preallocated_run_id"] or row["preallocated_run_id"] != run_id:
                raise AutomationLedgerError("automation_run_mismatch", "Run does not match the durable preallocation")
            entry = document["attempts"][attempt_key]
            try:
                run = selected_store.load(run_id)
            except (OSError, TypeError, ValueError) as exc:
                raise AutomationLedgerError("automation_run_binding_invalid", "Preallocated Run cannot be verified") from exc
            self._verify_run_binding(entry, row, run)
            row.update({
                "run_id": run_id,
                "state": "admitted",
                "stage": "run",
                "terminal_classification": None,
                "retry_class": "none",
                "not_before": None,
                "last_error_code": None,
                "diagnostic": None,
                "updated_at": utc_now(),
            })
            self._save_unlocked(document)
            return copy.deepcopy(row)

    def reconcile_preallocated_run(self, attempt_key: str, generation: int, *, store: BuildStore | None = None) -> dict:
        """Recover a preallocated Run during restart, before creators start.

        This explicit writable recovery point must run while the lifecycle is
        quiescent.  It can remove only a workspace with no committed run.json;
        normal reads never invoke it.
        """
        selected_store = store or BuildStore(self.data_dir / "builds")
        document = self.read()
        row = self._row(document, attempt_key, generation)
        if row["run_id"]:
            return {"linked": True, "recovered_incomplete": False, "record": copy.deepcopy(row)}
        run_id = row["preallocated_run_id"]
        if not run_id:
            return {"linked": False, "recovered_incomplete": False, "record": copy.deepcopy(row)}
        try:
            run = selected_store.load(run_id)
        except (OSError, TypeError, ValueError) as exc:
            raise AutomationLedgerError("automation_run_binding_invalid", "Preallocated Run cannot be verified") from exc
        if run is None and selected_store.run_dir(run_id).exists():
            if not selected_store.discard_incomplete_creation(run_id):
                raise AutomationLedgerError(
                    "automation_run_binding_invalid", "Incomplete preallocated Run cannot be discarded",
                )
            return {"linked": False, "recovered_incomplete": True, "record": copy.deepcopy(row)}
        if run is None:
            return {"linked": False, "recovered_incomplete": False, "record": copy.deepcopy(row)}
        return {
            "linked": True,
            "recovered_incomplete": False,
            "record": self.link_run(attempt_key, generation, run_id, store=selected_store),
        }

    def finish(self, attempt_key: str, generation: int, classification: str, *, retry_class: str = "none", retry_count: int = 0, not_before: str | None = None, last_error_code: str | None = None, diagnostic: str | None = None) -> dict:
        if classification not in TERMINAL_CLASSIFICATIONS or retry_class not in RETRY_CLASSES:
            raise AutomationLedgerError("automation_classification_invalid", "Unsupported automation terminal/retry classification")
        if classification == "transient_retryable":
            state = "retry_delayed"
        elif classification == "blocked_manual":
            state = "blocked"
        else:
            state = "terminal"
        with self._locked():
            document = self._load_unlocked()
            row = self._row(document, attempt_key, generation)
            previous_stage = row["stage"]
            row.update({
                "state": state,
                "stage": "terminal" if state in {"terminal", "blocked"} else row["stage"],
                "terminal_classification": classification,
                "retry_class": retry_class,
                "retry_count": retry_count,
                "not_before": not_before,
                "last_error_code": last_error_code,
                "diagnostic": diagnostic,
                "updated_at": utc_now(),
            })
            if classification == "transient_retryable":
                field = {
                    "run_admission": "run_retry_count",
                    "validation_admission": "validation_retry_count",
                    "validation": "validation_retry_count",
                    "publication": "publication_retry_count",
                }.get(previous_stage)
                if field:
                    row[field] = retry_count
            self._save_unlocked(document)
            return copy.deepcopy(row)

    def advance_lifecycle(
        self,
        attempt_key: str,
        generation: int,
        stage: str,
        *,
        validation_attempt_id: str | None = None,
        publication_attempt_id: str | None = None,
    ) -> dict:
        """Persist only orchestration links; canonical stores own stage outcomes."""
        if stage not in STAGES or stage in {"detection", "terminal"}:
            raise AutomationLedgerError("automation_stage_invalid", "Unsupported active automation stage")
        with self._locked():
            document = self._load_unlocked()
            row = self._row(document, attempt_key, generation)
            if row["state"] in {"terminal", "blocked"}:
                return copy.deepcopy(row)
            if not row["run_id"]:
                raise AutomationLedgerError("automation_run_not_linked", "Lifecycle cannot advance before Run linkage")
            if validation_attempt_id is not None:
                current = row["validation_attempt_id"]
                if current and current != validation_attempt_id and stage not in {"validation_admission", "validation"}:
                    raise AutomationLedgerError("automation_validation_mismatch", "Validation linkage cannot change at this stage")
                row["validation_attempt_id"] = validation_attempt_id
            if publication_attempt_id is not None:
                current = row["publication_attempt_id"]
                row["publication_attempt_id"] = publication_attempt_id
            row.update({
                "state": "admitted", "stage": stage, "terminal_classification": None,
                "retry_class": "none", "not_before": None, "last_error_code": None,
                "diagnostic": None, "updated_at": utc_now(),
            })
            self._save_unlocked(document)
            return copy.deepcopy(row)

    def mark_completion_notified(self, attempt_key: str, generation: int) -> dict:
        with self._locked():
            document = self._load_unlocked()
            row = self._row(document, attempt_key, generation)
            if row["state"] not in {"terminal", "blocked"}:
                raise AutomationLedgerError("automation_not_terminal", "Automation completion is not terminal")
            if not row["completion_notified"]:
                row.update({"completion_notified": True, "updated_at": utc_now()})
                self._save_unlocked(document)
            return copy.deepcopy(row)

    def may_create_run(self, attempt_key: str, generation: int | None = None) -> bool:
        document = self.read()
        entry = document["attempts"].get(attempt_key)
        if not entry:
            return False
        index = len(entry["generations"]) - 1 if generation is None else generation
        if not 0 <= index < len(entry["generations"]):
            return False
        row = entry["generations"][index]
        if row["desired_policy"] == "detect" or row["state"] not in {"claimed", "run_preallocated"} or row["run_id"] is not None:
            return False
        if row["state"] == "run_preallocated" and BuildStore(self.data_dir / "builds").run_dir(row["preallocated_run_id"]).exists():
            return False
        return True

    @staticmethod
    def _row(document: dict, attempt_key: str, generation: int) -> dict:
        entry = document["attempts"].get(attempt_key)
        if entry is None or isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation < len(entry["generations"]):
            raise AutomationLedgerError("automation_attempt_not_found", "Automation generation was not found")
        return entry["generations"][generation]
