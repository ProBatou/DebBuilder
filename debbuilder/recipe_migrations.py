"""Pure, sequential migrations for persisted Recipe documents."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable


CURRENT_SCHEMA_VERSION = 3


class RecipeMigrationError(ValueError):
    """Structured failure suitable for imports and future startup migration."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_version: int | None = None,
        target_version: int = CURRENT_SCHEMA_VERSION,
        path: str = "$.schema_version",
    ):
        super().__init__(message)
        self.code = code
        self.source_version = source_version
        self.target_version = target_version
        self.path = path


@dataclass(frozen=True)
class RecipeMigrationResult:
    document: dict
    source_version: int
    target_version: int
    applied_migrations: tuple[str, ...]

    @property
    def migrated(self) -> bool:
        return bool(self.applied_migrations)


def _document_version(document: dict) -> int:
    if "schema_version" not in document:
        return 0
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise RecipeMigrationError(
            "invalid_schema_version",
            "Recipe schema_version must be an integer",
        )
    if version < 0:
        raise RecipeMigrationError(
            "invalid_schema_version",
            "Recipe schema_version must not be negative",
            source_version=version,
        )
    return version


def _require_source(document: dict, expected: int) -> None:
    version = _document_version(document)
    if version != expected:
        raise RecipeMigrationError(
            "migration_source_mismatch",
            f"Recipe migration expected schema v{expected}, found v{version}",
            source_version=version,
            target_version=expected + 1,
        )


def migrate_v0_to_v1(document: dict) -> dict:
    """Give an unversioned/explicit-v0 document explicit v1 identity."""
    _require_source(document, 0)
    migrated = deepcopy(document)
    migrated["schema_version"] = 1
    return migrated


def migrate_v1_to_v2(document: dict) -> dict:
    """Canonicalize the deliberate historical v1 compatibility forms."""
    _require_source(document, 1)
    migrated = deepcopy(document)

    if "steps" in migrated:
        steps = migrated["steps"]
        if steps != []:
            raise RecipeMigrationError(
                "manual_recipe_migration_required",
                "A non-empty historical Recipe steps field requires manual review",
                source_version=1,
                path="$.steps",
            )
        migrated.pop("steps")

    build = migrated.get("build")
    if isinstance(build, dict) and "timeout" in build:
        build.setdefault("inactivity_timeout", build["timeout"])
        build.pop("timeout", None)

    install = migrated.get("install")
    if isinstance(install, dict) and "config_policy" in install:
        default_policy = str(install.pop("config_policy") or "dpkg_conffile")
        normalized = []
        for row in install.get("config_files") or []:
            if isinstance(row, str):
                normalized.append({"source": row.lstrip("/"), "destination": row, "policy": default_policy})
            elif isinstance(row, dict):
                normalized.append({**row, "policy": row.get("policy") or default_policy})
            else:
                normalized.append(row)
        install["config_files"] = normalized

    service = migrated.get("service")
    if isinstance(service, dict):
        service.pop("configured", None)

    artifact = migrated.get("artifact")
    if isinstance(artifact, dict):
        if "selected_files" in artifact and "payload" in artifact:
            raise RecipeMigrationError(
                "ambiguous_recipe_fields",
                "artifact must not contain both selected_files and payload",
                source_version=1,
                path="$.artifact",
            )
        if "selected_files" in artifact:
            selected_files = artifact.pop("selected_files")
            if artifact.get("mode") == "upstream_archive":
                if isinstance(selected_files, list):
                    selected_files = [row.strip() if isinstance(row, str) else row for row in selected_files]
                artifact["payload"] = {
                    "mode": "paths", "include": selected_files, "exclude": [],
                    "legacy_file_layout": "basename",
                }
            elif selected_files:
                raise RecipeMigrationError(
                    "legacy_field_not_applicable",
                    "artifact.selected_files is only valid for upstream_archive",
                    source_version=1,
                    path="$.artifact.selected_files",
                )
        if artifact.get("mode") == "upstream_archive":
            has_selector = bool(artifact.get("asset_name") or artifact.get("name_pattern"))
            artifact.setdefault("archive_source", "release_asset" if has_selector else "auto")
            artifact.setdefault("asset_selection", "exact" if artifact.get("asset_name") else "pattern")

    migrated["schema_version"] = 2
    return migrated


def migrate_v2_to_v3(document: dict) -> dict:
    """Add the explicit, host-independent per-command resource policy."""
    _require_source(document, 2)
    if "resource_limits" in document:
        raise RecipeMigrationError(
            "ambiguous_recipe_fields",
            "Recipe v2 unexpectedly contains resource_limits and cannot be migrated safely",
            source_version=2,
            path="$.resource_limits",
        )
    migrated = deepcopy(document)
    migrated["resource_limits"] = {
        "memory_max_bytes": None,
        "tasks_max": None,
        "cpu_quota_percent": None,
        "io_read_bandwidth_max_bytes_per_sec": None,
        "io_write_bandwidth_max_bytes_per_sec": None,
    }
    migrated["schema_version"] = 3
    return migrated


Migration = Callable[[dict], dict]
MIGRATIONS: dict[int, Migration] = {
    0: migrate_v0_to_v1,
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
}


def migrate_recipe_document(document: dict) -> RecipeMigrationResult:
    """Return a current-schema copy after applying every required step."""
    if not isinstance(document, dict):
        raise RecipeMigrationError(
            "invalid_root",
            "Recipe JSON root must be an object",
            path="$",
        )
    source_version = _document_version(document)
    if source_version > CURRENT_SCHEMA_VERSION:
        raise RecipeMigrationError(
            "future_schema_version",
            f"Recipe schema v{source_version} is newer than supported v{CURRENT_SCHEMA_VERSION}",
            source_version=source_version,
        )

    migrated = deepcopy(document)
    version = source_version
    applied: list[str] = []
    while version < CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RecipeMigrationError(
                "unsupported_schema_version",
                f"No Recipe migration is available from schema v{version} to v{version + 1}",
                source_version=version,
            )
        try:
            next_document = migration(migrated)
        except RecipeMigrationError:
            raise
        except (TypeError, ValueError) as exc:
            raise RecipeMigrationError(
                "recipe_migration_failed",
                f"Recipe migration from v{version} to v{version + 1} failed: {exc}",
                source_version=version,
                target_version=version + 1,
                path="$",
            ) from exc
        next_version = _document_version(next_document)
        if next_version != version + 1:
            raise RecipeMigrationError(
                "migration_target_mismatch",
                f"Recipe migration from v{version} did not produce schema v{version + 1}",
                source_version=version,
                target_version=version + 1,
            )
        applied.append(f"v{version}_to_v{next_version}")
        migrated = next_document
        version = next_version

    return RecipeMigrationResult(
        document=migrated,
        source_version=source_version,
        target_version=version,
        applied_migrations=tuple(applied),
    )
