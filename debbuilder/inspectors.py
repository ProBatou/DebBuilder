"""Versioned, transport-independent, allowlisted Recipe and Run inspections.

An inspection explains canonical state; it is deliberately not a durable JSON
dump, a log view, or a second business model.
"""
from __future__ import annotations

from datetime import datetime
import re

from . import artifact_publication, builtin_recipe
from .api_errors import STABLE_ERROR_CODES
from .archive_payload import PAYLOAD_MODES
from .build_models import RUN_STATUSES, STEP_NAMES, STEP_STATUSES, validate_run
from .recipe_schema import (
    ARCHIVE_ASSET_SELECTIONS, ARCHIVE_SOURCES, ARTIFACT_MODES,
    AUTOMATION_POLICIES, DEBIAN_RELATION, OUTPUT_MODES, RESTART_POLICIES, SAFE_ARCH,
    SERVICE_TYPES, SOURCE_ARCHIVE_FORMATS, VERSION_SOURCES,
    automation_eligible, validate_recipe_metadata,
)
from .validation_contracts import VALIDATION_STATUSES


RECIPE_INSPECTION_VERSION = 1
RUN_INSPECTION_VERSION = 1
MAX_RECIPE_BYTES = 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_VALIDATION_ATTEMPTS = 512
MAX_COLLECTION_COUNT = 10000
_ID = re.compile(r"[A-Za-z0-9_.+-]{1,128}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+.-]{0,63}\Z")
_GENERATED_ATTEMPT_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{6}-[0-9a-f]{4}\Z")
_SENSITIVE = re.compile(r"(?i)(ghp_|secret|token|password|passwd|credential|private|oidc|cookie|authorization)")
_RUN_ERROR_CODES = STABLE_ERROR_CODES | frozenset({
    "build_failed", "build_command_failed", "build_command_timeout",
    "execution_interrupted", "execution_worker_error", "execution_worker_fatal_error",
    "execution_cancelled", "execution_cancellation_termination_failed",
    "missing_build_tools", "missing_build_dependencies",
    "post_build_directory_invalid_path", "post_build_directory_escape",
    "post_build_directory_symlink", "post_build_directory_not_directory",
    "post_build_directory_creation_failed",
    "resolver_environment_incomplete", "resolver_environment_mismatch", "soname_unresolved",
    "resolver_mapping_ambiguous", "elf_architecture_mismatch", "malformed_elf",
    "invalid_dependency_override", "resolver_command_failed", "resolver_output_invalid",
})
PUBLICATION_INSPECTION_STATUSES = frozenset({"not_run", "running", "success", "failed", "cancelled"})


def _enum(value, choices, fallback="unknown") -> str:
    return value if isinstance(value, str) and value in choices else fallback


def _identifier(value) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) and not _SENSITIVE.search(value) else None


def _public_package(value) -> str | None:
    return value if isinstance(value, str) and _PACKAGE.fullmatch(value) and not _SENSITIVE.search(value) else None


def _digest(value) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _timestamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _count(value) -> int:
    return min(len(value), MAX_COLLECTION_COUNT) if isinstance(value, (list, tuple)) else 0


