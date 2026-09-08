"""Application lifecycle ownership for durable HTTP mutations."""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager


class MutationGateClosed(RuntimeError):
    """A durable mutation was requested after lifecycle shutdown began."""

    code = "application_shutting_down"


class MutationGate:
    """A monotonic admission gate and lease count for durable HTTP mutations."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._accepting = True
        self._active = 0

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    def begin_shutdown(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    @contextmanager
    def lease(self):
        with self._condition:
            if not self._accepting:
                raise MutationGateClosed("Application shutdown has started; durable mutations are closed")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def wait_for_quiescence(self, timeout: float | None) -> dict:
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("mutation shutdown timeout must be a finite non-negative number or None")
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("mutation shutdown timeout must be a finite non-negative number or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._active:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    break
                self._condition.wait(remaining)
            complete = not self._active
            return {
                "status": "complete" if complete else "incomplete",
                "complete": complete,
                "admission_closed": not self._accepting,
                "active_mutations": self._active,
                "timed_out": not complete and deadline is not None,
            }


def is_durable_mutation_route(method: str, path: str) -> bool:
    """Classify HTTP routes whose operation may durably change application state."""
    if method == "DELETE":
        return (
            path.startswith("/api/workflows/")
            or (path.startswith("/api/executions/") and path.endswith("/logs"))
            or path.startswith("/api/packages/")
        )
    if method != "POST":
        return False
    return (
        path == "/api/recipes/import"
        or path == "/api/run"
        or path == "/api/notifications/test"
        or path == "/api/settings"
        or path == "/api/executions/delete-logs"
        or path == "/api/packages"
        or path.startswith("/api/packages/")
        or path.startswith("/api/workflows/")
        or (
            path.startswith("/api/executions/")
            and path.endswith(("/cancel", "/validate", "/publish", "/reconcile-publication"))
        )
    )
