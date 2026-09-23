"""Canonical archive payload selectors shared by Recipe and archive domains."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


PAYLOAD_MODES = {"paths", "entire_archive"}


@dataclass(frozen=True)
class ArchivePath:
    """One canonical logical path in the post-root-stripping archive namespace."""

    value: str
    parts: tuple[str, ...]
    is_directory: bool


def parse_archive_path(value: str) -> ArchivePath:
    """Parse a canonical file or recursive-directory selector without normalizing aliases."""
    if not isinstance(value, str) or not value:
        raise ValueError("archive payload path must be a non-empty string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("archive payload path must not contain control characters")
    if "\\" in value:
        raise ValueError("archive payload path must use POSIX separators")
    if value.startswith("/"):
        raise ValueError("archive payload path must be relative")
    if re.match(r"^[A-Za-z]:/", value):
        raise ValueError("archive payload path must not use a Windows drive prefix")

    is_directory = value.endswith("/")
    path = value[:-1] if is_directory else value
    if not path or path.endswith("/"):
        raise ValueError("archive payload path has a malformed directory marker")
    parts = tuple(path.split("/"))
    if any(not part for part in parts):
        raise ValueError("archive payload path must not contain repeated separators")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("archive payload path must not contain . or .. components")
    return ArchivePath(value=value, parts=parts, is_directory=is_directory)


def selector_matches(selector: str | ArchivePath, candidate: str | ArchivePath) -> bool:
    """Return whether a canonical selector includes the candidate path."""
    selected = selector if isinstance(selector, ArchivePath) else parse_archive_path(selector)
    path = candidate if isinstance(candidate, ArchivePath) else parse_archive_path(candidate)
    if not selected.is_directory:
        return not path.is_directory and selected.parts == path.parts
    if path.parts[:len(selected.parts)] != selected.parts:
        return False
    return len(path.parts) > len(selected.parts) or path.is_directory


def selectors_match(selectors: list[str], candidate: str | ArchivePath) -> bool:
    """Return whether any selector matches a canonical inventory path."""
    path = candidate if isinstance(candidate, ArchivePath) else parse_archive_path(candidate)
    return any(selector_matches(selector, path) for selector in selectors)


def _canonical_selectors(values, what: str) -> list[str]:
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError(f"{what} must be a list")

    parsed = [parse_archive_path(value) for value in values]
    kinds_by_parts: dict[tuple[str, ...], bool] = {}
    for path in parsed:
        previous = kinds_by_parts.setdefault(path.parts, path.is_directory)
        if previous != path.is_directory:
            raise ValueError(f"{what} contains conflicting file and directory selectors: {'/'.join(path.parts)}")

    unique: list[ArchivePath] = []
    seen = set()
    for path in parsed:
        if path.value not in seen:
            unique.append(path)
            seen.add(path.value)

    directories = {path.parts for path in unique if path.is_directory}
    retained = [
        path for path in unique
        if not any(path.parts[:depth] in directories for depth in range(1, len(path.parts)))
    ]
    retained.sort(key=lambda path: path.value)
    return [path.value for path in retained]


def normalize_archive_payload(value, *, require_include: bool = True) -> dict:
    """Return the canonical archive payload selection object."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("artifact.payload must be an object")

    mode = str(value.get("mode") or "paths")
    if mode not in PAYLOAD_MODES:
        raise ValueError("unsupported archive payload mode")
    include = _canonical_selectors(value.get("include"), "artifact.payload.include")
    exclude = _canonical_selectors(value.get("exclude"), "artifact.payload.exclude")
    if mode == "entire_archive":
        include = []
    elif require_include and not include:
        raise ValueError("paths archive payload requires at least one included path")
    return {
        "mode": mode,
        "include": include,
        "exclude": exclude,
    }
