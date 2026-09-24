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
    """Classify durable HTTP mutations from the canonical API registry."""
    from .api_routes import RouteEffect, match_route

    matched = match_route(method, path)
    return matched is not None and matched.route.effect is RouteEffect.DURABLE_MUTATION
