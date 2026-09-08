"""Bounded local FIFO execution for persisted DebBuilder Build Runs."""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from . import build_pipeline
from .build_models import utc_now
from .build_store import BuildStore, RunStatusTransitionError
from .execution_cancellation import CANCELLATION_CODE, SERVER_SHUTDOWN, USER_REQUESTED, CancellationControl


LOGGER = logging.getLogger(__name__)

# This is a graceful-shutdown diagnostic target, not a hard process bound.
# Strong-containment termination currently has a three-second operation target
# followed by at most one second of output draining.  Keep this target above
# that backend policy without duplicating its TERM / KILL timings here.
DEFAULT_SHUTDOWN_TIMEOUT = 10.0
TERMINAL_RUN_STATUSES = frozenset({"prepared", "success", "failed", "cancelled"})


class ExecutionManagerError(RuntimeError):
    """A deterministic admission or manager lifecycle error."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class _AdmissionReservation:
    def __init__(self, manager: "ExecutionManager", token: object):
        self._manager = manager
        self._token = token

    def submit(self, run_id: str) -> dict:
        """Consume this reserved queue place for one newly-created Run."""
        self.track(run_id)
        return self._manager._submit_reserved(self._token, run_id)

    def track(self, run_id: str) -> None:
        """Tie this lease to the durable Run as soon as it is created."""
        self._manager._track_reservation(self._token, run_id)

    def confirm_terminal(self, run_id: str) -> bool:
        """Release ownership only when durable state is already terminal."""
        return self._manager._confirm_reservation_terminal(self._token, run_id)

    def confirm_absent(self, run_id: str) -> bool:
        """Release ownership when creation provably made no durable workspace."""
        return self._manager._confirm_reservation_absent(self._token, run_id)

    def resolve_failed_creation(self, run_id: str, failure: dict) -> bool:
        """Safely terminalize or discard an owned incomplete Run creation."""
        return self._manager._resolve_failed_creation(self._token, run_id, failure)

    def transfer_unresolved(self, run_id: str, failure: dict) -> None:
        """Transfer a non-terminal admission failure to shutdown ownership."""
        self._manager._transfer_reservation_unresolved(self._token, run_id, failure)

    def release(self) -> None:
        self._manager._release_reservation(self._token)


class ExecutionManager:
    """Run one persisted Build at a time from a bounded local FIFO."""

    def __init__(self, store: BuildStore, *, queue_capacity: int = 8, execute=None):
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("queue capacity must be a positive integer")
        self.store = store
        self.queue_capacity = queue_capacity
        self._execute = execute or build_pipeline.execute_pipeline_run
        self._condition = threading.Condition()
        self._queue: deque[str] = deque()
        self._reservations: set[object] = set()
        self._admission_leases: set[object] = set()
        self._reservation_run_ids: dict[object, str] = {}
        self._submitting_run_ids: set[str] = set()
        self._worker: threading.Thread | None = None
        self._active_run_id: str | None = None
        self._active_cancellation_control: CancellationControl | None = None
        self._accepting = False
        self._stopping = False
        self._admission_blocker: dict | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_pending: deque[str] = deque()
        self._shutdown_run_ids: list[str] = []
        self._shutdown_cancelled_run_ids: list[str] = []
        self._shutdown_failures: dict[str, dict] = {}
        self._unresolved_admission_runs: dict[str, dict] = {}
        self._unresolved_active_runs: dict[str, dict] = {}
        self._worker_fatal_error: dict | None = None
        self._shutdown_active_run_id: str | None = None
        self._shutdown_active_cancellation: dict | None = None
        self._shutdown_active_requested = False

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def admission_blocker(self) -> dict | None:
        with self._condition:
            return dict(self._admission_blocker) if self._admission_blocker else None

    @property
    def active_run_id(self) -> str | None:
        with self._condition:
            return self._active_run_id

    @property
    def active_cancellation_control(self) -> CancellationControl | None:
        """Return the worker-owned control for internal orchestration only."""
        with self._condition:
            return self._active_cancellation_control

    @property
    def queued_run_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._queue)

    @property
    def worker(self) -> threading.Thread | None:
        with self._condition:
            return self._worker

    def start(self, *, admission_blocker: dict | None = None) -> None:
        """Start the worker, opening admission only after startup recovery."""
        with self._condition:
            if self._shutdown_started:
                raise ExecutionManagerError("execution_manager_stopped", "Execution manager cannot be restarted")
            if self._worker is not None:
                if self._worker.is_alive():
                    return
                raise ExecutionManagerError("execution_manager_stopped", "Execution manager cannot be restarted")
            self._admission_blocker = dict(admission_blocker) if admission_blocker else None
            self._accepting = self._admission_blocker is None
            self._stopping = False
            # The worker owns durable Run and command state.  Process return is
            # therefore forbidden until it has stopped; the supervisor remains
            # the future hard outer bound for an incomplete graceful shutdown.
            self._worker = threading.Thread(target=self._worker_main, name="debbuilder-execution", daemon=False)
            self._worker.start()

    def _require_accepting(self) -> None:
        if not self._accepting:
            if self._admission_blocker:
                raise ExecutionManagerError(
                    str(self._admission_blocker.get("code") or "execution_recovery_blocked"),
                    str(self._admission_blocker.get("message") or "Execution admission is blocked by startup recovery"),
                    details=dict(self._admission_blocker.get("details") or {}),
                )
            raise ExecutionManagerError("execution_manager_not_accepting", "Execution manager is not accepting Runs")

    def _require_capacity(self) -> None:
        if len(self._queue) + len(self._reservations) + len(self._submitting_run_ids) >= self.queue_capacity:
            raise ExecutionManagerError(
                "execution_queue_full",
                "Execution queue is full",
                details={
                    "queued": len(self._queue),
                    "reserved": len(self._reservations),
                    "submitting": len(self._submitting_run_ids),
                    "capacity": self.queue_capacity,
                },
            )

    @contextmanager
    def reserve(self):
        """Atomically reserve one waiting place until creation and submission finish."""
        token = object()
        with self._condition:
            self._require_accepting()
            self._require_capacity()
            self._reservations.add(token)
            self._admission_leases.add(token)
        reservation = _AdmissionReservation(self, token)
        try:
            yield reservation
        finally:
            reservation.release()

    def submit(self, run_id: str) -> dict:
        """Admit one existing pending Run without a prior reservation."""
        with self._condition:
            self._require_accepting()
            self._require_not_submitted(run_id)
            self._require_capacity()
            self._submitting_run_ids.add(run_id)
        return self._persist_and_enqueue(run_id)

    def _submit_reserved(self, token: object, run_id: str) -> dict:
        with self._condition:
            if token not in self._reservations:
                raise ExecutionManagerError("execution_reservation_inactive", "Execution queue reservation is no longer active")
            tracked = self._reservation_run_ids.get(token)
            if tracked != run_id:
                raise ExecutionManagerError(
                    "execution_reservation_run_mismatch",
                    "Execution queue reservation does not own this Build Run",
                    details={"run_id": run_id},
                )
            self._require_not_submitted(run_id)
            self._reservations.remove(token)
            self._submitting_run_ids.add(run_id)
        return self._persist_and_enqueue(run_id, reservation_token=token)

    def _persist_and_enqueue(self, run_id: str, *, reservation_token: object | None = None) -> dict:
        try:
            run = self._mark_queued(run_id)
        except BaseException:
            with self._condition:
                self._submitting_run_ids.discard(run_id)
                self._condition.notify_all()
            raise
        with self._condition:
            self._submitting_run_ids.remove(run_id)
            if self._shutdown_started:
                self._capture_shutdown_run_locked(run_id)
            else:
                self._queue.append(run_id)
            if reservation_token is not None:
                self._settle_reservation_locked(reservation_token)
            self._condition.notify_all()
        return run

    def _capture_shutdown_run_locked(self, run_id: str) -> None:
        if run_id not in self._shutdown_run_ids:
            self._shutdown_run_ids.append(run_id)
        if run_id not in self._shutdown_pending:
            self._shutdown_pending.append(run_id)

    def _track_reservation(self, token: object, run_id: str) -> None:
        self.store.run_dir(run_id)
        with self._condition:
            if token not in self._admission_leases:
                raise ExecutionManagerError(
                    "execution_reservation_inactive", "Execution queue reservation is no longer active",
                )
            tracked = self._reservation_run_ids.get(token)
            if tracked is not None and tracked != run_id:
                raise ExecutionManagerError(
                    "execution_reservation_run_mismatch",
                    "Execution queue reservation already owns another Build Run",
                    details={"run_id": run_id, "owned_run_id": tracked},
                )
            self._reservation_run_ids[token] = run_id

    def _settle_reservation_locked(self, token: object) -> bool:
        removed = token in self._reservations or token in self._admission_leases
        self._reservations.discard(token)
        self._admission_leases.discard(token)
        self._reservation_run_ids.pop(token, None)
        return removed

    @staticmethod
    def is_terminal_run(run: dict | None) -> bool:
        """Return whether validated durable Run state is canonically terminal."""
        return isinstance(run, dict) and run.get("status") in TERMINAL_RUN_STATUSES

    def _durably_terminal(self, run_id: str) -> bool:
        try:
            return self.is_terminal_run(self.store.load(run_id))
        except BaseException:
            return False

    def _confirm_reservation_terminal(self, token: object, run_id: str) -> bool:
        if not self._durably_terminal(run_id):
            return False
        with self._condition:
            if self._reservation_run_ids.get(token) != run_id:
                return token not in self._admission_leases
            removed = self._settle_reservation_locked(token)
            if removed:
                self._condition.notify_all()
            return True

    def _confirm_reservation_absent(self, token: object, run_id: str) -> bool:
        try:
            absent = not self.store.run_dir(run_id).exists()
        except BaseException:
            return False
        if not absent:
            return False
        with self._condition:
            if self._reservation_run_ids.get(token) != run_id:
                return token not in self._admission_leases
            removed = self._settle_reservation_locked(token)
            if removed:
                self._condition.notify_all()
            return True

    def _transfer_reservation_unresolved(self, token: object, run_id: str, failure: dict) -> None:
        with self._condition:
            if token not in self._admission_leases:
                return
            if self._reservation_run_ids.get(token) != run_id:
                raise ExecutionManagerError(
                    "execution_reservation_run_mismatch",
                    "Execution queue reservation does not own this Build Run",
                    details={"run_id": run_id},
                )
            self._unresolved_admission_runs.setdefault(run_id, dict(failure))
            self._shutdown_failures.setdefault(run_id, {
                "code": "execution_shutdown_persistence_failed",
                "message": "Admission-failed Run remains durably non-terminal",
                "run_id": run_id,
                "ownership": "admission",
                "exception_type": str(
                    failure.get("terminalization_exception_type")
                    or failure.get("exception_type")
                    or "DurableStateUnresolved"
                ),
            })
            self._settle_reservation_locked(token)
            self._condition.notify_all()

    def _resolve_failed_creation(self, token: object, run_id: str, failure: dict) -> bool:
        try:
            resolved = self._terminalize_unresolved_admission(run_id, failure)
        except BaseException:
            return (
                self._confirm_reservation_terminal(token, run_id)
                or self._confirm_reservation_absent(token, run_id)
            )
        if not resolved:
            return False
        return (
            self._confirm_reservation_terminal(token, run_id)
            or self._confirm_reservation_absent(token, run_id)
        )

    def _release_reservation(self, token: object) -> None:
        with self._condition:
            if token not in self._admission_leases:
                return
            run_id = self._reservation_run_ids.get(token)
            if run_id is None:
                self._settle_reservation_locked(token)
                self._condition.notify_all()
                return
            manager_owned = (
                run_id == self._active_run_id
                or run_id in self._queue
                or run_id in self._submitting_run_ids
                or run_id in self._shutdown_pending
            )
            if manager_owned:
                self._settle_reservation_locked(token)
                self._condition.notify_all()
                return

        if self._confirm_reservation_terminal(token, run_id):
            return
        self._transfer_reservation_unresolved(token, run_id, {
            "code": "execution_enqueue_persistence_unresolved",
            "message": "Build Run admission failed before durable ownership could be confirmed",
            "run_id": run_id,
            "exception_type": "UnknownAdmissionFailure",
        })

    def _require_not_submitted(self, run_id: str) -> None:
        if run_id == self._active_run_id or run_id in self._queue or run_id in self._submitting_run_ids:
            raise ExecutionManagerError(
                "build_run_already_submitted",
                "Build Run is already queued or running",
                details={"run_id": run_id},
            )

    def _mark_queued(self, run_id: str) -> dict:
        try:
            return self.store.transition_status(run_id, expected="pending", status="queued")
        except FileNotFoundError as exc:
            raise ExecutionManagerError("build_run_not_found", "Build Run was not found", details={"run_id": run_id}) from exc
        except RunStatusTransitionError as exc:
            raise ExecutionManagerError(
                "build_run_not_pending",
                f"Build Run cannot be queued from status {exc.actual}",
                details={"run_id": run_id, "status": exc.actual},
            ) from exc

    def cancel(self, run_id: str) -> dict:
        """Cancel queue-owned work or request cancellation from its active control."""
        # run_dir applies the same safe-name validation used by Run persistence.
        self.store.run_dir(run_id)
        active_result = None
        with self._condition:
            if run_id in self._queue:
                return self._cancel_queued_locked(run_id)
            if run_id == self._active_run_id:
                control = self._active_cancellation_control
                if control is None:
                    raise RuntimeError("active Run is missing its cancellation control")
                requested = control.request_cancel()
                if not requested["accepted"]:
                    active_result = {
                        "outcome": "terminal_not_cancellable",
                        "run_id": run_id,
                        "status": "active",
                    }
                else:
                    active_result = {
                        "outcome": "active_cancel_requested",
                        "run_id": run_id,
                        "first_request": requested["first_request"],
                        "cancellation": requested["cancellation"],
                    }

        if active_result is not None:
            # Durable terminal persistence may commit just before the worker's
            # ownership-safety finalizer clears the active context.  Repeated
            # cancellation observes that commit instead of projecting stale
            # ``cancelling`` state from the still-installed context.
            if not active_result.get("first_request"):
                run = self.store.load(run_id)
                if run and run.get("status") == "cancelled":
                    return {
                        "outcome": "already_cancelled",
                        "run_id": run_id,
                        "status": "cancelled",
                        "cancellation": dict(run.get("cancellation") or {}),
                    }
            return active_result

        run = self.store.load(run_id)
        with self._condition:
            # Ownership may have changed while durable state was read.  Prefer
            # the manager owner unless the read already proves terminal state.
            if run_id == self._active_run_id and not self.is_terminal_run(run):
                control = self._active_cancellation_control
                if control is None:
                    raise RuntimeError("active Run is missing its cancellation control")
                requested = control.request_cancel()
                if requested["accepted"]:
                    return {
                        "outcome": "active_cancel_requested",
                        "run_id": run_id,
                        "first_request": requested["first_request"],
                        "cancellation": requested["cancellation"],
                    }
            if not run:
                return {"outcome": "not_found", "run_id": run_id}
            if run.get("status") == "cancelled":
                return {
                    "outcome": "already_cancelled",
                    "run_id": run_id,
                    "status": "cancelled",
                    "cancellation": dict(run.get("cancellation") or {}),
                }
            return {
                "outcome": "terminal_not_cancellable",
                "run_id": run_id,
                "status": str(run.get("status") or ""),
            }

    def _cancel_queued_locked(self, run_id: str) -> dict:
        """Persist then remove one queue-owned Run while holding the Condition."""
        cancellation = self._persist_queued_cancellation(run_id, reason=USER_REQUESTED)

        # Persistence is the commit point.  A failed save leaves the deque and
        # its capacity unchanged so the worker may still execute the Run.
        self._queue.remove(run_id)
        self._condition.notify_all()
        return {
            "outcome": "queued_cancelled",
            "run_id": run_id,
            "status": "cancelled",
            "cancellation": cancellation,
        }

    def _persist_queued_cancellation(self, run_id: str, *, reason: str) -> dict:
        """Persist the canonical terminal state for one manager-owned queued Run."""
        with self.store.locked_run(run_id):
            run = self.store.load(run_id)
            if not run:
                raise ExecutionManagerError(
                    "build_run_not_found", "Build Run was not found", details={"run_id": run_id},
                )
            existing = run.get("cancellation") or {}
            if (
                run.get("status") == "cancelled"
                and existing.get("code") == CANCELLATION_CODE
                and existing.get("reason") == reason
                and existing.get("phase") == "queue"
            ):
                return dict(existing)
            if run.get("status") != "queued":
                raise ExecutionManagerError(
                    "build_run_not_queued",
                    f"Build Run cannot be cancelled from status {run.get('status')}",
                    details={"run_id": run_id, "status": run.get("status")},
                )
            requested_at = utc_now()
            completed_at = utc_now()
            cancellation = {
                "code": CANCELLATION_CODE,
                "reason": reason,
                "phase": "queue",
                "stage": "queue",
                "requested_at": requested_at,
                "completed_at": completed_at,
            }
            run.update({
                "status": "cancelled",
                "started_at": None,
                "finished_at": completed_at,
                "duration": 0.0,
                "error": None,
                "cancellation": cancellation,
            })
            self.store.save(run)
        return cancellation

    def stop(self, timeout: float | None = None) -> None:
        """Revoke unused reservations, drain submitted work, and join the worker."""
        with self._condition:
            self._accepting = False
            self._stopping = True
            for token in tuple(self._admission_leases):
                if token not in self._reservation_run_ids:
                    self._settle_reservation_locked(token)
            worker = self._worker
            self._condition.notify_all()
        if worker is None:
            return
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError("Execution manager worker did not stop before the timeout")

    def _begin_shutdown_locked(self, *, request_active: bool) -> None:
        if not self._shutdown_started:
            self._shutdown_started = True
            self._accepting = False
            self._stopping = True
            while self._queue:
                self._capture_shutdown_run_locked(self._queue.popleft())
            self._shutdown_active_run_id = self._active_run_id
        if request_active and not self._shutdown_active_requested:
            self._shutdown_active_requested = True
            control = self._active_cancellation_control
            if control is not None:
                self._shutdown_active_cancellation = control.request_cancel(reason=SERVER_SHUTDOWN)
        self._condition.notify_all()

    def begin_shutdown(self) -> None:
        """Close admission and freeze queue ownership without blocking on I/O."""
        with self._condition:
            self._begin_shutdown_locked(request_active=False)

    def _cancel_shutdown_queued_run(self, run_id: str) -> None:
        """Terminalize one queue-owned Run without holding the manager condition."""
        self._persist_queued_cancellation(run_id, reason=SERVER_SHUTDOWN)

    @staticmethod
    def _admission_failure_error(failure: dict) -> dict:
        run_id = str(failure.get("run_id") or "")
        details = {
            "run_id": run_id,
            "exception_type": str(failure.get("exception_type") or "UnknownAdmissionFailure"),
        }
        if failure.get("manager_code"):
            details["manager_code"] = str(failure["manager_code"])
        return {
            "stage": "queue",
            "code": "execution_enqueue_failed",
            "message": "The Build Run could not be submitted for execution",
            "details": details,
        }

    def _terminalize_unresolved_admission(self, run_id: str, failure: dict) -> bool:
        """Idempotently settle request-created work without executing it."""
        try:
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                if self.is_terminal_run(run):
                    return True
                if not run:
                    if failure.get("ownership_phase") != "creation":
                        return False
                    return self.store.discard_incomplete_creation(run_id)
                if run.get("status") not in {"pending", "queued"}:
                    return False
                run.update({
                    "status": "failed",
                    "finished_at": utc_now(),
                    "duration": 0.0,
                    "error": self._admission_failure_error(failure),
                })
                self.store.save(run)
        except Exception:
            return self._durably_terminal(run_id)
        try:
            self.store.append_log_line(run_id, "Execution enqueue failed; Run marked failed.", level="error")
        except OSError as exc:
            LOGGER.warning(
                "Could not append enqueue failure log for Run %s (%s)", run_id, type(exc).__name__,
            )
        return True

    def _shutdown_task_locked(self, attempted: set[tuple[str, str]], kinds: frozenset[str]):
        if "queued" in kinds:
            for run_id in self._shutdown_pending:
                key = ("queued", run_id)
                if key not in attempted:
                    return key, None
        if "admission" in kinds:
            for run_id, failure in self._unresolved_admission_runs.items():
                key = ("admission", run_id)
                if key not in attempted:
                    return key, dict(failure)
        if "active" in kinds:
            for run_id, failure in self._unresolved_active_runs.items():
                key = ("active", run_id)
                if key not in attempted:
                    return key, dict(failure)
        return None, None

    def _record_shutdown_task_result(
        self, kind: str, run_id: str, *, resolved: bool, exception: BaseException | None = None,
    ) -> None:
        with self._condition:
            if resolved:
                if kind == "queued":
                    if run_id in self._shutdown_pending:
                        self._shutdown_pending.remove(run_id)
                    if run_id not in self._shutdown_cancelled_run_ids:
                        self._shutdown_cancelled_run_ids.append(run_id)
                elif kind == "admission":
                    self._unresolved_admission_runs.pop(run_id, None)
                else:
                    self._unresolved_active_runs.pop(run_id, None)
                self._shutdown_failures.pop(run_id, None)
            else:
                labels = {
                    "queued": "Queued Run cancellation could not be persisted during shutdown",
                    "admission": "Admission-failed Run could not be terminalized during shutdown",
                    "active": "Worker-failed Run could not be terminalized during shutdown",
                }
                self._shutdown_failures[run_id] = {
                    "code": "execution_shutdown_persistence_failed",
                    "message": labels[kind],
                    "run_id": run_id,
                    "ownership": kind,
                    "exception_type": type(exception).__name__ if exception is not None else "DurableStateUnresolved",
                }
            self._condition.notify_all()

    def _drain_shutdown_persistence(
        self, deadline: float | None, *, kinds: frozenset[str] = frozenset({"queued", "admission", "active"}),
    ) -> None:
        attempted: set[tuple[str, str]] = set()
        while True:
            with self._condition:
                task, failure = self._shutdown_task_locked(attempted, kinds)
                admitted = bool(self._admission_leases or self._submitting_run_ids)
                remaining = self._remaining(deadline)
                if task is None and admitted and (remaining is None or remaining > 0):
                    self._condition.wait(timeout=remaining)
                    continue
                unresolved = bool(
                    ("queued" in kinds and self._shutdown_pending)
                    or ("admission" in kinds and self._unresolved_admission_runs)
                    or ("active" in kinds and self._unresolved_active_runs)
                )
                if task is None and unresolved and deadline is None:
                    self._condition.wait(timeout=1.0)
                    attempted.clear()
                    continue
            remaining = self._remaining(deadline)
            if task is None or (remaining is not None and remaining == 0):
                return
            kind, run_id = task
            attempted.add(task)
            try:
                if kind == "queued":
                    self._cancel_shutdown_queued_run(run_id)
                    resolved = True
                elif kind == "admission":
                    resolved = self._terminalize_unresolved_admission(run_id, failure or {})
                else:
                    resolved = self._finalize_worker_error(run_id, safe_error=failure)
            except BaseException as exc:
                self._record_shutdown_task_result(kind, run_id, resolved=False, exception=exc)
            else:
                self._record_shutdown_task_result(kind, run_id, resolved=resolved)

    def _shutdown_result(self, *, worker_joined: bool, timed_out: bool) -> dict:
        with self._condition:
            unresolved_queued = list(self._shutdown_pending)
            unresolved_admission = list(self._unresolved_admission_runs)
            unresolved_active = list(self._unresolved_active_runs)
            unresolved = list(dict.fromkeys(
                unresolved_queued + unresolved_admission + unresolved_active
            ))
            active = dict(self._shutdown_active_cancellation or {})
            errors = [self._shutdown_failures[run_id] for run_id in unresolved if run_id in self._shutdown_failures]
            if self._worker_fatal_error is not None:
                errors.append(dict(self._worker_fatal_error))
            if not worker_joined:
                errors.append({
                    "code": "execution_shutdown_timeout",
                    "message": "Execution manager worker did not stop before the shutdown deadline",
                })
            complete = (
                self._shutdown_started
                and worker_joined
                and not unresolved
                and self._active_run_id is None
                and not self._admission_leases
                and not self._submitting_run_ids
                and self._worker_fatal_error is None
            )
            status = "complete_after_deadline" if complete and timed_out else ("complete" if complete else "incomplete")
            return {
                "status": status,
                "complete": complete,
                "admission_closed": not self._accepting,
                "queued_run_ids": list(self._shutdown_run_ids),
                "cancelled_run_ids": list(self._shutdown_cancelled_run_ids),
                "unresolved_run_ids": unresolved,
                "unresolved_queued_run_ids": unresolved_queued,
                "unresolved_admission_run_ids": unresolved_admission,
                "unresolved_active_run_ids": unresolved_active,
                "active_run_id": self._shutdown_active_run_id,
                "active_context_run_id": self._active_run_id,
                "active_cancellation": active or None,
                "worker_fatal_error": dict(self._worker_fatal_error) if self._worker_fatal_error else None,
                "worker_joined": worker_joined,
                "timed_out": timed_out,
                "outstanding_reservations": len(self._admission_leases),
                "submitting_run_ids": sorted(self._submitting_run_ids),
                "errors": errors,
            }

    @staticmethod
    def _shutdown_deadline(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("shutdown timeout must be a finite non-negative number or None")
        value = float(timeout)
        if not math.isfinite(value) or value < 0:
            raise ValueError("shutdown timeout must be a finite non-negative number or None")
        return time.monotonic() + value

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def shutdown(self, timeout: float | None = DEFAULT_SHUTDOWN_TIMEOUT) -> dict:
        """Cancel manager-owned work and join the worker within a graceful target.

        Queue ownership is transferred while holding the Condition.  Run
        persistence then happens outside it, so storage cannot block manager
        state observation or active cancellation.  ``None`` retains ownership
        until all admitted work is resolved and the worker has stopped.
        """
        deadline = self._shutdown_deadline(timeout)
        remaining = self._remaining(deadline)
        if remaining is None:
            acquired = self._shutdown_lock.acquire()
        elif remaining == 0:
            acquired = self._shutdown_lock.acquire(blocking=False)
        else:
            acquired = self._shutdown_lock.acquire(timeout=remaining)
        if not acquired:
            worker = self.worker
            joined = worker is None or not worker.is_alive()
            return self._shutdown_result(worker_joined=joined, timed_out=True)
        try:
            with self._condition:
                self._begin_shutdown_locked(request_active=True)

            self._drain_shutdown_persistence(deadline)

            worker = self.worker
            if worker is not None and worker.is_alive():
                worker.join(self._remaining(deadline))
            joined = worker is None or not worker.is_alive()
            if joined:
                self._drain_shutdown_persistence(deadline, kinds=frozenset({"active"}))
            deadline_expired = deadline is not None and self._remaining(deadline) == 0
            timed_out = deadline_expired
            return self._shutdown_result(worker_joined=joined, timed_out=timed_out)
        finally:
            self._shutdown_lock.release()

    def _worker_main(self) -> None:
        LOGGER.info("Execution worker started")
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: bool(self._queue)
                        or (self._stopping and not self._admission_leases and not self._submitting_run_ids)
                    )
                    if self._shutdown_started:
                        if not self._admission_leases and not self._submitting_run_ids:
                            return
                        continue
                    if not self._queue:
                        if self._stopping and not self._admission_leases and not self._submitting_run_ids:
                            return
                        continue
                    run_id = self._queue.popleft()
                    # Dequeue and active ownership are one Condition-protected
                    # operation.  Cancellation can never observe an ownership gap.
                    self._active_run_id = run_id
                    self._active_cancellation_control = CancellationControl()
                    cancellation_control = self._active_cancellation_control
                LOGGER.info("Dequeued Build Run %s", run_id)
                worker_failure = None
                fatal_error = None
                try:
                    try:
                        self._execute(
                            run_id, store=self.store, expected_initial_status="queued",
                            cancellation_control=cancellation_control,
                        )
                    except BaseException as exc:
                        LOGGER.error("Execution worker failed for Build Run %s (%s)", run_id, type(exc).__name__)
                        worker_failure = self._worker_failure(exc)
                        if not isinstance(exc, Exception):
                            fatal_error = exc
                    else:
                        if not self._durably_terminal(run_id):
                            LOGGER.error("Execution worker returned without terminal Build Run state for %s", run_id)
                            worker_failure = self._worker_failure(
                                RuntimeError("execution callback returned without durable terminal state")
                            )
                    if worker_failure is not None:
                        try:
                            self._finalize_worker_error(run_id, safe_error=worker_failure)
                        except BaseException as exc:
                            LOGGER.error(
                                "Execution worker finalizer failed for Build Run %s (%s)",
                                run_id, type(exc).__name__,
                            )
                            if fatal_error is None and not isinstance(exc, Exception):
                                fatal_error = exc
                finally:
                    resolved = self._durably_terminal(run_id)
                    ownership_failure = worker_failure or self._worker_failure(
                        RuntimeError("active Run durable state could not be proven terminal")
                    )
                    with self._condition:
                        if not resolved:
                            self._unresolved_active_runs.setdefault(run_id, dict(ownership_failure))
                            self._shutdown_failures.setdefault(run_id, {
                                "code": "execution_shutdown_persistence_failed",
                                "message": "Worker-failed Run remains durably non-terminal",
                                "run_id": run_id,
                                "ownership": "active",
                                "exception_type": str(
                                    (ownership_failure.get("details") or {}).get("exception_type")
                                    or "DurableStateUnresolved"
                                ),
                            })
                            # A worker whose completion could not be persisted
                            # fails closed.  Remaining queue ownership is kept
                            # for shutdown; no workload is executed twice.
                            self._accepting = False
                            self._stopping = True
                        if fatal_error is not None:
                            self._worker_fatal_error = {
                                "code": "execution_worker_fatal_error",
                                "message": "Execution worker exited because of a fatal control-flow exception",
                                "run_id": run_id,
                                "exception_type": type(fatal_error).__name__,
                            }
                            self._accepting = False
                            self._stopping = True
                        self._active_run_id = None
                        self._active_cancellation_control = None
                        self._condition.notify_all()
                if fatal_error is not None:
                    raise fatal_error
                if not resolved:
                    return
        finally:
            LOGGER.info("Execution worker stopped")

    @staticmethod
    def _elapsed(started_at: str | None, finished_at: str) -> float:
        try:
            return round(max(0.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(str(started_at))).total_seconds()), 6)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _worker_failure(exc: BaseException) -> dict:
        return {
            "stage": "worker",
            "code": "execution_worker_error",
            "message": "The execution worker encountered an unexpected error",
            "details": {"exception_type": type(exc).__name__},
        }

    def _finalize_worker_error(
        self, run_id: str, exc: BaseException | None = None, *, safe_error: dict | None = None,
    ) -> bool:
        """Idempotently persist worker failure, proving terminal state on errors."""
        failure = dict(safe_error or self._worker_failure(exc or RuntimeError("worker failure")))
        try:
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                if self.is_terminal_run(run):
                    return True
                if not run or run.get("status") not in {"queued", "running", "cancelling"}:
                    return False
                finished_at = utc_now()
                active_step = next((step for step in run["steps"] if step.get("status") == "running"), None)
                if active_step:
                    active_step.update({
                        "status": "failed",
                        "finished_at": finished_at,
                        "duration": self._elapsed(active_step.get("started_at"), finished_at),
                        "summary": failure["message"],
                        "error": failure,
                    })
                    failure["stage"] = active_step["name"]
                run.update({
                    "status": "failed",
                    "finished_at": finished_at,
                    "duration": self._elapsed(run.get("started_at"), finished_at),
                    "error": failure,
                })
                self.store.save(run)
        except Exception as finalize_error:
            if self._durably_terminal(run_id):
                return True
            LOGGER.error(
                "Could not finalize Build Run %s after worker failure (%s)",
                run_id, type(finalize_error).__name__,
            )
            return False
        try:
            exception_type = str((failure.get("details") or {}).get("exception_type") or "UnknownWorkerFailure")
            self.store.append_log_line(
                run_id, f"Execution worker error ({exception_type}); Run marked failed.", level="error",
            )
        except Exception as finalize_error:
            LOGGER.warning(
                "Could not append worker failure log for Build Run %s (%s)",
                run_id, type(finalize_error).__name__,
            )
        return True
