"""Canonical package state projections derived from Build Runs."""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

from . import apt_repo


BUILDABLE_PACKAGE_STATES = frozenset({
    "update_available", "build_available", "build_required", "not_published",
    "recipe_missing", "failed", "build_failed", "validation_failed",
})


def _event_epoch(value, fallback):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return fallback


def derive_lifecycle_status(build_status: str, validation_status: str = "not_run", publication_status: str = "not_run") -> str:
    """Return the canonical user-facing state of one real Build Run."""
    if build_status == "failed":
        return "build_failed"
    if build_status == "running":
        return "building"
    if build_status != "success":
        return build_status or "unknown"
    if validation_status == "running":
        return "validating"
    if validation_status == "failed":
        return "validation_failed"
    if validation_status != "success":
        return "validation_needed"
    if publication_status == "running":
        return "publishing"
    if publication_status == "failed":
        return "publication_failed"
    if publication_status == "success":
        return "published"
    return "ready_to_publish"


def allowed_actions(package_state: str, recipe_id: str, run: dict | None) -> dict[str, bool]:
    """Return the canonical actions allowed by package and latest Build Run facts."""
    run = run or {}
    artifact = run.get("artifact") or {}
    validation = (run.get("validations") or [{}])[-1]
    publication = (run.get("publications") or [{}])[-1]
    build_ready = run.get("mode") == "build" and run.get("status") == "success" and bool(artifact.get("path"))
    validation_status = validation.get("status", "not_run")
    publication_status = publication.get("status", "not_run")
    has_recipe = bool(recipe_id)
    return {
        "test": has_recipe,
        "build": has_recipe and package_state in BUILDABLE_PACKAGE_STATES,
        "validate": build_ready and validation_status != "running" and publication_status != "running",
        "publish": build_ready and validation_status == "success" and publication_status not in {"running", "success"},
    }


def summarize_runs(runs: list[dict], summary, *, include_history: bool = True) -> dict:
    """Select the Build Store facts used by package projections."""
    last_real = next((run for run in runs if run.get("mode") == "build"), None)
    last_dry = next((run for run in runs if run.get("mode") == "dry_run"), None)
    successful = next((run for run in runs if run.get("mode") == "build" and run.get("status") == "success" and (run.get("artifact") or {}).get("path")), None)
    resolved = next((run for run in runs if (run.get("version") or {}).get("upstream")), None)
    latest_validation = (last_real.get("validations") or [])[-1] if last_real and last_real.get("validations") else None
    latest_publication = (last_real.get("publications") or [])[-1] if last_real and last_real.get("publications") else None
    history = []
    if include_history:
        for run in runs:
            build = summary(run)
            history.append(build)
            for validation in run.get("validations", []):
                history.append({**build, "action": "validation", "status": validation.get("status", "unknown"), "updated": _event_epoch(validation.get("finished_at") or validation.get("started_at"), build.get("updated")), "event_id": validation.get("id", "")})
            for publication in run.get("publications", []):
                history.append({**build, "action": "publication", "status": publication.get("status", "unknown"), "version": publication.get("published_version") or build.get("version", ""), "updated": _event_epoch(publication.get("finished_at") or publication.get("requested_at"), build.get("updated")), "event_id": publication.get("id", "")})
    return {
        "last_real": summary(last_real) if last_real else None,
        "last_dry_run": summary(last_dry) if last_dry else None,
        "successful": successful,
        "resolved": resolved,
        "latest_validation": latest_validation,
        "latest_publication": latest_publication,
        "history": sorted(history, key=lambda row: row.get("updated") or 0, reverse=True),
    }


def compute_package_state(source_version: str = "", built_version: str = "", published_version: str = "", has_verified_build: bool = False, last_error: str = "", is_building: bool = False, candidate_is_newer: bool | None = None) -> str:
    if is_building:
        return "building"
    if last_error:
        return "failed"
    if has_verified_build and built_version and built_version != published_version and candidate_is_newer is not False:
        return "publication_available"
    if has_verified_build and source_version and built_version and published_version and candidate_is_newer is False:
        return "up_to_date"
    if source_version and not published_version:
        return "build_required"
    if source_version and published_version:
        if source_version == published_version:
            return "up_to_date"
        try:
            relation = apt_repo.upstream_version_relation(source_version, published_version, workspace=Path.cwd())["relation"]
        except (OSError, RuntimeError, ValueError):
            relation = "equal" if source_version == published_version else "unknown"
        if relation == "newer":
            return "update_available"
        if relation in {"equal", "older"}:
            return "up_to_date"
        return "unknown"
    if not published_version:
        return "build_required"
    return "unknown"


def infer_source_type(source: dict) -> str:
    typ = source.get("type") or "manual"
    repo = source.get("repository") or ""
    if typ == "github":
        if source.get("asset_pattern") or source.get("method") == "release_asset":
            return "github_release_asset"
        return "github"
    if repo:
        return typ
    return typ


def enrich_package(pkg: dict, published_version: str = "", source_version: str = "", built_version: str = "", has_verified_build: bool = False, state_source_version: str = "", candidate_is_newer: bool | None = None) -> dict:
    src = dict(pkg.get("source") or {"type": "manual"})
    displayed_source = source_version or pkg.get("upstream_version", "")
    state = compute_package_state(source_version=state_source_version or displayed_source, built_version=built_version, published_version=published_version or pkg.get("apt_version", ""), has_verified_build=has_verified_build, candidate_is_newer=candidate_is_newer)
    return {
        **pkg,
        "source": {
            "type": infer_source_type(src),
            "url": src.get("url") or (f"https://github.com/{src.get('repository')}" if src.get("repository") else ""),
            "repository": src.get("repository", ""),
            "default_branch": src.get("default_branch", ""),
            "ref_type": src.get("ref_type", "release" if src.get("repository") else "local"),
            "branch": src.get("branch", ""),
            "tag": src.get("tag", ""),
            "release": src.get("release", ""),
            "latest_release": src.get("latest_release", ""),
            "release_url": src.get("release_url", ""),
            "commit": src.get("commit", ""),
            "subdirectory": src.get("subdirectory", ""),
            "asset_pattern": src.get("asset_pattern", ""),
        },
        "version": {
            "source": displayed_source,
            "debian": pkg.get("debian_version") or built_version or pkg.get("apt_version", ""),
            "published": published_version or pkg.get("apt_version", ""),
            "candidate": built_version or "",
            "strategy": pkg.get("version_strategy", "manual"),
        },
        "build": {
            "method": pkg.get("build_method", "recipe" if pkg.get("recipe") else "manual"),
            "architecture": pkg.get("architecture", "all"),
            "last_build_id": (pkg.get("last_build") or {}).get("id", "") if isinstance(pkg.get("last_build"), dict) else "",
            "last_artifact": pkg.get("last_artifact", ""),
            "last_status": (pkg.get("last_build") or {}).get("status", "") if isinstance(pkg.get("last_build"), dict) else "",
            "state": state,
            "artifact_source": pkg.get("artifact_source", ""),
            "artifact_filename": pkg.get("artifact_filename", ""),
            "artifact_sha256": pkg.get("artifact_sha256", ""),
        },
        "repository": {
            "distribution": pkg.get("distribution", ""),
            "component": pkg.get("component", ""),
            "architectures": [pkg.get("architecture", "all")],
            "published": bool(published_version or pkg.get("apt_version")),
            "status": "published" if published_version or pkg.get("apt_version") else "not_published",
        },
        "lifecycle_state": state,
    }
