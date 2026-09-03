"""Content-based source modifications confined to an acquired source tree."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

OPERATIONS = {"replace", "insert_before", "insert_after", "remove", "create_file", "remove_file"}


class SourceChangeError(RuntimeError):
    def __init__(self, code: str, message: str, *, index: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.index = index
        self.details = details or {}


def _safe_target(source_root: str | Path, relative_path: str, *, allow_missing: bool) -> Path:
    root = Path(source_root).resolve()
    raw = Path(str(relative_path or ""))
    if not str(relative_path or "") or raw.is_absolute() or ".." in raw.parts:
        raise SourceChangeError("unsafe_source_path", f"Source change path must be relative and remain in the source workspace: {relative_path}")
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise SourceChangeError("unsafe_source_path", f"Source change path contains a symbolic link: {relative_path}")
    target = (root / raw).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SourceChangeError("unsafe_source_path", f"Source change path escapes the source workspace: {relative_path}") from exc
    if not allow_missing and not target.exists():
        raise SourceChangeError("source_file_not_found", f"Source file not found: {relative_path}")
    return target


def _read_text(target: Path, relative_path: str) -> str:
    if not target.is_file():
        raise SourceChangeError("source_file_not_found", f"Source file not found: {relative_path}")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SourceChangeError("source_not_text", f"Source file is not valid UTF-8 text: {relative_path}") from exc


def _atomic_write(target: Path, content: str, *, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, mode if mode is not None else 0o644)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def apply_change(source_root: str | Path, change: dict, *, index: int = 1) -> dict:
    operation = str(change.get("operation") or "")
    relative_path = str(change.get("path") or "")
    if operation not in OPERATIONS:
        raise SourceChangeError("unsupported_source_change", f"Unsupported source change operation: {operation}", index=index)
    target = _safe_target(source_root, relative_path, allow_missing=operation == "create_file")
    details = {"index": index, "operation": operation, "path": relative_path, "matches": None, "status": "applied"}
    try:
        if operation == "create_file":
            if target.exists():
                raise SourceChangeError("source_file_exists", f"Create file target already exists: {relative_path}")
            _atomic_write(target, str(change.get("content") or ""))
        elif operation == "remove_file":
            if not target.is_file():
                raise SourceChangeError("source_file_not_found", f"Source file not found: {relative_path}")
            target.unlink()
        else:
            search = change.get("search")
            if not isinstance(search, str) or not search:
                raise SourceChangeError("source_match_required", f"A non-empty search value is required for {operation}: {relative_path}")
            original = _read_text(target, relative_path)
            matches = original.count(search)
            details["matches"] = matches
            if matches == 0:
                raise SourceChangeError("source_match_not_found", f"Search content not found in {relative_path}")
            if matches > 1:
                raise SourceChangeError("source_match_ambiguous", f"Search content matched {matches} times in {relative_path}; exactly one match is required")
            content = str(change.get("content") or "")
            if operation == "replace":
                updated = original.replace(search, content, 1)
            elif operation == "insert_before":
                updated = original.replace(search, content + search, 1)
            elif operation == "insert_after":
                updated = original.replace(search, search + content, 1)
            else:
                updated = original.replace(search, "", 1)
            _atomic_write(target, updated, mode=target.stat().st_mode & 0o777)
        return details
    except SourceChangeError as exc:
        if exc.index is None:
            exc.index = index
        if not exc.details:
            exc.details = details
        raise


def apply_changes(source_root: str | Path, changes: list[dict], *, on_applied=None) -> dict:
    applied = []
    for index, change in enumerate(changes, 1):
        try:
            result = apply_change(source_root, change, index=index)
        except SourceChangeError as exc:
            if exc.index is None:
                exc.index = index
            exc.details = {"failed": exc.details, "applied": applied, "applied_count": len(applied), "requested": len(changes)}
            raise
        applied.append(result)
        if callable(on_applied):
            on_applied(result)
    return {"requested": len(changes), "applied_count": len(applied), "applied": applied}
