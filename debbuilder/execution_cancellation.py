"""Thread-safe cancellation ownership for one asynchronous execution."""
from __future__ import annotations

import threading

from .build_models import utc_now


CANCELLATION_CODE = "execution_cancelled"
USER_REQUESTED = "user_requested"
SERVER_SHUTDOWN = "server_shutdown"


class ExecutionCancelled(RuntimeError):
    """Known cooperative cancellation propagated by execution components."""

    def __init__(self, cancellation: dict | None = None, *, command_result: dict | None = None):
        metadata = {
            "code": CANCELLATION_CODE,
            "reason": USER_REQUESTED,
            **(cancellation or {}),
        }
        super().__init__("Execution cancelled")
        self.cancellation = metadata
        self.command_result = command_result
        self.termination_error = str((command_result or {}).get("termination_error") or "")


def raise_for_cancelled_result(result: dict) -> None:
    """Preserve cancellation when a compatible runner returns instead of raising."""
    if result.get("cancellation_requested") or result.get("status") == "cancelled":
        raise ExecutionCancelled(result.get("cancellation"), command_result=result)


class CancellationControl:
    """Settle cancellation versus terminalization exactly once.

    The event is the future cooperative notification mechanism.  The ownership
    gate is deliberately independent from Build stages and persistence.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._owner = "open"
        self._request: dict | None = None

    @property
    def owner(self) -> str:
        with self._lock:
            return self._owner

    @property
    def request(self) -> dict | None:
        with self._lock:
            return dict(self._request) if self._request else None

    def request_cancel(self, *, reason: str = USER_REQUESTED) -> dict:
        """Claim cancellation ownership, or report that terminalization won."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("cancellation reason must be a non-empty string")
        with self._lock:
            if self._owner == "terminal_owned":
                return {
                    "accepted": False,
                    "first_request": False,
                    "owner": self._owner,
                    "cancellation": None,
                }
            first_request = self._owner == "open"
            if first_request:
                self._owner = "cancellation_owned"
                self._request = {
                    "code": CANCELLATION_CODE,
                    "reason": reason,
                    "requested_at": utc_now(),
                }
                self.event.set()
            return {
                "accepted": True,
                "first_request": first_request,
                "owner": self._owner,
                "cancellation": dict(self._request or {}),
            }

    def claim_terminal(self) -> bool:
        """Claim normal terminalization once unless cancellation won first."""
        with self._lock:
            if self._owner != "open":
                return False
            self._owner = "terminal_owned"
            return True
