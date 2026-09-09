"""Read-only HTTP serving for the public APT repository.

Only explicitly public repository paths are exposed.  reprepro's conf/ and db/
directories are intentionally unreachable through this helper.
"""
from __future__ import annotations

import mimetypes
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .repository_lock import RepositoryLockError, pinned_directory, safe_relative_path

PUBLIC_ROOT_FILES = {"repository.gpg", "install.sh"}
PUBLIC_PREFIXES = ("dists/", "pool/")


def resolve_public_repo_file(repo_root: Path, request_path: str) -> Path | None:
    rel = request_path.lstrip("/")
    if rel not in PUBLIC_ROOT_FILES and not rel.startswith(PUBLIC_PREFIXES):
        return None
    root = repo_root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@contextmanager
def open_public_repo_file(repo_root: Path, request_path: str):
    """Open one allowlisted public file through a pinned, no-follow path walk."""
    rel = request_path.lstrip("/")
    if rel not in PUBLIC_ROOT_FILES and not rel.startswith(PUBLIC_PREFIXES):
        yield None
        return
    pinned = pinned_directory(repo_root)
    root_fd = parent_fd = file_fd = -1
    entered = False
    try:
        relative = safe_relative_path(rel)
        _root, root_fd = pinned.__enter__()
        entered = True
        parent_fd = os.dup(root_fd)
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child
        file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("public repository object is not a regular file")
    except (FileNotFoundError, OSError, RepositoryLockError):
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if entered:
            pinned.__exit__(None, None, None)
        yield None
        return
    try:
        yield file_fd, info, Path(relative.as_posix())
    finally:
        os.close(file_fd)
        os.close(parent_fd)
        pinned.__exit__(None, None, None)


def content_type(path: Path) -> str:
    if path.name in {"InRelease", "Release", "Release.gpg", "Packages"}:
        return "text/plain; charset=utf-8"
    if path.suffix == ".gz":
        return "application/gzip"
    if path.suffix == ".deb":
        return "application/vnd.debian.binary-package"
    if path.suffix == ".gpg":
        return "application/pgp-keys"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
