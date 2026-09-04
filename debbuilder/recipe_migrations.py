"""Narrow migrations for legacy fields still present in persisted Recipes."""
from __future__ import annotations

from copy import deepcopy


def migrate_legacy_recipe(workflow: dict) -> dict:
    """Translate known persisted v1-era fields into the canonical v1 shape.

    These cases are retained because current user Recipes still contain them.
    New callers must use the canonical nested fields directly.
    """
    migrated = deepcopy(workflow)

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
    if isinstance(artifact, dict) and artifact.get("mode") == "upstream_archive":
        has_selector = bool(artifact.get("asset_name") or artifact.get("name_pattern"))
        artifact.setdefault("archive_source", "release_asset" if has_selector else "auto")
        artifact.setdefault("asset_selection", "exact" if artifact.get("asset_name") else "pattern")

    return migrated
