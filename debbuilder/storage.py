"""Filesystem persistence primitives for workflows and executions."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def locked_path(path: Path):
    """Serialize read-modify-write operations for one process-local path."""
    with _path_lock(path):
        yield


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a text file atomically without sharing a predictable temp path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_path(path):
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        pass
    return default


def save_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def list_workflows(
    sources: Iterable[tuple[str, Path, bool]],
    read_workflow: Callable[[Path], dict],
) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for source, folder, writable in sources:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.stem in seen:
                continue
            try:
                workflow = read_workflow(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            seen.add(path.stem)
            items.append({
                "id": path.stem,
                "name": workflow.get("name", path.stem),
                "source": source,
                "writable": writable,
                "updated": path.stat().st_mtime,
            })
    return items


def workflow_path(
    workflow_id: str,
    user_dir: Path,
    read_dirs: Iterable[Path],
    validate_name: Callable[[str, str], str],
    *,
    for_write: bool = False,
) -> Path | None:
    validate_name(workflow_id, "workflow id")
    if for_write:
        return user_dir / f"{workflow_id}.json"
    for folder in read_dirs:
        path = folder / f"{workflow_id}.json"
        if path.exists():
            return path
    return None
