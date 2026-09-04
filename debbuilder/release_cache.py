"""Persistent stale-while-refresh cache for GitHub release metadata."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import github_client, storage


class GitHubReleaseCache:
    def __init__(self, data_dir: Path, token_provider: Callable[[], str], *, workers: int = 4):
        self.data_dir = Path(data_dir)
        self.token_provider = token_provider
        self.entries: dict[str, tuple[float, dict]] = {}
        self._loaded = False
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="github-refresh")

    @property
    def path(self) -> Path:
        return self.data_dir / "github-release-cache.json"

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            rows = storage.load_json(self.path, {})
            for repository, row in rows.items() if isinstance(rows, dict) else []:
                if isinstance(row, dict) and isinstance(row.get("release"), dict):
                    self.entries[str(repository)] = (float(row.get("expires_at") or 0), row["release"])
            self._loaded = True

    def _refresh(self, repository: str, ttl: int) -> None:
        try:
            release = github_client.latest_release(repository, token=self.token_provider())
            with self._lock:
                self.entries[repository] = (time.time() + ttl, release)
                snapshot = dict(self.entries)
            rows = {name: {"expires_at": expires_at, "release": value} for name, (expires_at, value) in snapshot.items()}
            storage.save_json(self.path, rows)
        finally:
            with self._lock:
                self._refreshing.discard(repository)

    def get(self, repository: str, *, ttl: int = 300) -> dict | None:
        """Return cached data immediately and refresh stale entries off-request."""
        self._load()
        with self._lock:
            cached = self.entries.get(repository)
            stale = not cached or cached[0] <= time.time()
            if stale and repository not in self._refreshing:
                self._refreshing.add(repository)
                self._executor.submit(self._refresh, repository, ttl)
            return cached[1] if cached else None
