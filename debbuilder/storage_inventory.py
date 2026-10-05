"""Fail-soft, read-only accounting for DebBuilder-managed storage."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import storage_pruning
from .build_models import validate_run
from .build_store import BuildStore
from .maintenance import MAINTENANCE_INTERVAL_SECONDS
from .workspace_cleanup import (
    DEFAULT_POLICY,
    MAX_SAFE_JSON_INTEGER,
    directory_fd,
    validate_policy,
)

CATEGORIES = (
    "metadata",
    "logs_manifests",
    "artifacts",
    "validation_previous",
    "disposable",
    "cache",
    "unknown",
)
MAX_DIAGNOSTICS = 20
MAX_LARGEST_RUNS = 5
RUN_METADATA_FILES = frozenset({
    "run.json",
    "recipe.json",
    ".workspace.lock",
    ".active-command.json",
    ".workspace-cleanup.json",
    ".artifact-pruning-intent.json",
    ".staging-manifest-pruning-intent.json",
    ".execution-history-deleted.json",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _unknown_filesystems() -> dict:
    return {
        "builds": {
            "measurement_state": "unknown",
            "pressure_state": "unknown",
        },
        "repository": {
            "measurement_state": "unknown",
            "pressure_state": "unknown",
            "same_as_builds": False,
        },
    }


def _filesystem_measurement(fd: int, *, measured_scope: str) -> dict:
    """Measure one pinned filesystem without traversing its contents."""
    info = os.fstat(fd)
    capacity = os.fstatvfs(fd)
    fragment_size = int(capacity.f_frsize)
    blocks = int(capacity.f_blocks)
    free_blocks = int(capacity.f_bfree)
    available_blocks = int(capacity.f_bavail)
    device_id = int(info.st_dev)
    if (
        fragment_size <= 0
        or blocks <= 0
        or not 0 <= available_blocks <= free_blocks <= blocks
        or not 0 <= device_id <= MAX_SAFE_JSON_INTEGER
    ):
        raise ValueError("Filesystem capacity values are invalid")
    total = blocks * fragment_size
    free = free_blocks * fragment_size
    available = available_blocks * fragment_size
    used = total - free
    if any(
        value > MAX_SAFE_JSON_INTEGER
        for value in (total, used, free, available)
    ):
        raise ValueError("Filesystem capacity exceeds the exact JSON integer range")
    return {
        "measurement_state": "ready",
        "measured_scope": measured_scope,
        "device_id": device_id,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "available_bytes": available,
        "available_percent": round(available * 100 / total, 2),
        "utilized_percent": round(used * 100 / total, 2),
    }


def _measurement_error(message: str) -> dict:
    return {
        "measurement_state": "error",
        "pressure_state": "measurement_error",
        "diagnostic": str(message)[:240],
    }


def _measure_builds_filesystem(
    data_root: Path,
    *,
    measure_fd: Callable[..., dict] = _filesystem_measurement,
) -> dict:
    """Measure DATA/builds, or its safely pinned DATA parent before it exists."""
    try:
        with directory_fd(data_root) as data_fd:
            builds_fd = -1
            try:
                builds_fd = os.open(
                    "builds",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=data_fd,
                )
            except FileNotFoundError:
                return measure_fd(data_fd, measured_scope="data_parent")
            try:
                return measure_fd(builds_fd, measured_scope="builds")
            finally:
                os.close(builds_fd)
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        return _measurement_error(
            f"Build workspace filesystem measurement failed: {type(exc).__name__}"
        )


def _measure_repository_filesystem(
    repository_root: Path,
    *,
    measure_fd: Callable[..., dict] = _filesystem_measurement,
) -> dict:
    try:
        with directory_fd(repository_root) as repository_fd:
            return measure_fd(repository_fd, measured_scope="repository")
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        return _measurement_error(
            f"Repository filesystem measurement failed: {type(exc).__name__}"
        )


def effective_pressure_thresholds(total_bytes: int, policy: dict) -> tuple[int, int]:
    """Return integer start/target bytes without rounded percentage decisions."""
    canonical = validate_policy(policy)
    start_percent = (
        total_bytes * canonical["pressure_minimum_free_percent"] + 99
    ) // 100
    target_percent = (
        total_bytes * canonical["pressure_target_free_percent"] + 99
    ) // 100
    return (
        max(canonical["pressure_minimum_free_bytes"], start_percent),
        max(canonical["pressure_target_free_bytes"], target_percent),
    )


def pressure_state_for_measurement(
    previous_state: str,
    measurement: dict,
    policy: dict,
) -> tuple[str, int | None, int | None]:
    """Apply the accepted cold-start and hysteresis state machine."""
    if measurement.get("measurement_state") != "ready":
        return "measurement_error", None, None
    total = int(measurement["total_bytes"])
    available = int(measurement["available_bytes"])
    start, target = effective_pressure_thresholds(total, policy)
    if previous_state == "pressure":
        state = "normal" if available >= target else "pressure"
    else:
        state = "pressure" if available < start else "normal"
    return state, start, target


def collect_filesystem_capacity(
    data_root: Path,
    repository_root: Path,
    *,
    policy: dict,
    previous_states: dict[str, str] | None = None,
    measure_fd: Callable[..., dict] = _filesystem_measurement,
) -> dict:
    """Collect bounded O(1) capacity projections for workspace and repository."""
    canonical = validate_policy(policy)
    previous = previous_states or {}
    builds = _measure_builds_filesystem(Path(data_root), measure_fd=measure_fd)
    builds_state, builds_start, builds_target = pressure_state_for_measurement(
        previous.get("builds", "unknown"), builds, canonical,
    )
    builds["pressure_state"] = builds_state
    builds["effective_start_bytes"] = builds_start
    builds["effective_target_bytes"] = builds_target

    repository_measurement = _measure_repository_filesystem(
        Path(repository_root), measure_fd=measure_fd,
    )
    same_device = (
        builds.get("measurement_state") == "ready"
        and repository_measurement.get("measurement_state") == "ready"
        and repository_measurement.get("device_id") == builds.get("device_id")
    )
    if same_device:
        repository = {
            "measurement_state": "ready",
            "pressure_state": builds_state,
            "same_as_builds": True,
            "device_id": builds["device_id"],
        }
    else:
        repository = repository_measurement
        repository_state, repository_start, repository_target = pressure_state_for_measurement(
            previous.get("repository", "unknown"), repository, canonical,
        )
        repository.update({
            "pressure_state": repository_state,
            "effective_start_bytes": repository_start,
            "effective_target_bytes": repository_target,
            "same_as_builds": False,
        })
    return {"builds": builds, "repository": repository}


def _mount_points() -> set[Path]:
    points: set[Path] = set()
    lines = Path("/proc/self/mountinfo").read_text().splitlines()
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        value = fields[4]
        for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            value = value.replace(encoded, decoded)
        points.add(_absolute(Path(value)))
    return points


def _classify_data(relative: Path) -> str:
    parts = relative.parts
    if not parts:
        return "unknown"
    if parts[0] != "builds":
        if parts[0] == "toolchains":
            return "cache"
        if parts[0] in {"workflows", "settings.json", "packages.json", "secrets.json", "upstream-observations.json"}:
            return "metadata"
        return "unknown"
    if len(parts) < 3:
        return "unknown"
    run_parts = parts[2:]
    first = run_parts[0]
    if first in {"source", "staging", "downloads", "toolchain"} or first == "source.tar.gz":
        return "disposable"
    if first in {"logs", "manifests"}:
        return "logs_manifests"
    if first == "artifacts" and relative.suffix == ".deb":
        return "artifacts"
    if first == "validation" and len(run_parts) >= 3:
        if run_parts[-1] == "previous.deb" and len(run_parts) == 3:
            return "validation_previous"
        if run_parts[2] == "commands":
            return "logs_manifests"
    if len(run_parts) == 1 and first in RUN_METADATA_FILES:
        return "metadata"
    return "unknown"


class _Scan:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.categories = {name: 0 for name in CATEGORIES}
        self.run_bytes: dict[str, int] = {}
        self.run_artifact_counts: dict[str, int] = {}
        self.run_metadata: dict[str, dict] = {}
        self.hidden_runs: set[str] = set()
        self.run_revisions: dict[str, tuple[int, int, int, int]] = {}
        self.artifact_count = 0
        self.artifact_bytes = 0
        self.diagnostics: list[str] = []
        self.partial = False

    def diagnostic(self, message: str) -> None:
        self.partial = True
        if len(self.diagnostics) < MAX_DIAGNOSTICS:
            self.diagnostics.append(str(message)[:300])

    def account_data_file(self, relative: Path, size: int) -> None:
        category = _classify_data(relative)
        self.categories[category] += size
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == "builds":
            run_id = parts[1]
            self.run_bytes[run_id] = self.run_bytes.get(run_id, 0) + size
            if category == "artifacts":
                self.artifact_count += 1
                self.artifact_bytes += size
                self.run_artifact_counts[run_id] = self.run_artifact_counts.get(run_id, 0) + 1

    def account_unknown(self, relative: Path, size: int) -> None:
        self.categories["unknown"] += size
        parts = relative.parts
        if len(parts) >= 2 and parts[0] == "builds":
            run_id = parts[1]
            self.run_bytes[run_id] = self.run_bytes.get(run_id, 0) + size


def _read_run_metadata(data_root: Path, run_id: str, scan: _Scan) -> None:
    path = data_root / "builds" / run_id / "run.json"
    fd = -1
    try:
        with directory_fd(path.parent) as workspace_fd:
            fd = os.open(
                "run.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=workspace_fd,
            )
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("run.json is not a single regular file")
            with os.fdopen(fd) as handle:
                fd = -1
                run = json.load(handle)
            current = os.stat("run.json", dir_fd=workspace_fd, follow_symlinks=False)
        opened_revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        current_revision = (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
        )
        if current_revision != opened_revision or scan.run_revisions.get(run_id) != current_revision:
            scan.diagnostic(f"Run {run_id} changed while storage was measured")
        if not isinstance(run, dict) or str(run.get("id") or "") != run_id:
            raise ValueError("Run identity is invalid")
        run = validate_run(run)
        if Path(str(run.get("workspace") or "")) != path.parent:
            raise ValueError("Run workspace is not canonical")
        scan.run_metadata[run_id] = run
        if BuildStore(data_root / "builds").execution_history_deleted(run_id, run):
            scan.hidden_runs.add(run_id)
            return
        active = run.get("status") in {"pending", "queued", "running", "cancelling"}
        active = active or any(step.get("status") == "running" for step in run.get("steps") or [])
        from .validation_service import ACTIVE_STATUSES, list_attempts

        attempts = list_attempts(BuildStore(data_root / "builds"), run_id, strict=True)
        active = active or any(row.get("status") in ACTIVE_STATUSES for row in attempts)
        active = active or any(
            row.get("status") == "running"
            for key in ("publications",)
            for row in (run.get(key) or [])
            if isinstance(row, dict)
        )
        if active:
            scan.diagnostic(f"Run {run_id} was active while storage was measured")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        scan.diagnostic(f"Run {run_id} metadata is unavailable: {exc}")
    finally:
        if fd >= 0:
            os.close(fd)


def _scan_root(
    root: Path,
    *,
    scan: _Scan,
    data_root: bool,
    skip: Path | None = None,
    mounts: set[Path] | None = None,
) -> int | None:
    """Return logical file bytes below one pinned, non-symlink root."""
    root = _absolute(root)
    mounts = _mount_points() if mounts is None else mounts
    root_fd = -1
    try:
        with directory_fd(root) as pinned_fd:
            root_fd = os.dup(pinned_fd)
    except FileNotFoundError as exc:
        scan.diagnostic(f"Storage root {root} is unavailable: {exc}")
        return None
    except (OSError, ValueError) as exc:
        scan.diagnostic(f"Storage root {root} cannot be opened safely: {exc}")
        return None
    root_device = os.fstat(root_fd).st_dev
    total = 0

    def descend(fd: int, relative: Path) -> None:
        nonlocal total
        if data_root and len(relative.parts) == 2 and relative.parts[0] == "builds":
            run_id = relative.parts[1]
            try:
                revision = os.stat("run.json", dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(revision.st_mode):
                    raise ValueError("run.json is not a regular file")
                scan.run_revisions[run_id] = (
                    revision.st_dev,
                    revision.st_ino,
                    revision.st_size,
                    revision.st_mtime_ns,
                )
            except (OSError, ValueError) as exc:
                scan.diagnostic(f"Run {run_id} revision is unavailable: {exc}")
        try:
            with os.scandir(fd) as entries:
                rows = list(entries)
        except OSError as exc:
            scan.diagnostic(f"Cannot inspect {root / relative}: {exc}")
            return
        for entry in rows:
            child_relative = relative / entry.name
            child_path = root / child_relative
            if skip is not None and child_path == skip:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                scan.diagnostic(f"Cannot inspect {child_path}: {exc}")
                continue
            if stat.S_ISLNK(info.st_mode):
                size = max(0, int(info.st_size))
                total += size
                if data_root:
                    scan.account_unknown(child_relative, size)
                continue
            if stat.S_ISREG(info.st_mode):
                size = max(0, int(info.st_size))
                total += size
                if data_root:
                    scan.account_data_file(child_relative, size)
                continue
            if not stat.S_ISDIR(info.st_mode):
                size = max(0, int(info.st_size))
                total += size
                if data_root:
                    scan.categories["unknown"] += size
                scan.diagnostic(f"Unsupported filesystem entry {child_path}")
                continue
            if child_path in mounts or info.st_dev != root_device:
                scan.diagnostic(f"Nested mount boundary skipped at {child_path}")
                continue
            child_fd = -1
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                descend(child_fd, child_relative)
            except OSError as exc:
                scan.diagnostic(f"Cannot descend into {child_path}: {exc}")
            finally:
                if child_fd >= 0:
                    os.close(child_fd)

    try:
        descend(root_fd, Path())
    finally:
        os.close(root_fd)
    return total


def collect_storage_snapshot(
    data_root: Path,
    repository_root: Path,
    *,
    retention_policy: dict | None = None,
    filesystem_capacity: dict | None = None,
) -> dict:
    data = _absolute(Path(data_root))
    repository = _absolute(Path(repository_root))
    policy = validate_policy(retention_policy or DEFAULT_POLICY)
    filesystems = (
        collect_filesystem_capacity(data, repository, policy=policy)
        if filesystem_capacity is None
        else filesystem_capacity
    )
    repository_within_data = _within(repository, data)
    scan = _Scan(data)
    try:
        mounts = _mount_points()
    except OSError as exc:
        mounts = set()
        scan.diagnostic(f"Mount inventory is unavailable: {exc}")
    if repository == data:
        data_bytes = _scan_root(data, scan=scan, data_root=True, mounts=mounts)
        repository_bytes = data_bytes
    else:
        repository_bytes = _scan_root(
            repository, scan=scan, data_root=False, mounts=mounts,
        )
        data_without_repository = _scan_root(
            data,
            scan=scan,
            data_root=True,
            skip=repository if repository_within_data else None,
            mounts=mounts,
        )
        if data_without_repository is None or (repository_within_data and repository_bytes is None):
            data_bytes = None
        else:
            data_bytes = data_without_repository + (
                repository_bytes if repository_within_data else 0
            )
    if data_bytes is None or repository_bytes is None:
        managed_total = None
    else:
        managed_total = data_bytes if repository_within_data else data_bytes + repository_bytes
    for run_id in sorted(scan.run_bytes):
        _read_run_metadata(data, run_id, scan)

    by_mode: dict[str, int] = {}
    by_status: dict[str, int] = {}
    failed_count = 0
    test_count = 0
    pruned_artifact_count = 0
    pruned_artifact_bytes = 0
    visible_runs = {run_id: run for run_id, run in scan.run_metadata.items() if run_id not in scan.hidden_runs}
    for run in visible_runs.values():
        mode = str(run.get("mode") or "unknown")
        status = str(run.get("status") or "unknown")
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        failed_count += int(status in {"failed", "cancelled"})
        test_count += int(mode == "dry_run")
        pruned_size = storage_pruning.intentional_pruned_artifact_size(run)
        if pruned_size is not None and scan.run_artifact_counts.get(str(run.get("id") or ""), 0) == 0:
            pruned_artifact_count += 1
            pruned_artifact_bytes += pruned_size
    largest = [
        {
            "id": run_id,
            "bytes": size,
            "mode": str(scan.run_metadata.get(run_id, {}).get("mode") or "unknown"),
            "status": str(scan.run_metadata.get(run_id, {}).get("status") or "unknown"),
        }
        for run_id, size in sorted(
            ((run_id, scan.run_bytes.get(run_id, 0)) for run_id in visible_runs),
            key=lambda row: (-row[1], row[0]),
        )[:MAX_LARGEST_RUNS]
    ]
    measured_at = _utc_now()
    return {
        "state": "partial" if scan.partial else "ready",
        "measured_at": measured_at,
        "last_successful_measurement": None if scan.partial else measured_at,
        "partial": scan.partial,
        "diagnostics": scan.diagnostics,
        "roots": {
            "data_root": str(data),
            "repository_root": str(repository),
            "repository_within_data": repository_within_data,
        },
        "bytes": {
            "managed_total": managed_total,
            "data_root": data_bytes,
            "repository": repository_bytes,
        },
        "filesystems": filesystems,
        "categories": scan.categories,
        "runs": {
            "count": len(visible_runs),
            "by_mode": by_mode,
            "by_status": by_status,
            "failed_count": failed_count,
            "test_count": test_count,
            "artifact_count": scan.artifact_count,
            "artifact_bytes": scan.artifact_bytes,
            "pruned_artifact_count": pruned_artifact_count,
            "pruned_artifact_bytes": pruned_artifact_bytes,
            "largest": largest,
        },
        "retention_policy": {
            **policy,
            "scope": ["source", "staging", "downloads", "source.tar.gz", "toolchain"],
            "terminal_run_disposal_scope": ["toolchain"],
            "startup_destructive_cleanup": True,
            "periodic_destructive_cleanup": True,
            "cleanup_interval_seconds": MAINTENANCE_INTERVAL_SECONDS,
            "lifecycle_destructive_cleanup": True,
            "published_run_artifact_pruning": "exact_repository_proof_required",
            "artifact_manifests_preserved": True,
            "terminal_staging_manifests_pruned_without_retained_workspace_evidence": True,
            "validation_previous_preserved": True,
        },
    }


def _initial_snapshot() -> dict:
    return {
        "state": "collecting",
        "measured_at": None,
        "last_successful_measurement": None,
        "partial": True,
        "diagnostics": [],
        "roots": {"data_root": "", "repository_root": "", "repository_within_data": False},
        "bytes": {"managed_total": None, "data_root": None, "repository": None},
        "filesystems": _unknown_filesystems(),
        "categories": {name: None for name in CATEGORIES},
        "runs": {
            "count": None, "by_mode": {}, "by_status": {}, "failed_count": None,
            "test_count": None, "artifact_count": None, "artifact_bytes": None, "largest": [],
            "pruned_artifact_count": None, "pruned_artifact_bytes": None,
        },
        "retention_policy": {},
    }


class StorageInventory:
    """Hold the last completed snapshot without scanning from readers."""

    def __init__(
        self,
        data_root: Path,
        repository_root: Path,
        *,
        policy_provider: Callable[[], dict] | None = None,
        stale_after: float = 900,
    ):
        self.data_root = Path(data_root)
        self.repository_root = Path(repository_root)
        self.policy_provider = policy_provider or (lambda: dict(DEFAULT_POLICY))
        self.stale_after = float(stale_after)
        self._lock = threading.Lock()
        self._snapshot = _initial_snapshot()
        self._filesystems = _unknown_filesystems()
        self._pressure_states = {"builds": "unknown", "repository": "unknown"}
        self._pressure_policy: tuple[int, int, int, int] | None = None
        self._capacity_lock = threading.Lock()
        self._measured_monotonic: float | None = None
        self._collecting = False
        self._revision = 0
        self._completed_revision = 0

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1

    def needs_refresh(self) -> bool:
        with self._lock:
            return self._revision != self._completed_revision

    def snapshot(self) -> dict:
        with self._lock:
            result = deepcopy(self._snapshot)
            result["filesystems"] = deepcopy(self._filesystems)
            measured = self._measured_monotonic
            collecting = self._collecting
            pending = self._revision != self._completed_revision
        if collecting or pending:
            result["state"] = "collecting"
            result["partial"] = True
            result["bytes"] = {key: None for key in result["bytes"]}
            result["categories"] = {key: None for key in result["categories"]}
            result["runs"] = {**result["runs"], "count": None, "by_mode": {}, "by_status": {},
                              "failed_count": None, "test_count": None, "artifact_count": None,
                              "artifact_bytes": None, "largest": []}
        elif measured is not None and time.monotonic() - measured > self.stale_after:
            result["state"] = "stale"
            result["partial"] = True
        return result

    def collect_capacity(self, *, policy: dict | None = None) -> dict:
        """Refresh the O(1) filesystem view independently from recursive inventory."""
        canonical = validate_policy(policy or self.policy_provider())
        policy_identity = tuple(
            canonical[field]
            for field in (
                "pressure_minimum_free_bytes",
                "pressure_minimum_free_percent",
                "pressure_target_free_bytes",
                "pressure_target_free_percent",
            )
        )
        # Capacity refreshes are serialized independently from snapshot readers.
        # A threshold change deliberately starts from cold-state semantics.
        with self._capacity_lock:
            with self._lock:
                if self._pressure_policy == policy_identity:
                    previous_states = dict(self._pressure_states)
                else:
                    self._pressure_states = {
                        "builds": "unknown", "repository": "unknown",
                    }
                    previous_states = dict(self._pressure_states)
            filesystems = collect_filesystem_capacity(
                self.data_root,
                self.repository_root,
                policy=canonical,
                previous_states=previous_states,
            )
            with self._lock:
                self._filesystems = deepcopy(filesystems)
                self._pressure_policy = policy_identity
                for scope in ("builds", "repository"):
                    state = filesystems[scope].get("pressure_state")
                    if state in {"normal", "pressure"}:
                        self._pressure_states[scope] = state
                return deepcopy(filesystems)

    def collect(self) -> dict:
        with self._lock:
            self._collecting = True
            revision = self._revision
        try:
            policy = validate_policy(self.policy_provider())
            filesystems = self.collect_capacity(policy=policy)
            snapshot = collect_storage_snapshot(
                self.data_root,
                self.repository_root,
                retention_policy=policy,
                filesystem_capacity=filesystems,
            )
        except Exception as exc:
            with self._lock:
                previous = deepcopy(self._snapshot)
                previous["filesystems"] = deepcopy(self._filesystems)
                previous["state"] = "stale" if previous.get("measured_at") else "error"
                previous["partial"] = True
                previous["diagnostics"] = [f"Storage inventory failed: {exc}"[:300]]
                self._snapshot = previous
                self._collecting = False
                if revision == self._revision:
                    self._completed_revision = revision
                return deepcopy(previous)
        with self._lock:
            if snapshot.get("last_successful_measurement") is None:
                snapshot["last_successful_measurement"] = self._snapshot.get(
                    "last_successful_measurement"
                )
            self._snapshot = deepcopy(snapshot)
            self._measured_monotonic = time.monotonic()
            self._collecting = False
            if revision == self._revision:
                self._completed_revision = revision
            return deepcopy(snapshot)
