"""One lifecycle-owned worker for storage observation and requested cleanup."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

LOGGER = logging.getLogger(__name__)
MAINTENANCE_INTERVAL_SECONDS = 300


class MaintenanceService:
    def __init__(
        self,
        inventory,
        *,
        cleanup: Callable[[], object] | None = None,
        refresh_interval: float = MAINTENANCE_INTERVAL_SECONDS,
    ):
        self.inventory = inventory
        self.cleanup = cleanup
        self.refresh_interval = float(refresh_interval)
        self._condition = threading.Condition()
        self._refresh_requested = True
        self._cleanup_requested = False
        self._stop_requested = False
        self._pass_active = False
        self._accept_followup = False
        self._thread: threading.Thread | None = None

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                raise RuntimeError("Maintenance service already started")
            self._thread = threading.Thread(
                target=self._run,
                name="storage-maintenance",
                daemon=False,
            )
            self._thread.start()

    def request(self, *, refresh: bool = True, cleanup: bool = False) -> None:
        with self._condition:
            if self._stop_requested:
                return
            # One pass may retain one coalesced follow-up. Requests received
            # during that follow-up are already represented by the work in
            # progress and are left to the fixed periodic sweep.
            if self._pass_active and not self._accept_followup:
                return
            self._refresh_requested = self._refresh_requested or bool(refresh)
            self._cleanup_requested = self._cleanup_requested or bool(cleanup)
            self._condition.notify()

    def _run(self) -> None:
        next_periodic = time.monotonic() + self.refresh_interval
        while True:
            with self._condition:
                while not (
                    self._refresh_requested
                    or self._cleanup_requested
                    or self._stop_requested
                ):
                    remaining = next_periodic - time.monotonic()
                    if remaining <= 0:
                        self._refresh_requested = True
                        self._cleanup_requested = True
                        break
                    self._condition.wait(remaining)
                if self._stop_requested:
                    self._refresh_requested = False
                    self._cleanup_requested = False
                    return
                refresh = self._refresh_requested
                cleanup = self._cleanup_requested
                self._refresh_requested = False
                self._cleanup_requested = False
                self._pass_active = True
                self._accept_followup = True

            while True:
                if cleanup:
                    try:
                        if self.cleanup is not None:
                            self.cleanup()
                    except Exception:
                        LOGGER.exception("Requested workspace cleanup failed")
                if not self.stop_requested() and (refresh or cleanup):
                    try:
                        self.inventory.collect()
                    except Exception:
                        LOGGER.exception("Storage inventory refresh failed")
                if cleanup:
                    # A completed pass, including a denied or failed one,
                    # satisfies this interval. Base the next deadline on all
                    # of its work so a slow scan cannot create catch-up spin.
                    next_periodic = time.monotonic() + self.refresh_interval

                with self._condition:
                    if self._stop_requested:
                        self._refresh_requested = False
                        self._cleanup_requested = False
                        self._pass_active = False
                        self._accept_followup = False
                        return
                    if self._accept_followup and (
                        self._refresh_requested or self._cleanup_requested
                    ):
                        refresh = self._refresh_requested
                        cleanup = self._cleanup_requested
                        self._refresh_requested = False
                        self._cleanup_requested = False
                        self._accept_followup = False
                        continue
                    # Bound a request burst to the active pass and one
                    # follow-up. A periodic pass provides eventual cleanup for
                    # notifications coalesced into the follow-up itself.
                    self._refresh_requested = False
                    self._cleanup_requested = False
                    self._pass_active = False
                    self._accept_followup = False
                    break

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._refresh_requested = False
            self._cleanup_requested = False
            self._condition.notify_all()

    def stop_requested(self) -> bool:
        with self._condition:
            return self._stop_requested

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


class TargetMaintenanceService:
    """Lifecycle adapter retained for deterministic target-based tests."""

    def __init__(self, target):
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=target,
            args=(self._stop,),
            name="storage-maintenance",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def request(self, *, refresh: bool = True, cleanup: bool = False) -> None:
        return None

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()
