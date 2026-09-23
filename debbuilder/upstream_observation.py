"""Bounded advisory upstream observations for Recipe projections."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import stat
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import recipe_store, storage
from .build_store import canonical_recipe_sha256
from .recipe_schema import require_safe_name, runtime_recipe_for_storage, validate_recipe_metadata


SCHEMA_VERSION = 1
STATE_FILENAME = "upstream-observations.json"
MAX_STATE_BYTES = 1024 * 1024
MAX_RECIPES = 1000
MAX_IN_FLIGHT = 64
MAX_DISPLAY_LENGTH = 200
SAFE_CLASSIFICATIONS = frozenset({
    "detected", "no_change", "rate_limited", "upstream_unavailable", "source_not_found",
    "explicit_asset_not_found", "ambiguous_asset", "unsupported_source", "incomplete_identity",
    "invalid_configuration", "manual_action_required", "recipe_changed", "capacity_exhausted",
    "automation_blocked", "shutting_down",
})
SAFE_DIAGNOSTICS = frozenset({
    "github_rate_limited", "github_unavailable", "github_api_error", "repository_not_found",
    "release_not_found", "release_asset_not_found", "ambiguous_release_asset",
    "ambiguous_archive_source", "unsupported_artifact_tracking", "incomplete_upstream_identity",
    "invalid_upstream_identity", "invalid_release_version", "recipe_changed_during_detection",
    "invalid_automation_configuration", "manual_action_required", "automation_ledger_bounds_exceeded",
    "automation_ledger_malformed", "automation_ledger_future_version", "automation_ledger_unavailable",
    "application_shutting_down", "observation_failed",
})
LOGGER = logging.getLogger(__name__)


class UpstreamObservationError(RuntimeError):
    """Stable failure for advisory persistence and coordination."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def _timestamp(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise UpstreamObservationError("observation_state_malformed", "Observation timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UpstreamObservationError("observation_state_malformed", "Observation timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise UpstreamObservationError("observation_state_malformed", "Observation timestamp lacks timezone")
    return value


def _bounded_display(value) -> str:
    text = str(value or "")
    if len(text) > MAX_DISPLAY_LENGTH or "://" in text or any(ord(char) < 32 for char in text):
        raise UpstreamObservationError("observation_state_malformed", "Observation display value is malformed")
    return text


class UpstreamObservationStore:
    """Versioned, fail-soft durable state containing display-only observations."""

    def __init__(self, data_dir: str | Path, *, wall_clock: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STATE_FILENAME
        self.wall_clock = wall_clock

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": SCHEMA_VERSION, "recipes": {}}

    def _validate(self, value) -> dict:
        if not isinstance(value, dict) or set(value) != {"schema_version", "recipes"}:
            raise UpstreamObservationError("observation_state_malformed", "Observation state root is malformed")
        version = value["schema_version"]
        if version != SCHEMA_VERSION or isinstance(version, bool):
            code = "observation_state_future_version" if isinstance(version, int) and version > SCHEMA_VERSION else "observation_state_malformed"
            raise UpstreamObservationError(code, "Observation state version is unsupported")
        recipes = value["recipes"]
        if not isinstance(recipes, dict) or len(recipes) > MAX_RECIPES:
            raise UpstreamObservationError("observation_state_capacity", "Observation Recipe state exceeds its bound")
        normalized = {}
        for recipe_id, row in recipes.items():
            require_safe_name(recipe_id, "Recipe ID")
            if not isinstance(row, dict) or set(row) != {"recipe_sha256", "last_success", "last_attempt"}:
                raise UpstreamObservationError("observation_state_malformed", "Observation Recipe row is malformed")
            recipe_sha = row["recipe_sha256"]
            if not isinstance(recipe_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recipe_sha):
                raise UpstreamObservationError("observation_state_malformed", "Observation Recipe hash is malformed")
            success = row["last_success"]
            if success is not None:
                if not isinstance(success, dict) or set(success) != {"observed_at", "display_version", "display_ref"}:
                    raise UpstreamObservationError("observation_state_malformed", "Observation success is malformed")
                success = {
                    "observed_at": _timestamp(success["observed_at"]),
                    "display_version": _bounded_display(success["display_version"]),
                    "display_ref": _bounded_display(success["display_ref"]),
                }
            attempt = row["last_attempt"]
            if not isinstance(attempt, dict) or set(attempt) != {"attempted_at", "status", "classification", "diagnostic_code"}:
                raise UpstreamObservationError("observation_state_malformed", "Observation attempt is malformed")
            if attempt["status"] not in {"success", "failed"}:
                raise UpstreamObservationError("observation_state_malformed", "Observation attempt status is malformed")
            classification = attempt["classification"]
            diagnostic = attempt["diagnostic_code"]
            if classification not in SAFE_CLASSIFICATIONS:
                raise UpstreamObservationError("observation_state_malformed", "Observation classification is malformed")
            if diagnostic is not None and diagnostic not in SAFE_DIAGNOSTICS:
                raise UpstreamObservationError("observation_state_malformed", "Observation diagnostic is malformed")
            if attempt["status"] == "success" and (classification != "detected" or diagnostic is not None or success is None):
                raise UpstreamObservationError("observation_state_malformed", "Observation success attempt is inconsistent")
            normalized[recipe_id] = {
                "recipe_sha256": recipe_sha,
                "last_success": success,
                "last_attempt": {
                    "attempted_at": _timestamp(attempt["attempted_at"]),
                    "status": attempt["status"],
                    "classification": classification,
                    "diagnostic_code": diagnostic,
                },
            }
        return {"schema_version": SCHEMA_VERSION, "recipes": normalized}

    def _load(self) -> dict:
        descriptor = -1
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise UpstreamObservationError("observation_state_unavailable", "Observation state cannot be read") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_BYTES:
                raise UpstreamObservationError("observation_state_malformed", "Observation state is unsafe or unbounded")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                raw = handle.read(MAX_STATE_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
                raise UpstreamObservationError("observation_state_malformed", "Observation state is unbounded")
            return self._validate(json.loads(raw))
        except UpstreamObservationError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise UpstreamObservationError("observation_state_malformed", "Observation state cannot be decoded") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def read(self) -> dict:
        """Return state without repairing malformed/future documents."""
        return copy.deepcopy(self._load())

    def projection(self, recipe_id: str, recipe_sha256: str) -> dict | None:
        try:
            row = self._load()["recipes"].get(recipe_id)
        except (UpstreamObservationError, ValueError):
            return None
        if not row or row["recipe_sha256"] != recipe_sha256:
            return None
        return copy.deepcopy(row)

    def _save(self, document: dict) -> None:
        canonical = self._validate(document)
        encoded = json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise UpstreamObservationError("observation_state_capacity", "Observation state exceeds its byte bound")
        try:
            storage.atomic_write_text(self.path, encoded)
            self.path.chmod(0o600)
        except OSError as exc:
            raise UpstreamObservationError("observation_state_unavailable", "Observation state cannot be saved") from exc

    def record_success(self, recipe_id: str, recipe_sha256: str, result: dict) -> dict:
        require_safe_name(recipe_id, "Recipe ID")
        now = _utc_now(self.wall_clock)
        row = {
            "recipe_sha256": recipe_sha256,
            "last_success": {
                "observed_at": now,
                "display_version": _bounded_display(result.get("display_version")),
                "display_ref": _bounded_display(result.get("display_ref")),
            },
            "last_attempt": {
                "attempted_at": now, "status": "success", "classification": "detected", "diagnostic_code": None,
            },
        }
        with storage.locked_path(self.path):
            document = self._load()
            if recipe_id not in document["recipes"] and len(document["recipes"]) >= MAX_RECIPES:
                raise UpstreamObservationError("observation_state_capacity", "Observation Recipe state is full")
            document["recipes"][recipe_id] = row
            self._save(document)
        return copy.deepcopy(row)

    def record_failure(self, recipe_id: str, recipe_sha256: str, *, classification: str, diagnostic_code: str) -> dict:
        require_safe_name(recipe_id, "Recipe ID")
        classification = classification if classification in SAFE_CLASSIFICATIONS else "upstream_unavailable"
        diagnostic_code = diagnostic_code if diagnostic_code in SAFE_DIAGNOSTICS else "observation_failed"
        now = _utc_now(self.wall_clock)
        with storage.locked_path(self.path):
            document = self._load()
            previous = document["recipes"].get(recipe_id)
            last_success = previous.get("last_success") if previous and previous["recipe_sha256"] == recipe_sha256 else None
            if recipe_id not in document["recipes"] and len(document["recipes"]) >= MAX_RECIPES:
                raise UpstreamObservationError("observation_state_capacity", "Observation Recipe state is full")
            row = {
                "recipe_sha256": recipe_sha256,
                "last_success": last_success,
                "last_attempt": {
                    "attempted_at": now, "status": "failed", "classification": classification,
                    "diagnostic_code": diagnostic_code,
                },
            }
            document["recipes"][recipe_id] = row
            self._save(document)
        return copy.deepcopy(row)


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: BaseException | None = None


class UpstreamObservationService:
    """Fresh exact resolution with bounded per-Recipe single-flight ownership."""

    def __init__(
        self,
        store: UpstreamObservationStore,
        *,
        resolver: Callable[..., dict],
        max_in_flight: int = MAX_IN_FLIGHT,
    ):
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or not 1 <= max_in_flight <= MAX_IN_FLIGHT:
            raise ValueError("observation in-flight bound is invalid")
        self.store = store
        self.resolver = resolver
        self.max_in_flight = max_in_flight
        self._lock = threading.Lock()
        self._in_flight: dict[tuple[str, str], _Flight] = {}

    def projection(self, recipe_id: str, recipe: dict) -> dict | None:
        canonical = validate_recipe_metadata(runtime_recipe_for_storage(recipe))
        return self.store.projection(recipe_id, canonical_recipe_sha256(canonical))

    def observe(
        self,
        recipe_id: str,
        recipe: dict,
        recipe_path: str | Path,
        *,
        token: str = "",
        resolver: Callable[..., dict] | None = None,
        mutation_lease=None,
    ) -> dict:
        canonical = validate_recipe_metadata(runtime_recipe_for_storage(recipe))
        recipe_sha = canonical_recipe_sha256(canonical)
        key = (recipe_id, recipe_sha)
        with self._lock:
            flight = self._in_flight.get(key)
            leader = flight is None
            if leader:
                if len(self._in_flight) >= self.max_in_flight:
                    raise UpstreamObservationError("observation_capacity_exhausted", "Observation refresh capacity is exhausted")
                flight = _Flight()
                self._in_flight[key] = flight
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.result)

        try:
            try:
                result = (resolver or self.resolver)(runtime_recipe_for_storage(canonical), token=token)
            except Exception as exc:
                try:
                    with recipe_store.locked_recipe(Path(recipe_path)) as current:
                        if canonical_recipe_sha256(current) != recipe_sha:
                            raise UpstreamObservationError(
                                "recipe_changed_during_detection",
                                "Recipe changed during upstream observation",
                            )
                        lease = mutation_lease() if mutation_lease is not None else nullcontext()
                        with lease:
                            self.store.record_failure(
                                recipe_id, recipe_sha,
                                classification=str(getattr(exc, "classification", "upstream_unavailable")),
                                diagnostic_code=str(getattr(exc, "code", "observation_failed")),
                            )
                except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError, UpstreamObservationError, RuntimeError):
                    pass
                raise

            persisted = True
            persistence_error = None
            try:
                with recipe_store.locked_recipe(Path(recipe_path)) as current:
                    if canonical_recipe_sha256(current) != recipe_sha:
                        raise UpstreamObservationError(
                            "recipe_changed_during_detection",
                            "Recipe changed during upstream observation",
                        )
                    lease = mutation_lease() if mutation_lease is not None else nullcontext()
                    with lease:
                        observation = self.store.record_success(recipe_id, recipe_sha, result)
            except recipe_store.RecipeStoreError as exc:
                raise UpstreamObservationError(
                    "recipe_changed_during_detection", "Recipe changed during upstream observation",
                ) from exc
            except (UpstreamObservationError, RuntimeError) as exc:
                if getattr(exc, "code", None) == "recipe_changed_during_detection":
                    raise
                persisted = False
                observation = None
                persistence_error = str(getattr(exc, "code", "observation_state_unavailable"))[:80]
                LOGGER.warning("Advisory upstream observation could not be persisted (%s)", persistence_error)
            flight.result = {
                **result,
                "recipe_id": recipe_id,
                "recipe_sha256": recipe_sha,
                "observation": observation,
                "observation_persisted": persisted,
                "observation_persistence_error": persistence_error,
            }
            return copy.deepcopy(flight.result)
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            flight.done.set()
            with self._lock:
                self._in_flight.pop(key, None)
