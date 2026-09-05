"""Constrained workspace reclamation, independent from execution visibility."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .build_models import utc_now, validate_run
from .build_store import EXECUTION_HISTORY_DELETION_FILE as HISTORY_MARKER
from .recipe_schema import require_safe_name

DISPOSABLE_DIRECTORIES = ("source", "staging", "downloads")
DISPOSABLE_FILES = ("source.tar.gz",)
DEFAULT_POLICY = {"enabled": True, "failed_workspaces_to_retain": 5}
CLEANUP_MARKER = ".workspace-cleanup.json"


class WorkspaceBusyError(RuntimeError):
    """The execution owns its workspace or has not finished."""


@contextmanager
def directory_fd(path: Path):
    """Pin every directory component; never follow a symlink, including root."""
    absolute = Path(path).absolute()
    if ".." in absolute.parts or absolute == Path("/"):
        raise ValueError("Unsafe builds root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def locked_workspace(root: Path, run_id: str, *, blocking: bool = True):
    require_safe_name(run_id, "build run id")
    if run_id in {".", ".."}:
        raise ValueError("Unsafe build run id")
    with directory_fd(root) as root_fd:
        fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            lock = os.open(".workspace.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Unsafe workspace lock")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError as exc:
                    raise WorkspaceBusyError("Execution is active; deletion/cleanup is not cancellation") from exc
                yield fd
            finally:
                os.close(lock)
        finally:
            os.close(fd)


def read_json(fd: int, name: str):
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Unsafe workspace metadata: {name}")
        return json.load(handle)


def write_json(fd: int, name: str, value) -> None:
    """Atomic replacement relative to the pinned workspace, not runtime paths."""
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(file_fd, "w") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def read_run(fd: int, root: Path, run_id: str) -> dict:
    run = read_json(fd, "run.json")
    if not isinstance(run, dict):
        raise FileNotFoundError("Execution not found")
    validate_run(run)
    if run.get("id") != run_id or Path(str(run.get("workspace", ""))) != root.absolute() / run_id:
        raise ValueError("Run identity/workspace does not match the canonical builds root")
    return run


def require_finished(run: dict) -> None:
    from .build_pipeline import execution_summary
    if execution_summary(run)["lifecycle_active"] or any(step.get("status") == "running" for step in run["steps"]):
        raise WorkspaceBusyError("Execution is active; deletion/cleanup is not cancellation")


def _check_targets(fd: int, directories: tuple[str, ...], files: tuple[str, ...] = ()) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("Workspace cleanup requires symlink-safe rmtree")
    # rmtree does not follow symlinks, but would descend into a bind mount.
    # Refuse mounted targets/subtrees, including mounts on the same device.
    base = Path(os.readlink(f"/proc/self/fd/{fd}"))
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
        if any(mount.is_relative_to(base / name) for name in directories + files):
            raise ValueError("Mounted workspace cleanup target; cleanup refused")
    for name in directories + files:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        expected_type = stat.S_ISDIR if name in directories else stat.S_ISREG
        if not expected_type(info.st_mode) or info.st_dev != os.fstat(fd).st_dev:
            raise ValueError(f"Unsafe workspace cleanup target: {name}")


def _remove_targets(fd: int, directories: tuple[str, ...], files: tuple[str, ...] = ()) -> list[str]:
    removed = []
    for name in directories + files:
        try:
            if name in directories:
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
            removed.append(name)
        except FileNotFoundError:
            pass
    return removed


def _check_artifact(run: dict, *, extra_directories: tuple[str, ...] = ()) -> None:
    workspace = Path(run["workspace"])
    candidates = [run.get("artifact") or {}]
    for step in run["steps"]:
        details = step.get("details") or {}
        candidates.append(details.get("artifact") or {})
        if step["name"] == "artifact":
            candidates.append(details)
    for artifact in candidates:
        if not artifact.get("path"):
            continue
        path = Path(artifact["path"])
        resolved = (path if path.is_absolute() else workspace / path).resolve(strict=False)
        if any(resolved.is_relative_to(workspace / name) for name in DISPOSABLE_DIRECTORIES + extra_directories) or resolved == workspace / "source.tar.gz":
            raise ValueError("Final artifact is inside disposable workspace data; cleanup refused")


def _require_unused_workspace(workspace: Path) -> None:
    """Protect old failed Runs whose command descendants outlived the runner."""
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            references = [process / "cwd", process / "exe", *(process / "fd").iterdir()]
            for reference in references:
                try:
                    target = os.readlink(reference)
                except FileNotFoundError:  # A process or descriptor can disappear.
                    continue
                if target.startswith("/") and Path(target.removesuffix(" (deleted)")).is_relative_to(workspace):
                    raise WorkspaceBusyError("A process still uses this workspace; cleanup/deletion refused")
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise WorkspaceBusyError("Cannot verify whether a process still uses the workspace") from exc


def _clean_locked(fd: int, run: dict, *, reason: str) -> dict:
    require_finished(run)
    _check_artifact(run)
    _check_targets(fd, DISPOSABLE_DIRECTORIES, DISPOSABLE_FILES)
    _require_unused_workspace(Path(run["workspace"]))
    removed = _remove_targets(fd, DISPOSABLE_DIRECTORIES, DISPOSABLE_FILES)
    result = {"id": run["id"], "removed": removed, "reason": reason}
    if removed:
        write_json(fd, CLEANUP_MARKER, {**result, "cleaned_at": utc_now()})
    return result


def clean_workspace(store, run_id: str, *, reason: str = "manual") -> dict:
    with store.locked_run(run_id, blocking=False) as fd:
        run = read_run(fd, store.root, run_id)
        return _clean_locked(fd, run, reason=reason)


def _clear_output(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"stdout", "stderr"}:
                value[key] = ""
            else:
                _clear_output(child)
    elif isinstance(value, list):
        for child in value:
            _clear_output(child)


def delete_history(store, run_id: str) -> dict:
    with store.locked_run(run_id, blocking=False) as fd:
        run = read_run(fd, store.root, run_id)
        require_finished(run)
        marker = read_json(fd, HISTORY_MARKER)
        already_deleted = bool(marker or run.get("log_deleted"))
        _check_targets(fd, ("logs", "validation"))
        # Validate all targets before the first deletion. Validation command
        # output is history too; keep the previous artifact used for upgrades.
        validation_fds = []
        try:
            try:
                validation_fd = os.open("validation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                validation_fd = None
            if validation_fd is not None:
                try:
                    for name in os.listdir(validation_fd):
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=validation_fd)
                        validation_fds.append((name, child))
                        _check_targets(child, ("commands",))
                finally:
                    os.close(validation_fd)
            _check_artifact(run, extra_directories=("logs",) + tuple(f"validation/{name}/commands" for name, _child in validation_fds))
            cleanup = _clean_locked(fd, run, reason="history_deleted")
            removed = _remove_targets(fd, ("logs",))
            for name, child in validation_fds:
                removed.extend(f"validation/{name}/{target}" for target in _remove_targets(child, ("commands",)))
            os.mkdir("logs", mode=0o700, dir_fd=fd)
            logs_fd = os.open("logs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                os.mkdir("commands", mode=0o700, dir_fd=logs_fd)
            finally:
                os.close(logs_fd)
            _clear_output(run)
            run["events"] = []
            run["log_deleted"] = True
            write_json(fd, "run.json", run)
            if not marker:
                marker = {"run_id": run_id, "deleted_at": utc_now()}
                write_json(fd, HISTORY_MARKER, marker)
            return {"id": run_id, "deleted": "log_history", "removed": removed,
                    "already_deleted": already_deleted, "deleted_at": marker["deleted_at"], "workspace_cleanup": cleanup}
        finally:
            for _name, child in validation_fds:
                os.close(child)


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict):
        raise ValueError("workspace_cleanup settings must be an object")
    result = {**DEFAULT_POLICY, **policy}
    if type(result["enabled"]) is not bool:
        raise ValueError("workspace_cleanup.enabled must be a boolean")
    count = result["failed_workspaces_to_retain"]
    if type(count) is not int or not 0 <= count <= 1000:
        raise ValueError("failed_workspaces_to_retain must be an integer between 0 and 1000")
    return {key: result[key] for key in DEFAULT_POLICY}


def completed_history_ids(store) -> list[str]:
    """Manual clear uses the same confined read and workspace lease as deletion."""
    try:
        with directory_fd(store.root) as fd:
            ids = [entry.name for entry in os.scandir(fd) if entry.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return []
    selected = []
    for run_id in ids:
        try:
            with store.locked_run(run_id, blocking=False) as fd:
                run = read_run(fd, store.root, run_id)
                require_finished(run)
                if not (read_json(fd, HISTORY_MARKER) or run.get("log_deleted")):
                    selected.append(run_id)
        except (WorkspaceBusyError, FileNotFoundError):
            continue
    return selected


def _completion_time(run: dict) -> float:
    dates = [run.get("finished_at") or run.get("created_at")]
    dates.extend(attempt.get("finished_at") for key in ("validations", "publications") for attempt in (run.get(key) or []))
    return max(datetime.fromisoformat(date).timestamp() for date in dates if date)


def apply_retention(store, policy: dict | None = None) -> dict:
    policy = validate_policy(DEFAULT_POLICY if policy is None else policy)
    result = {"cleaned": [], "retained": [], "skipped": [], "errors": []}
    if not policy["enabled"]:
        return result
    try:
        with directory_fd(store.root) as fd:
            ids = [entry.name for entry in os.scandir(fd) if entry.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return result
    candidates = []
    for run_id in ids:
        try:
            with store.locked_run(run_id, blocking=False) as fd:
                run = read_run(fd, store.root, run_id)
                require_finished(run)
                entries = set(os.listdir(fd))
                if not entries.intersection(DISPOSABLE_DIRECTORIES + DISPOSABLE_FILES):
                    continue
                from .build_pipeline import execution_summary
                failed = execution_summary(run)["lifecycle_status"] in {"failed", "build_failed", "validation_failed", "publication_failed", "cancelled"}
                deleted = bool(read_json(fd, HISTORY_MARKER) or run.get("log_deleted"))
                revision = os.stat("run.json", dir_fd=fd, follow_symlinks=False).st_mtime_ns
                candidates.append((run_id, _completion_time(run), failed, deleted, revision))
        except (WorkspaceBusyError, FileNotFoundError):
            result["skipped"].append(run_id)
        except (OSError, ValueError) as exc:
            result["errors"].append({"id": run_id, "error": str(exc)})
    candidates.sort(key=lambda row: (row[1], row[0]), reverse=True)
    retained = 0
    for run_id, _date, failed, deleted, revision in candidates:
        if failed and not deleted and retained < policy["failed_workspaces_to_retain"]:
            result["retained"].append(run_id)
            retained += 1
            continue
        try:
            # Re-read while locked; a validation/publication may have begun
            # since the scan. The snapshot is never used to authorize deletion.
            with store.locked_run(run_id, blocking=False) as fd:
                run = read_run(fd, store.root, run_id)
                require_finished(run)
                if os.stat("run.json", dir_fd=fd, follow_symlinks=False).st_mtime_ns != revision:
                    result["skipped"].append(run_id)
                    continue
                current_failed = execution_summary(run)["lifecycle_status"] in {"failed", "build_failed", "validation_failed", "publication_failed", "cancelled"}
                if current_failed != failed:
                    result["skipped"].append(run_id)
                    continue
                cleanup = _clean_locked(fd, run, reason="retention")
                if cleanup["removed"]:
                    result["cleaned"].append(cleanup)
        except (WorkspaceBusyError, FileNotFoundError):
            result["skipped"].append(run_id)
        except (OSError, ValueError) as exc:
            result["errors"].append({"id": run_id, "error": str(exc)})
    return result
