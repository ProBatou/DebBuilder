"""Application-owned periodic automation detection and retry scheduling."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import recipe_store, storage
from .automation_ledger import AutomationLedger, AutomationLedgerError, MAX_RECIPES
from .build_store import canonical_recipe_sha256
from .lifecycle import MutationGate, MutationGateClosed
from .recipe_schema import BUILTIN_RECIPE_ID, require_safe_name, validate_recipe_metadata

LOGGER = logging.getLogger(__name__)
STATE_SCHEMA_VERSION = 1
STATE_FILENAME = "automation-scheduler.json"
STATE_LOCK_FILENAME = ".automation-scheduler.lock"
MAX_STATE_BYTES = 1024 * 1024
MAX_RETRY_COUNT = 8
MIN_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 6 * 60 * 60
TRANSIENT_CLASSIFICATIONS = {"upstream_unavailable", "rate_limited"}
SOURCE_ABSENT_CLASSIFICATIONS = {"source_not_found"}


class AutomationSchedulerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def _timestamp(value, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler timestamp lacks timezone")
    return value


class AutomationRetryStore:
    """Small process-safe state for failures that have no attempt identity."""

    def __init__(self, data_dir: str | Path, *, wall_clock: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STATE_FILENAME
        self.lock_path = self.data_dir / STATE_LOCK_FILENAME
        self.wall_clock = wall_clock

    def _validate(self, value) -> dict:
        if not isinstance(value, dict) or set(value) != {"schema_version", "recipes"}:
            raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler state root is malformed")
        version = value["schema_version"]
        if version != STATE_SCHEMA_VERSION or isinstance(version, bool):
            code = "automation_scheduler_state_future_version" if isinstance(version, int) and version > STATE_SCHEMA_VERSION else "automation_scheduler_state_malformed"
            raise AutomationSchedulerError(code, "Scheduler state version is unsupported")
        rows = value["recipes"]
        if not isinstance(rows, dict) or len(rows) > MAX_RECIPES:
            raise AutomationSchedulerError("automation_scheduler_state_capacity", "Scheduler retry state exceeds its bound")
        normalized = {}
        required = {"recipe_id", "recipe_sha256", "retry_class", "classification", "diagnostic", "retry_count", "not_before", "updated_at"}
        for recipe_id, row in rows.items():
            if not isinstance(recipe_id, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", recipe_id):
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler Recipe ID is malformed")
            if not isinstance(row, dict) or set(row) != required or row.get("recipe_id") != recipe_id:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry row is malformed")
            recipe_sha = row["recipe_sha256"]
            retry_count = row["retry_count"]
            if not isinstance(recipe_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recipe_sha):
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler Recipe SHA is malformed")
            if row["retry_class"] not in {"transient", "source_absent", "operator"}:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry class is malformed")
            if isinstance(retry_count, bool) or not isinstance(retry_count, int) or not 1 <= retry_count <= MAX_RETRY_COUNT:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry count is malformed")
            for field in ("classification", "diagnostic"):
                text = row[field]
                if not isinstance(text, str) or not re.fullmatch(r"[a-z0-9_.-]{1,80}", text):
                    raise AutomationSchedulerError("automation_scheduler_state_malformed", f"Scheduler {field} is malformed")
            not_before = _timestamp(row["not_before"], optional=True)
            if row["retry_class"] != "operator" and not_before is None:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Delayed retry lacks not_before")
            if row["retry_class"] == "operator" and not_before is not None:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Operator retry cannot carry not_before")
            normalized[recipe_id] = {**row, "not_before": not_before, "updated_at": _timestamp(row["updated_at"])}
        return {"schema_version": STATE_SCHEMA_VERSION, "recipes": normalized}

    def _load_unlocked(self) -> dict:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return {"schema_version": STATE_SCHEMA_VERSION, "recipes": {}}
        except OSError as exc:
            raise AutomationSchedulerError("automation_scheduler_state_unavailable", "Scheduler retry state cannot be read") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_BYTES:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry state is unsafe or unbounded")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                raw = handle.read(MAX_STATE_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
                raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry state is unbounded")
            return self._validate(json.loads(raw))
        except AutomationSchedulerError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise AutomationSchedulerError("automation_scheduler_state_malformed", "Scheduler retry state cannot be decoded") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @contextmanager
    def _locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with storage.locked_path(self.path):
            try:
                descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except OSError as exc:
                raise AutomationSchedulerError("automation_scheduler_state_unavailable", "Scheduler state lock is unavailable") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise AutomationSchedulerError("automation_scheduler_state_unavailable", "Scheduler state lock is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def read(self) -> dict:
        return copy.deepcopy(self._load_unlocked())

    def _save_unlocked(self, document: dict) -> None:
        canonical = self._validate(document)
        encoded = json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise AutomationSchedulerError("automation_scheduler_state_capacity", "Scheduler retry state exceeds its byte bound")
        try:
            storage.atomic_write_text(self.path, encoded)
            self.path.chmod(0o600)
        except OSError as exc:
            raise AutomationSchedulerError("automation_scheduler_state_unavailable", "Scheduler retry state cannot be saved") from exc

    def record(self, row: dict) -> None:
        canonical = self._validate({"schema_version": STATE_SCHEMA_VERSION, "recipes": {row["recipe_id"]: row}})["recipes"][row["recipe_id"]]
        with self._locked():
            document = self._load_unlocked()
            existing = document["recipes"].get(row["recipe_id"])
            if existing and {k: v for k, v in existing.items() if k != "updated_at"} == {k: v for k, v in canonical.items() if k != "updated_at"}:
                return
            if row["recipe_id"] not in document["recipes"] and len(document["recipes"]) >= MAX_RECIPES:
                raise AutomationSchedulerError("automation_scheduler_state_capacity", "Scheduler retry state is full")
            document["recipes"][row["recipe_id"]] = canonical
            self._save_unlocked(document)

    def clear(self, recipe_id: str) -> None:
        with self._locked():
            document = self._load_unlocked()
            if recipe_id not in document["recipes"]:
                return
            del document["recipes"][recipe_id]
            self._save_unlocked(document)


class AutomationScheduler:
    """One periodic owner for bounded CP2 detection and orchestration handoff."""

    def __init__(
        self, recipe_directory: str | Path, detection_service, ledger: AutomationLedger,
        retry_store: AutomationRetryStore, *, mutation_gate: MutationGate | None = None,
        admission_open: Callable[[], bool] = lambda: True,
        token_provider: Callable[[], str] = lambda: "", interval_seconds: int = 3600,
        concurrency: int = 4, enabled: bool = True,
        orchestrator=None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or not 60 <= interval_seconds <= 86400:
            raise ValueError("automation scheduler interval is outside supported bounds")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 8:
            raise ValueError("automation scheduler concurrency is outside supported bounds")
        self.recipe_directory = Path(recipe_directory)
        self.detection_service = detection_service
        self.ledger = ledger
        self.retry_store = retry_store
        self.mutation_gate = mutation_gate
        self.admission_open = admission_open
        self.token_provider = token_provider
        self.interval_seconds = interval_seconds
        self.concurrency = concurrency
        self.enabled = bool(enabled)
        self.orchestrator = orchestrator
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self._condition = threading.Condition()
        self._admission_gate = MutationGate()
        # Durable lifecycle reconciliation is independent from whether new
        # upstream network checks are enabled.
        self._wake_requested = self.enabled or self.orchestrator is not None
        self._stop_requested = False
        self._pass_active = False
        self._pass_inventory_ready = False
        self._pass_recipes: set[str] = set()
        self._accept_followup = False
        self._inflight: set[str] = set()
        self._requested_recipes: set[str] = set()
        self._observations: dict[str, dict] = {}
        self._futures = set()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="automation-detection")
        self._running = False
        self._last_started = None
        self._last_finished = None
        self._next_check = None
        self._blocker = None
        self._retry_recheck_deadline = None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                raise RuntimeError("Automation scheduler already started")
            self._thread = threading.Thread(target=self._run, name="automation-scheduler", daemon=False)
            self._running = True
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._running = False
                self._stop_requested = True
                self._admission_gate.begin_shutdown()
                self._executor.shutdown(wait=False, cancel_futures=True)
                raise
        LOGGER.info("Automation scheduler started")

    def request(self) -> bool:
        with self._condition:
            if self._stop_requested or not self.enabled:
                return False
            if self._pass_active and not self._accept_followup:
                return False
            self._wake_requested = True
            self._condition.notify_all()
            return True

    def request_orchestration(self) -> bool:
        """Wake durable lifecycle reconciliation without requiring checks on."""
        with self._condition:
            if self._stop_requested or not self._running or not self._admission_gate.accepting:
                return False
            if self._pass_active and not self._accept_followup:
                return False
            self._wake_requested = True
            self._condition.notify_all()
            return True

    def request_recipe(self, recipe_id: str) -> dict:
        """Queue one eligible Recipe on this scheduler, coalescing duplicates."""
        require_safe_name(recipe_id, "Recipe ID")
        with self._condition:
            if self._stop_requested or not self._running or not self._admission_gate.accepting:
                raise AutomationSchedulerError(
                    "automation_scheduler_unavailable", "Automation scheduler is unavailable",
                )
            if not self.enabled:
                raise AutomationSchedulerError(
                    "automation_scheduler_disabled", "Automatic upstream checks are disabled globally",
                )
            if not self.admission_open():
                raise AutomationSchedulerError(
                    "automation_admission_blocked", "Automation admission is unavailable",
                )
            # Every pass evaluates the full eligible Recipe set. Once its
            # inventory is known, a matching target converges with that pass.
            # Before then, the pass has not taken its Recipe snapshot yet.
            existing = (
                recipe_id in self._inflight
                or recipe_id in self._requested_recipes
                or (self._pass_active and self._pass_inventory_ready and recipe_id in self._pass_recipes)
            )
            if not existing:
                if len(self._requested_recipes) >= MAX_RECIPES:
                    raise AutomationSchedulerError(
                        "automation_recipe_capacity", "Automation Recipe request capacity is exhausted",
                    )
                if self._pass_active and not self._accept_followup:
                    raise AutomationSchedulerError(
                        "automation_scheduler_busy", "Automation scheduler cannot accept another check yet",
                    )
                self._requested_recipes.add(recipe_id)
                if not self._pass_active:
                    self._wake_requested = True
                self._condition.notify_all()
            return {"accepted": True, "created": not existing, "recipe_id": recipe_id}

    def recipe_activity(self, recipe_id: str) -> dict:
        require_safe_name(recipe_id, "Recipe ID")
        with self._condition:
            return {
                "queued": recipe_id in self._requested_recipes,
                "checking": recipe_id in self._inflight,
                "observation": copy.deepcopy(self._observations.get(recipe_id)),
            }

    def stop(self) -> None:
        self._admission_gate.begin_shutdown()
        with self._condition:
            self._stop_requested = True
            self._wake_requested = False
            self._requested_recipes.clear()
            for future in tuple(self._futures):
                future.cancel()
            self._condition.notify_all()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else self.monotonic() + max(0.0, timeout)
        thread = self._thread
        if thread is not None:
            remaining = None if deadline is None else max(0.0, deadline - self.monotonic())
            thread.join(remaining)
        with self._condition:
            while self._inflight:
                remaining = None if deadline is None else max(0.0, deadline - self.monotonic())
                if remaining == 0:
                    break
                self._condition.wait(remaining)

    def is_alive(self) -> bool:
        with self._condition:
            return bool((self._thread and self._thread.is_alive()) or self._inflight)

    def status(self) -> dict:
        with self._condition:
            return {
                "state": "running" if self._running else "stopped",
                "checks_enabled": self.enabled,
                "admission_open": self._admission_gate.accepting and not self._stop_requested,
                "pass_active": self._pass_active,
                "last_pass_started": self._last_started,
                "last_pass_finished": self._last_finished,
                "next_scheduled_check": self._next_check,
                "active_recipe_checks": len(self._inflight),
                "global_blocker": copy.deepcopy(self._blocker),
            }

    @contextmanager
    def _mutation_lease(self):
        with self._admission_gate.lease():
            if self.mutation_gate is None:
                yield
            else:
                with self.mutation_gate.lease():
                    yield

    def _eligible(self, retry_rows: dict) -> list[str]:
        paths = sorted(self.recipe_directory.glob("*.json"))
        if len(paths) > MAX_RECIPES:
            raise AutomationSchedulerError("automation_recipe_capacity", "Automation Recipe inventory exceeds its bound")
        eligible = []
        now = self.wall_clock()
        for path in paths:
            recipe_id = path.stem
            if recipe_id == BUILTIN_RECIPE_ID:
                continue
            try:
                recipe = validate_recipe_metadata(recipe_store.load_recipe(path, write_back=False))
            except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError):
                continue
            automation = recipe["automation"]
            if recipe.get("name") != recipe_id or not recipe["active"] or not automation["enabled"] or automation["policy"] == "manual":
                continue
            retry = retry_rows.get(recipe_id)
            if retry and retry["recipe_sha256"] == canonical_recipe_sha256(recipe) and retry.get("not_before"):
                remaining = datetime.fromisoformat(retry["not_before"]).timestamp() - now
                if 0 < remaining <= MAX_BACKOFF_SECONDS:
                    continue
            eligible.append(recipe_id)
        return eligible

    def _failure_result(self, recipe_id: str, code: str = "automation_worker_failure") -> dict:
        return {
            "recipe_id": recipe_id, "recipe_sha256": "0" * 64, "policy": "",
            "identity": None, "classification": "upstream_unavailable", "change": "none",
            "attempt_key": None, "generation": None, "attempt_state": None,
            "claim_eligible": False, "diagnostic": code,
            "retry_after_seconds": None, "rate_limit_reset": None,
        }

    def _worker(self, recipe_id: str) -> dict:
        try:
            if self._stop_requested:
                return self._failure_result(recipe_id, "automation_shutting_down")
            return self.detection_service.check(
                recipe_id, token=self.token_provider(), mutation_lease=self._mutation_lease,
            )
        except Exception as exc:
            LOGGER.error("Automatic upstream detection failed for Recipe %s (%s)", recipe_id, type(exc).__name__)
            return self._failure_result(recipe_id)
        finally:
            with self._condition:
                self._inflight.discard(recipe_id)
                self._condition.notify_all()

    def _future_finished(self, future, recipe_id: str) -> None:
        with self._condition:
            self._futures.discard(future)
            if future.cancelled():
                self._inflight.discard(recipe_id)
            self._condition.notify_all()

    def _retry_delay(self, recipe_id: str, count: int, result: dict) -> int:
        base = min(MAX_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS * (2 ** (count - 1)))
        stable = int(hashlib.sha256(f"{recipe_id}:{count}".encode()).hexdigest()[:8], 16) % 16
        computed = min(MAX_BACKOFF_SECONDS, base + (base * stable // 100))
        hints = [computed]
        retry_after = result.get("retry_after_seconds")
        if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after >= 0:
            hints.append(retry_after)
        reset = result.get("rate_limit_reset")
        if isinstance(reset, int) and not isinstance(reset, bool):
            hints.append(max(0, reset - int(self.wall_clock())))
        return max(MIN_BACKOFF_SECONDS, min(MAX_BACKOFF_SECONDS, max(hints)))

    def _record_outcome(self, result: dict, previous: dict | None) -> None:
        recipe_id = result["recipe_id"]
        classification = result.get("classification")
        with self._condition:
            if len(self._observations) >= MAX_RECIPES and recipe_id not in self._observations:
                oldest = min(self._observations, key=lambda key: self._observations[key]["checked_at"])
                self._observations.pop(oldest, None)
            self._observations[recipe_id] = {
                "checked_at": _utc_now(self.wall_clock),
                "recipe_sha256": str(result.get("recipe_sha256") or "")[:64],
                "display_version": str(result.get("display_version") or "")[:200],
                "display_ref": str(result.get("display_ref") or "")[:200],
            }
        if result.get("identity") is not None:
            with self._mutation_lease():
                self.retry_store.clear(recipe_id)
            return
        if classification == "shutting_down" or result.get("diagnostic") == "automation_shutting_down":
            return
        recipe_sha = result.get("recipe_sha256")
        if not isinstance(recipe_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recipe_sha):
            recipe_sha = "0" * 64
        retry_class = "transient" if classification in TRANSIENT_CLASSIFICATIONS else "source_absent" if classification in SOURCE_ABSENT_CLASSIFICATIONS else "operator"
        prior_count = previous["retry_count"] if previous and previous["recipe_sha256"] == recipe_sha and previous["retry_class"] == retry_class else 0
        count = min(MAX_RETRY_COUNT, prior_count + 1)
        not_before = None
        if retry_class != "operator":
            delay = self._retry_delay(recipe_id, count, result)
            not_before = datetime.fromtimestamp(self.wall_clock(), timezone.utc) + timedelta(seconds=delay)
            not_before = not_before.isoformat()
        row = {
            "recipe_id": recipe_id, "recipe_sha256": recipe_sha,
            "retry_class": retry_class, "classification": str(classification or "upstream_unavailable")[:80],
            "diagnostic": str(result.get("diagnostic") or "automation_detection_failed")[:80],
            "retry_count": count, "not_before": not_before, "updated_at": _utc_now(self.wall_clock),
        }
        with self._mutation_lease():
            self.retry_store.record(row)
        if retry_class != "operator":
            LOGGER.info("Automatic upstream detection retry delayed for Recipe %s (%s)", recipe_id, row["diagnostic"])
        else:
            LOGGER.warning("Automatic upstream detection blocked for Recipe %s (%s)", recipe_id, row["diagnostic"])
        if result.get("diagnostic") == "automation_ledger_bounds_exceeded":
            with self._condition:
                self._blocker = {"code": "automation_ledger_capacity_exhausted"}

    def run_pass(self, *, _accept_followup: bool = True) -> list[dict]:
        with self._condition:
            if self._pass_active or self._stop_requested:
                return []
            self._pass_active = True
            self._pass_inventory_ready = False
            self._pass_recipes.clear()
            self._accept_followup = _accept_followup
            self._last_started = _utc_now(self.wall_clock)
        results = []
        LOGGER.info("Automation detection pass started")
        try:
            if not self.admission_open():
                with self._condition:
                    self._blocker = {"code": "automation_admission_blocked"}
                return []
            if self.orchestrator is not None:
                try:
                    self.orchestrator.advance_all()
                except AutomationLedgerError as exc:
                    with self._condition:
                        self._blocker = {"code": exc.code}
                    return []
            if not self.enabled:
                return []
            try:
                ledger = self.ledger.read()
                state = self.retry_store.read()
            except (AutomationLedgerError, AutomationSchedulerError) as exc:
                with self._condition:
                    self._blocker = {"code": getattr(exc, "code", "automation_state_unavailable")}
                return []
            if len(ledger["attempts"]) >= self.ledger.max_attempts:
                with self._condition:
                    self._blocker = {"code": "automation_ledger_capacity_exhausted"}
                return []
            with self._condition:
                self._blocker = None
            recipe_ids = self._eligible(state["recipes"])
            with self._condition:
                self._pass_recipes = set(recipe_ids)
                self._pass_inventory_ready = True
                requested = set(self._requested_recipes)
                satisfied = requested.intersection(recipe_ids)
                self._requested_recipes.difference_update(
                    requested if not self._accept_followup else satisfied
                )
            recipe_ids.sort(key=lambda recipe_id: (recipe_id not in requested, recipe_id))
            futures = {}
            for recipe_id in recipe_ids:
                with self._condition:
                    if self._stop_requested or recipe_id in self._inflight:
                        continue
                    self._inflight.add(recipe_id)
                try:
                    future = self._executor.submit(self._worker, recipe_id)
                except RuntimeError:
                    with self._condition:
                        self._inflight.discard(recipe_id)
                        self._condition.notify_all()
                    if self._stop_requested:
                        break
                    raise
                futures[future] = recipe_id
                with self._condition:
                    self._futures.add(future)
                future.add_done_callback(lambda completed, selected=recipe_id: self._future_finished(completed, selected))
            pending = set(futures)
            while pending and not self._stop_requested:
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    results.append(result)
                    if result.get("change") == "new" or result.get("identity") is None:
                        LOGGER.info(
                            "Automation detection classified Recipe %s as %s",
                            result["recipe_id"], result.get("classification") or "unknown",
                        )
                    try:
                        self._record_outcome(result, state["recipes"].get(result["recipe_id"]))
                        if self.orchestrator is not None:
                            self.orchestrator.on_detection(result)
                    except (AutomationSchedulerError, MutationGateClosed):
                        if not self._stop_requested:
                            LOGGER.exception("Automation retry state update failed for Recipe %s", result["recipe_id"])
                    finally:
                        # _pass_recipes represents work this pass can still
                        # satisfy.  Once its result and handoff are complete,
                        # a later targeted request must queue the single
                        # follow-up pass instead of coalescing with stale work.
                        with self._condition:
                            self._pass_recipes.discard(result["recipe_id"])
                            self._condition.notify_all()
            for future in pending:
                future.cancel()
            return results
        except AutomationSchedulerError as exc:
            with self._condition:
                self._blocker = {"code": exc.code}
            return results
        except Exception as exc:
            LOGGER.error("Automation scheduler pass failed (%s)", type(exc).__name__)
            with self._condition:
                self._blocker = {"code": "automation_scheduler_internal_error"}
            return results
        finally:
            LOGGER.info("Automation detection pass completed (%d checked)", len(results))
            with self._condition:
                self._last_finished = _utc_now(self.wall_clock)
                self._pass_active = False
                self._pass_inventory_ready = False
                self._pass_recipes.clear()
                self._condition.notify_all()

    def _next_retry_delay(self) -> float | None:
        delays = []
        if self.enabled:
            try:
                rows = self.retry_store.read()["recipes"].values()
            except AutomationSchedulerError:
                rows = ()
            delays.extend(
                min(MAX_BACKOFF_SECONDS, max(0.0, datetime.fromisoformat(row["not_before"]).timestamp() - self.wall_clock()))
                for row in rows if row.get("not_before")
            )
        if self.orchestrator is not None:
            orchestration_delay = self.orchestrator.next_retry_delay()
            if orchestration_delay is not None:
                delays.append(min(MAX_BACKOFF_SECONDS, max(0.0, orchestration_delay)))
        if not delays:
            return None
        delay = min(delays)
        if delay == 0 and self._retry_recheck_deadline is not None:
            return max(0.0, self._retry_recheck_deadline - self.monotonic())
        return delay

    def _run(self) -> None:
        next_periodic = self.monotonic() + self.interval_seconds
        followup = False
        try:
            while True:
                with self._condition:
                    while not self._wake_requested and not self._stop_requested:
                        periodic_delay = max(0.0, next_periodic - self.monotonic())
                        retry_delay = self._next_retry_delay()
                        delay = min(periodic_delay, retry_delay) if retry_delay is not None else periodic_delay
                        self._next_check = (datetime.fromtimestamp(self.wall_clock(), timezone.utc) + timedelta(seconds=delay)).isoformat()
                        if delay <= 0:
                            self._wake_requested = True
                            break
                        self._condition.wait(delay)
                    if self._stop_requested:
                        return
                    self._wake_requested = False
                self.run_pass(_accept_followup=not followup)
                next_periodic = self.monotonic() + self.interval_seconds
                self._retry_recheck_deadline = self.monotonic() + MIN_BACKOFF_SECONDS
                with self._condition:
                    if self._stop_requested:
                        return
                    if not followup and (self._wake_requested or self._requested_recipes):
                        followup = True
                        self._wake_requested = True
                    else:
                        self._wake_requested = False
                        followup = False
                    self._accept_followup = False
                    self._next_check = None
        finally:
            with self._condition:
                self._running = False
                self._pass_active = False
                self._next_check = None
                self._condition.notify_all()
            LOGGER.info("Automation scheduler stopped")
