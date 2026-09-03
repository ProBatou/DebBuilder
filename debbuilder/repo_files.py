"""Read-only HTTP serving for the public APT repository.

Only explicitly public repository paths are exposed.  reprepro's conf/ and db/
directories are intentionally unreachable through this helper.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

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
