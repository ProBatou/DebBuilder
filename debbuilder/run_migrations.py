"""Pure, sequential migrations for persisted Build Run documents."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

from .resource_limits import historical_contract


CURRENT_RUN_SCHEMA_VERSION = 2


class RunMigrationError(ValueError):
    def __init__(self, code: str, message: str, *, source_version=None, target_version=CURRENT_RUN_SCHEMA_VERSION):
        super().__init__(message)
        self.code = code
        self.source_version = source_version
        self.target_version = target_version


@dataclass(frozen=True)
class RunMigrationResult:
    document: dict
    source_version: int
    target_version: int
    applied: tuple[int, ...]


def migrate_v1_to_v2(document: dict) -> dict:
    if document.get("schema_version") != 1:
        raise RunMigrationError("unsupported_run_schema_version", "Run v1 migration requires schema v1")
    migrated = copy.deepcopy(document)
    migrated["schema_version"] = 2
    migrated["resource_limits"] = historical_contract()
    return migrated


MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: migrate_v1_to_v2}


def migrate_run_document(document: dict) -> RunMigrationResult:
    if not isinstance(document, dict):
        raise RunMigrationError("invalid_run", "Run document must be an object")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RunMigrationError("invalid_run_schema_version", "Run schema_version must be a positive integer", source_version=version)
    if version > CURRENT_RUN_SCHEMA_VERSION:
        raise RunMigrationError(
            "future_run_schema_version",
            f"Run schema v{version} is newer than supported v{CURRENT_RUN_SCHEMA_VERSION}",
            source_version=version,
        )
    source, current, applied = version, copy.deepcopy(document), []
    while version < CURRENT_RUN_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RunMigrationError("unsupported_run_schema_version", f"No migration exists for Run schema v{version}", source_version=source)
        current = migration(current)
        applied.append(version)
        version = current.get("schema_version")
        if version != applied[-1] + 1:
            raise RunMigrationError("invalid_run_migration", "Run migration did not advance exactly one version", source_version=source)
    return RunMigrationResult(current, source, version, tuple(applied))