def inspect_recipe(recipe: dict, *, source: str = "user", observation: dict | None = None) -> dict:
    """Inspect one canonical Recipe without copying authored text or paths."""
    canonical = validate_recipe_metadata(recipe)
    package = canonical["package"]
    upstream = canonical["source"]
    build = canonical["build"]
    artifact = canonical["artifact"]
    install = canonical["install"]
    service = canonical["service"]
    automation = canonical["automation"]
    payload = artifact["payload"]
    management = canonical.get("management") or {}
    managed = builtin_recipe.is_builtin_recipe_id(canonical["name"]) and management.get("owner") == "application"
    detected = build.get("detected_project")
    if detected not in {"nodejs", "python", "rust", "static"}:
        detected = None
    observed = observation if isinstance(observation, dict) else {}
    attempt = observed.get("last_attempt") if isinstance(observed.get("last_attempt"), dict) else {}
    classification = attempt.get("classification")
    # Only this closed classification set is copied, never upstream display text.
    observation_state = _enum(classification, {
        "detected", "no_change", "rate_limited", "upstream_unavailable",
        "source_not_found", "explicit_asset_not_found", "ambiguous_asset",
        "unsupported_source", "incomplete_identity", "invalid_configuration",
        "manual_action_required", "recipe_changed", "capacity_exhausted",
        "automation_blocked", "shutting_down",
    }, "not_observed")
    collections = (build["commands"], build["output"].get("paths", []),
                   build.get("ensure_directories", []),
                   build["source_changes"], payload["include"], payload["exclude"],
                   install["directories"], install["config_files"])
    return {
        "schema_version": RECIPE_INSPECTION_VERSION,
        "counts_truncated": any(len(rows) > MAX_COLLECTION_COUNT for rows in collections),
        "identity": {
            "recipe_id": _identifier(canonical["name"]), "active": canonical["active"],
            "recipe_schema_version": canonical["schema_version"],
            "source": "builtin" if managed else _enum(source, {"user", "example"}),
            "managed": managed, "package": _public_package(package["name"]),
            "architecture": package["architecture"],
            "revision": package["version_revision"] if len(package["version_revision"]) <= 128 and
                        not _SENSITIVE.search(package["version_revision"]) else None,
        },
        "source": {
            "provider": _enum(upstream["provider"], {"github"}),
            "repository_configured": bool(upstream["repository"]),
            "tracking": _enum(upstream["tracking"], {"latest_release", "tag", "manual"}),
            "ref_configured": bool(upstream["ref"]),
            "version_source": _enum(upstream["version"]["source"], VERSION_SOURCES),
        },
        "build": {
            "detected_project": detected,
            "command_count": _count(build["commands"]),
            "output_mode": _enum(build["output"]["mode"], OUTPUT_MODES),
            "output_path_count": _count(build["output"].get("paths", [])) if build["output"]["mode"] == "paths" else
                                 1 if build["output"]["mode"] == "path" else 0,
            "ensure_directory_count": _count(build.get("ensure_directories", [])),
            "source_change_count": _count(build["source_changes"]),
            "inactivity_timeout_configured": build["inactivity_timeout"] is not None,
            "maximum_runtime_configured": build["maximum_runtime"] is not None,
        },
        "artifact": {
            "mode": _enum(artifact["mode"], ARTIFACT_MODES),
            "type": _enum(artifact["type"], {"deb", "archive", "tar.gz", "tgz", "tar.xz", "zip"}),
            "architecture": _enum(artifact["architecture"], SAFE_ARCH),
            "archive_source": _enum(artifact["archive_source"], ARCHIVE_SOURCES),
            "asset_selection": _enum(artifact["asset_selection"], ARCHIVE_ASSET_SELECTIONS),
            "archive_format": _enum(artifact["archive_format"], SOURCE_ARCHIVE_FORMATS),
            "payload_mode": _enum(payload["mode"], PAYLOAD_MODES),
            "include_count": _count(payload["include"]),
            "exclude_count": _count(payload["exclude"]),
        },
        "runtime_dependency_detection": {
            "enabled": package["runtime_dependency_detection"]["enabled"],
            "override_count": _count(package["runtime_dependency_detection"]["overrides"]),
        },
        "installation": {
            "content_source": _enum(install["content"]["source"], {"build_output", "configured_files"}),
            "destination_configured": bool(install["destination"]),
            "account_provisioning": bool(install["account"]["create_user"] or install["account"]["create_group"]),
            "directory_count": _count(install["directories"]),
            "config_mapping_count": _count(install["config_files"]),
            "maintainer_scripts_present": any(bool(value) for value in install["maintainer_scripts"].values()),
        },
        "service": {
            "configured": service["configured"], "enabled": service["enabled"],
            "type": _enum(service["type"], SERVICE_TYPES, "none"),
            "restart": _enum(service["restart"], RESTART_POLICIES, "none"),
        },
        "automation": {
            "enabled": automation["enabled"],
            "policy": _enum(automation["policy"], AUTOMATION_POLICIES),
            "eligible": not managed and automation_eligible(canonical),
        },
        "observation": {"classification": observation_state},
    }


