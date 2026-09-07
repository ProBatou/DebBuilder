"""Strict, durable persistence for versioned Recipe documents."""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import storage
from .recipe_migrations import CURRENT_SCHEMA_VERSION, RecipeMigrationError, migrate_recipe_document
from .recipe_schema import RecipeDocumentError, recipe_document_for_storage


class RecipeStoreError(ValueError):
    """Structured failure for one persisted Recipe file."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file: Path,
        path: str = "$",
        source_version: int | None = None,
        target_version: int | None = CURRENT_SCHEMA_VERSION,
    ):
        super().__init__(message)
        self.code = code
        self.file = Path(file)
        self.path = path
        self.source_version = source_version
        self.target_version = target_version

    def as_dict(self) -> dict:
        result = {
            "code": self.code,
            "message": str(self),
            "file": str(self.file),
            "path": self.path,
        }
        if self.source_version is not None:
            result["source_version"] = self.source_version
        if self.target_version is not None:
            result["target_version"] = self.target_version
        return result


@dataclass(frozen=True)
class RecipeLoadResult:
    recipe: dict
    source_version: int
    target_version: int
    applied_migrations: tuple[str, ...]
    rewritten: bool


@dataclass(frozen=True)
class RecipeFileReport:
    file: str
    status: str
    source_version: int | None = None
    target_version: int | None = None
    applied_migrations: tuple[str, ...] = ()
    error: dict | None = None

    def as_dict(self) -> dict:
        result = {"file": self.file, "status": self.status}
        if self.source_version is not None:
            result["source_version"] = self.source_version
        if self.target_version is not None:
            result["target_version"] = self.target_version
        if self.applied_migrations:
            result["applied_migrations"] = list(self.applied_migrations)
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass(frozen=True)
class RecipeDirectoryMigrationReport:
    directory: str
    inspected: int
    current: int
    migrated: int
    failed: int
    files: tuple[RecipeFileReport, ...]

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict:
        return {
            "directory": self.directory,
            "ok": self.ok,
            "inspected": self.inspected,
            "current": self.current,
            "migrated": self.migrated,
            "failed": self.failed,
            "files": [row.as_dict() for row in self.files],
        }


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_number(value: str):
    raise ValueError(f"non-standard JSON number: {value}")


def _decode_recipe(path: Path, raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecipeStoreError(
            "invalid_recipe_encoding",
            "Recipe file is not valid UTF-8",
            file=path,
        ) from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except json.JSONDecodeError as exc:
        raise RecipeStoreError(
            "invalid_recipe_json",
            f"Recipe JSON syntax error at line {exc.lineno}, column {exc.colno}",
            file=path,
        ) from exc
    except (TypeError, ValueError) as exc:
        raise RecipeStoreError("invalid_recipe_json", str(exc), file=path) from exc
    if not isinstance(document, dict):
        raise RecipeStoreError("invalid_root", "Recipe JSON root must be an object", file=path)
    return document


def _canonical_bytes(recipe: dict) -> bytes:
    return (json.dumps(recipe, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_bytes(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RecipeStoreError(
                "unsafe_recipe_path",
                "Recipe files must not be symbolic links",
                file=path,
            )
        if not stat.S_ISREG(metadata.st_mode):
            raise RecipeStoreError(
                "unsafe_recipe_path",
                "Recipe path must identify a regular file",
                file=path,
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecipeStoreError(
                "unsafe_recipe_path",
                "Recipe path must identify a regular file",
                file=path,
            )
        with os.fdopen(descriptor, "rb") as recipe_file:
            descriptor = None
            return recipe_file.read()
    except RecipeStoreError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RecipeStoreError(
                "unsafe_recipe_path",
                "Recipe files must not be symbolic links",
                file=path,
            ) from exc
        raise RecipeStoreError("recipe_read_failed", f"Could not read Recipe file: {exc}", file=path) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _durable_atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace one Recipe, syncing both its data and directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    directory_fd: int | None = None
    try:
        existing_mode = None
        try:
            existing = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(existing.st_mode):
                raise RecipeStoreError(
                    "unsafe_recipe_path",
                    "Recipe files must not be symbolic links",
                    file=path,
                )
            if not stat.S_ISREG(existing.st_mode):
                raise RecipeStoreError(
                    "unsafe_recipe_path",
                    "Recipe path must identify a regular file",
                    file=path,
                )
            existing_mode = stat.S_IMODE(existing.st_mode)
        except FileNotFoundError:
            pass

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            if existing_mode is not None:
                os.fchmod(temporary.fileno(), existing_mode)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        os.fsync(directory_fd)
    except RecipeStoreError:
        raise
    except OSError as exc:
        raise RecipeStoreError("recipe_write_failed", f"Could not durably write Recipe file: {exc}", file=path) from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _document_error(path: Path, exc: RecipeDocumentError) -> RecipeStoreError:
    return RecipeStoreError(
        exc.code,
        str(exc),
        file=path,
        path=exc.path,
        source_version=exc.source_version,
        target_version=exc.target_version,
    )


def _migration_error(path: Path, exc: RecipeMigrationError) -> RecipeStoreError:
    return RecipeStoreError(
        exc.code,
        str(exc),
        file=path,
        path=exc.path,
        source_version=exc.source_version,
        target_version=exc.target_version,
    )


def load_recipe_result(path: Path, *, write_back: bool = True) -> RecipeLoadResult:
    """Strictly load one Recipe and optionally persist its canonical current form."""
    path = Path(path)
    with storage.locked_path(path):
        original = _read_bytes(path)
        document = _decode_recipe(path, original)
        try:
            migration = migrate_recipe_document(document)
            canonical = recipe_document_for_storage(migration.document)
        except RecipeMigrationError as exc:
            raise _migration_error(path, exc) from exc
        except RecipeDocumentError as exc:
            raise _document_error(path, exc) from exc
        canonical_bytes = _canonical_bytes(canonical)
        rewritten = write_back and original != canonical_bytes
        if rewritten:
            _durable_atomic_write(path, canonical_bytes)
        return RecipeLoadResult(
            recipe=canonical,
            source_version=migration.source_version,
            target_version=migration.target_version,
            applied_migrations=migration.applied_migrations,
            rewritten=rewritten,
        )


def load_recipe(path: Path, *, write_back: bool = True) -> dict:
    """Return one canonical current Recipe or raise a structured refusal."""
    return load_recipe_result(path, write_back=write_back).recipe


def save_recipe(path: Path, document: dict) -> dict:
    """Validate and durably save one Recipe in canonical current-schema form."""
    path = Path(path)
    try:
        canonical = recipe_document_for_storage(document)
    except RecipeDocumentError as exc:
        raise _document_error(path, exc) from exc
    if path.stem != canonical["name"]:
        raise RecipeStoreError(
            "recipe_identity_mismatch",
            "Recipe name must match its storage identifier",
            file=path,
            path="$.name",
        )
    content = _canonical_bytes(canonical)
    with storage.locked_path(path):
        try:
            path.lstat()
        except FileNotFoundError:
            existing = None
        else:
            existing = _read_bytes(path)
        if existing != content:
            _durable_atomic_write(path, content)
    return canonical


def migrate_recipe_directory(directory: Path) -> RecipeDirectoryMigrationReport:
    """Scan every Recipe JSON file deterministically and migrate safe entries."""
    directory = Path(directory)
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("Recipe directory is missing, not a directory, or a symbolic link")
        paths = sorted(directory.glob("*.json"), key=lambda candidate: candidate.name)
    except OSError as exc:
        raise RecipeStoreError(
            "recipe_directory_unavailable",
            f"Could not scan Recipe directory: {exc}",
            file=directory,
        ) from exc

    current = migrated = failed = 0
    rows: list[RecipeFileReport] = []
    for path in paths:
        try:
            result = load_recipe_result(path, write_back=True)
        except RecipeStoreError as exc:
            failed += 1
            rows.append(RecipeFileReport(file=path.name, status="failed", error=exc.as_dict()))
            continue
        status = "migrated" if result.rewritten else "current"
        if result.rewritten:
            migrated += 1
        else:
            current += 1
        rows.append(RecipeFileReport(
            file=path.name,
            status=status,
            source_version=result.source_version,
            target_version=result.target_version,
            applied_migrations=result.applied_migrations,
        ))
    return RecipeDirectoryMigrationReport(
        directory=str(directory),
        inspected=len(paths),
        current=current,
        migrated=migrated,
        failed=failed,
        files=tuple(rows),
    )
