"""Bounded local FIFO execution for persisted DebBuilder Build Runs."""
from __future__ import annotations

import logging
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime

from . import build_pipeline
from .build_models import utc_now
from .build_store import BuildStore, RunStatusTransitionError


LOGGER = logging.getLogger(__name__)


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
        return self._manager._submit_reserved(self._token, run_id)

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
        self._submitting_run_ids: set[str] = set()
        self._worker: threading.Thread | None = None
        self._active_run_id: str | None = None
        self._accepting = False
        self._stopping = False

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def active_run_id(self) -> str | None:
        with self._condition:
            return self._active_run_id

    @property
    def queued_run_ids(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._queue)

    @property
    def worker(self) -> threading.Thread | None:
        with self._condition:
            return self._worker

    def start(self) -> None:
        """Start the single non-daemon worker and begin accepting Runs."""
        with self._condition:
            if self._worker is not None:
                if self._worker.is_alive():
                    return
                raise ExecutionManagerError("execution_manager_stopped", "Execution manager cannot be restarted")
            self._accepting = True
            self._stopping = False
            self._worker = threading.Thread(target=self._worker_main, name="debbuilder-execution", daemon=False)
            self._worker.start()

    def _require_accepting(self) -> None:
        if not self._accepting:
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
            self._require_not_submitted(run_id)
            self._reservations.remove(token)
            self._submitting_run_ids.add(run_id)
        return self._persist_and_enqueue(run_id)

    def _persist_and_enqueue(self, run_id: str) -> dict:
        try:
            run = self._mark_queued(run_id)
        except BaseException:
            with self._condition:
                self._submitting_run_ids.discard(run_id)
                self._condition.notify_all()
            raise
        with self._condition:
            self._submitting_run_ids.remove(run_id)
            self._queue.append(run_id)
            self._condition.notify_all()
        return run

    def _release_reservation(self, token: object) -> None:
        with self._condition:
            if token in self._reservations:
                self._reservations.remove(token)
                self._condition.notify_all()

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

    def stop(self, timeout: float | None = None) -> None:
        """Revoke unused reservations, drain submitted work, and join the worker."""
        with self._condition:
            self._accepting = False
            self._stopping = True
            self._reservations.clear()
            worker = self._worker
            self._condition.notify_all()
        if worker is None:
            return
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError("Execution manager worker did not stop before the timeout")

    def _worker_main(self) -> None:
        LOGGER.info("Execution worker started")
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: bool(self._queue) or (self._stopping and not self._submitting_run_ids))
                    if not self._queue:
                        if self._stopping and not self._submitting_run_ids:
                            return
                        continue
                    run_id = self._queue.popleft()
                    self._active_run_id = run_id
                LOGGER.info("Dequeued Build Run %s", run_id)
                try:
                    self._execute(run_id, store=self.store, expected_initial_status="queued")
                except BaseException as exc:
                    LOGGER.error("Execution worker failed for Build Run %s (%s)", run_id, type(exc).__name__)
                    self._finalize_worker_error(run_id, exc)
                finally:
                    with self._condition:
                        self._active_run_id = None
                        self._condition.notify_all()
        finally:
            LOGGER.info("Execution worker stopped")

    @staticmethod
    def _elapsed(started_at: str | None, finished_at: str) -> float:
        try:
            return round(max(0.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(str(started_at))).total_seconds()), 6)
        except (TypeError, ValueError):
            return 0.0

    def _finalize_worker_error(self, run_id: str, exc: BaseException) -> None:
        safe_error = {
            "stage": "worker",
            "code": "execution_worker_error",
            "message": "The execution worker encountered an unexpected error",
            "details": {"exception_type": type(exc).__name__},
        }
        try:
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                if not run or run.get("status") not in {"queued", "running"}:
                    return
                finished_at = utc_now()
                active_step = next((step for step in run["steps"] if step.get("status") == "running"), None)
                if active_step:
                    active_step.update({
                        "status": "failed",
                        "finished_at": finished_at,
                        "duration": self._elapsed(active_step.get("started_at"), finished_at),
                        "summary": safe_error["message"],
                        "error": safe_error,
                    })
                    safe_error["stage"] = active_step["name"]
                run.update({
                    "status": "failed",
                    "finished_at": finished_at,
                    "duration": self._elapsed(run.get("started_at"), finished_at),
                    "error": safe_error,
                })
                self.store.save(run)
            self.store.append_log_line(run_id, f"Execution worker error ({type(exc).__name__}); Run marked failed.", level="error")
        except Exception as finalize_error:
            LOGGER.error("Could not finalize Build Run %s after worker failure (%s)", run_id, type(finalize_error).__name__)