def inspect_run(run: dict, *, validation: dict | None = None, validation_count: int = 0,
                validation_inventory_truncated: bool = False, cancellation_owned: bool = False) -> dict:
    """Inspect one validated Run and at most one already-loaded Validation attempt."""
    canonical = validate_run(run)
    status = canonical["status"]
    steps = canonical["steps"]
    visited = [row for row in steps if row["status"] != "pending"]
    current = next((row["name"] for row in steps if row["status"] == "running"), None)
    if current is None and visited:
        current = visited[-1]["name"]
    artifact = canonical.get("artifact") if isinstance(canonical.get("artifact"), dict) else {}
    inspection = artifact.get("inspection") if isinstance(artifact.get("inspection"), dict) else {}
    latest_validation = validation if isinstance(validation, dict) else {}
    publications = canonical.get("publications") if isinstance(canonical.get("publications"), list) else []
    publication = publications[-1] if publications and isinstance(publications[-1], dict) else {}
    publication_status = artifact_publication.publication_attempt_status(publication, run=canonical) if publication else "not_run"
    proof = artifact_publication.successful_publication_proof(publication, run=canonical) if publication else None
    recovery = canonical.get("recovery") if isinstance(canonical.get("recovery"), dict) else {}
    error = canonical.get("error") if isinstance(canonical.get("error"), dict) else {}
    code = error.get("code")
    code = code if isinstance(code, str) and _CODE.fullmatch(code) and code in _RUN_ERROR_CODES else "other" if error else None
    stage = error.get("stage")
    stage = stage if stage in STEP_NAMES else None
    pub_repository = publication.get("repository") if isinstance(publication.get("repository"), dict) else {}
    build_step = next((row for row in steps if row.get("name") == "build"), {})
    build_details = build_step.get("details") if isinstance(build_step.get("details"), dict) else {}
    ensured = build_details.get("ensure_directories") if isinstance(build_details.get("ensure_directories"), dict) else {}
    staging_step = next((row for row in steps if row.get("name") == "staging"), {})
    staging_details = staging_step.get("details") if isinstance(staging_step.get("details"), dict) else {}
    dependency_details = staging_details.get("runtime_dependency_detection") if isinstance(staging_details.get("runtime_dependency_detection"), dict) else {}

    def public_relations(value):
        if not isinstance(value, list):
            return []
        return [row for row in value[:256] if isinstance(row, str) and DEBIAN_RELATION.fullmatch(row)]

    def bounded_count(value) -> int:
        return min(value, MAX_COLLECTION_COUNT) if type(value) is int and value >= 0 else 0

    def public_token(value):
        return value if isinstance(value, str) and _TOKEN.fullmatch(value) and not _SENSITIVE.search(value) else None

    return {
        "schema_version": RUN_INSPECTION_VERSION,
        "identity": {
            "run_id": _identifier(canonical.get("id")),
            "recipe_id": _identifier(canonical.get("recipe_id")),
            "run_schema_version": canonical["schema_version"],
            "mode": _enum(canonical.get("mode"), {"build", "dry_run"}),
            "status": _enum(status, RUN_STATUSES),
            "terminal": status in {"prepared", "success", "failed", "cancelled"},
            "recipe_sha256": _digest(canonical.get("recipe_sha256")),
        },
        "lifecycle": {
            "created_at": _timestamp(canonical.get("created_at")),
            "started_at": _timestamp(canonical.get("started_at")),
            "finished_at": _timestamp(canonical.get("finished_at")),
            "current_stage": current,
            "steps": [{"id": row["name"], "status": _enum(row["status"], STEP_STATUSES),
                       "started_at": _timestamp(row.get("started_at")),
                       "finished_at": _timestamp(row.get("finished_at"))}
                      for row in steps[:len(STEP_NAMES)]],
            "step_count": len(steps), "steps_truncated": len(steps) > len(STEP_NAMES),
        },
        "artifact": {
            "available": bool(artifact.get("path")) and artifact.get("pruning") is None,
            "package": _public_package(inspection.get("package")),
            "architecture": _enum(inspection.get("architecture"), SAFE_ARCH),
            "size": artifact.get("size") if type(artifact.get("size")) is int and 0 <= artifact["size"] <= 2**53 - 1 else None,
            "sha256": _digest(artifact.get("sha256")),
        },
        "validation": {
            "attempt_count": min(validation_count, MAX_VALIDATION_ATTEMPTS),
            "inventory_truncated": validation_inventory_truncated,
            "status": _enum(latest_validation.get("status"), VALIDATION_STATUSES,
                            "unknown" if validation_count else "not_run"),
            "attempt_id": latest_validation.get("id") if isinstance(latest_validation.get("id"), str) and
                          _GENERATED_ATTEMPT_ID.fullmatch(latest_validation["id"]) else None,
            "recovery_blocked": bool(latest_validation.get("recovery_blocker")),
        },
        "publication": {
            "attempt_count": min(len(publications), MAX_COLLECTION_COUNT),
            "history_truncated": len(publications) > MAX_COLLECTION_COUNT,
            "status": _enum(publication_status, PUBLICATION_INSPECTION_STATUSES),
            "published": proof is not None,
            "proof_available": proof is not None,
            "suite": public_token(pub_repository.get("distribution")),
            "component": public_token(pub_repository.get("component")),
        },
        "execution": {
            "cancellable": bool(cancellation_owned and status in {"queued", "running"}),
            "recovery_status": _enum(recovery.get("status"), {"blocked", "resolved"}, "none"),
            "recovery_blocked": recovery.get("status") == "blocked",
            "containment_backend": _enum(recovery.get("backend"), {"systemd_cgroup", "process_group", "none", "unknown"}),
        },
        "build": {
            "ensure_directories": {
                "requested": bounded_count(ensured.get("requested")),
                "created": bounded_count(ensured.get("created")),
                "already_existed": bounded_count(ensured.get("already_existed")),
            },
        },
        "runtime_dependency_detection": {
            "status": _enum(dependency_details.get("status"), {"disabled", "success"}, "not_run"),
            "detected_count": _count(dependency_details.get("detected_packages")),
            "manual_count": _count(dependency_details.get("manual_packages")),
            "bundled_count": bounded_count(dependency_details.get("bundled_requirements")),
            "unresolved_count": bounded_count(dependency_details.get("unresolved_requirements")),
            "overridden_count": bounded_count(dependency_details.get("overridden_requirements")),
            "effective_depends": public_relations(dependency_details.get("effective_depends")),
        },
        "error": {"code": code, "stage": stage},
    }
