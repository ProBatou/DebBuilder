"""One lifecycle-owned worker for storage observation and requested cleanup."""
from __future__ import annotations

import logging
import threading
from typing import Callable

LOGGER = logging.getLogger(__name__)


class MaintenanceService:
    def __init__(
        self,
        inventory,
        *,
        cleanup: Callable[[], object] | None = None,
        refresh_interval: float = 300,
    ):
        self.inventory = inventory
        self.cleanup = cleanup
        self.refresh_interval = float(refresh_interval)
        self._condition = threading.Condition()
        self._refresh_requested = True
        self._cleanup_requested = False
        self._stop_requested = False
        self._drain_on_stop = False
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
            self._refresh_requested = self._refresh_requested or bool(refresh)
            self._cleanup_requested = self._cleanup_requested or bool(cleanup)
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._refresh_requested and not self._cleanup_requested and not self._stop_requested:
                    self._condition.wait(self.refresh_interval)
                    if not self._stop_requested:
                        self._refresh_requested = True
                if self._stop_requested and not (
                    self._drain_on_stop and (self._refresh_requested or self._cleanup_requested)
                ):
                    return
                refresh = self._refresh_requested
                cleanup = self._cleanup_requested
                stopping = self._stop_requested
                self._refresh_requested = False
                self._cleanup_requested = False
            if cleanup and self.cleanup is not None:
                try:
                    self.cleanup()
                except Exception:
                    LOGGER.exception("Requested workspace cleanup failed")
            if refresh or cleanup:
                self.inventory.collect()
            if stopping:
                return

    def stop(self, *, drain: bool = False) -> None:
        with self._condition:
            self._drain_on_stop = self._drain_on_stop or bool(drain)
            self._stop_requested = True
            self._condition.notify_all()

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

    def stop(self, *, drain: bool = False) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()
