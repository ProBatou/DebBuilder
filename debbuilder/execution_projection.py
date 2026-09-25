"""Explicit v1 public projections for Build Run lifecycle state.

Durable Run documents are an internal persistence contract.  Every function in
this module constructs a new bounded response from named fields; adding a field
to run.json therefore never adds it to the HTTP API by accident.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PurePosixPath

from . import artifact_publication, package_store
from .notifications import redact
from .resource_limits import FIELDS as RESOURCE_LIMIT_FIELDS


MAX_TEXT = 500
MAX_ITEMS = 100
_SECRET_QUERY = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|client_secret|credential|signature|x-amz-|x-goog-)"
)
_JSON_SECRET_VALUE = re.compile(
    r'''(?i)(["']?(?:authorization|token|secret|password|passwd|api[_-]?key|client_secret|credential|signature)["']?\s*:\s*)(["'][^"']*["']|[^,\s}\]]+)'''
)
_CREDENTIAL_AUTHORITY = re.compile(
    r"(?i)\b[^\s/@:]+:[^\s/@]+@[a-z0-9.-]+(?::\d+)?(?:/[^\s<>\"']*)?"
)
_ANY_URL = re.compile(r"(?:https?|ssh|git|git\+https?|file)://[^\s<>\"']+", re.IGNORECASE)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![\w.])/(?:[^\s<>\"']+)")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)\b[a-z]:\\[^\s<>\"']+")


def safe_text(value, *, limit: int = MAX_TEXT) -> str:
    """Return bounded, secret-filtered text suitable for a public response."""
    if value is None:
        return ""
    text = str(redact(str(value)))
    text = re.sub(r"(?i)(authorization\s*:\s*)[^\s,;]+(?:\s+[^\s,;]+)?", r"\1[redacted]", text)
    text = _JSON_SECRET_VALUE.sub(r'\1"[redacted]"', text)
    # Redact credential-bearing and signed URLs even when embedded in a larger
    # diagnostic sentence.  Human-facing source links are projected separately.
    def clean_url(match: re.Match) -> str:
        raw = match.group(0)
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            return "[redacted-url]"
        if parsed.username or parsed.password or any(
            _SECRET_QUERY.search(key + item) for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        ):
            return "[redacted-url]"
        return raw

    text = re.sub(r"(?:https?|ssh|git|git\+https?)://[^\s<>\"']+", clean_url, text, flags=re.IGNORECASE)
    text = _CREDENTIAL_AUTHORITY.sub("[redacted-url]", text)
    return text[:limit]


def _safe_error_message(value) -> str:
    """Filter implementation locations in addition to ordinary secrets."""
    text = safe_text(value)
    text = _ANY_URL.sub("[redacted-url]", text)
    text = _WINDOWS_ABSOLUTE_PATH.sub("[redacted-path]", text)
    return _POSIX_ABSOLUTE_PATH.sub("[redacted-path]", text)


def _bounded_list(value, *, limit: int = MAX_ITEMS, text_limit: int = 240) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [safe_text(item, limit=text_limit) for item in value[:limit] if item is not None]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _identifier(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return safe_text(value, limit=128)


def _present(value) -> bool:
    return value is not None and value != ""


def _relative_path(value) -> str:
    text = safe_text(value, limit=500)
    if not text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()


def _relative_api_url(value) -> str:
    text = safe_text(value, limit=500)
    if not text or not text.startswith("/api/") or text.startswith("//"):
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return ""
    return text


def public_error(error) -> dict | None:
    """Bound an error without serializing implementation-specific details."""
    if not error:
        return None
    if not isinstance(error, dict):
        return {"code": "operation_failed", "stage": "", "message": _safe_error_message(error)}
    response = {
        "code": safe_text(error.get("code") or "operation_failed", limit=128),
        "stage": safe_text(error.get("stage"), limit=64),
        "message": _safe_error_message(error.get("message") or "The operation failed."),
    }
    suggested_action = _safe_error_message(error.get("suggested_action"))[:240]
    if suggested_action:
        response["suggested_action"] = suggested_action
    return response


def public_origin(origin) -> dict:
    origin = origin if isinstance(origin, dict) else {}
    return {
        "kind": safe_text(origin.get("kind") or "manual", limit=64),
        "trigger": safe_text(origin.get("trigger") or "manual", limit=80),
        "reason": safe_text(origin.get("reason"), limit=128) or None,
    }


def public_automation(automation) -> dict | None:
    if not isinstance(automation, dict):
        return None
    policy = safe_text(automation.get("policy"), limit=32)
    return {"policy": policy} if policy else None


def public_resource_limits(contract) -> dict:
    contract = contract if isinstance(contract, dict) else {}
    effective = contract.get("effective") if isinstance(contract.get("effective"), dict) else {}
    return {
        "effective": {field: _number(effective.get(field)) for field in RESOURCE_LIMIT_FIELDS},
        "enforcement_required": contract.get("enforcement_required") is True,
    }


def _public_inspection(inspection) -> dict:
    inspection = inspection if isinstance(inspection, dict) else {}
    result = {
        "ok": inspection.get("ok") is True,
        "package": safe_text(inspection.get("package"), limit=256),
        "version": safe_text(inspection.get("version"), limit=256),
        "architecture": safe_text(inspection.get("architecture"), limit=64),
        "installed_size": _number(inspection.get("installed_size")),
        "file_count": _number(inspection.get("file_count")),
        "depends": safe_text(inspection.get("depends")),
        "description": safe_text(inspection.get("description")),
    }
    return {key: value for key, value in result.items() if _present(value)}


def public_artifact(artifact) -> dict | None:
    if not isinstance(artifact, dict):
        return None
    name = safe_text(artifact.get("name"), limit=256)
    if not name and artifact.get("path"):
        name = safe_text(Path(str(artifact["path"])).name, limit=256)
    pruning = artifact.get("pruning") if isinstance(artifact.get("pruning"), dict) else None
    result = {
        "name": name,
        "size": _number(artifact.get("size")),
        "sha256": safe_text(artifact.get("sha256"), limit=128),
        "source": safe_text(artifact.get("source"), limit=64),
        "mode": safe_text(artifact.get("mode"), limit=64),
        "inspection": _public_inspection(artifact.get("inspection")),
        "available": bool(artifact.get("path")) and pruning is None,
        "pruning": ({
            "status": safe_text(pruning.get("status") or "pruned", limit=32),
            "pruned_at": safe_text(pruning.get("pruned_at"), limit=64) or None,
        } if pruning is not None else None),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_asset(asset) -> dict | None:
    if not isinstance(asset, dict):
        return None
    result = {
        "id": _identifier(asset.get("asset_id") if asset.get("asset_id") is not None else asset.get("id")),
        "name": safe_text(asset.get("name"), limit=256),
        "content_type": safe_text(asset.get("content_type"), limit=128),
        "size": _number(asset.get("download_size") if asset.get("download_size") is not None else asset.get("size") if asset.get("size") is not None else asset.get("declared_size")),
        "declared_size": _number(asset.get("declared_size")),
        "sha256": safe_text(asset.get("sha256"), limit=128),
        "expected_sha256": safe_text(asset.get("expected_sha256"), limit=128),
        "checksum_verified": asset.get("checksum_verified") is True,
        "payload_kind": safe_text(asset.get("payload_kind"), limit=64),
        "archive_format": safe_text(asset.get("archive_format"), limit=64),
        "file_count": _number(asset.get("file_count")),
        "source": safe_text(asset.get("source"), limit=64),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_archive_payload(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    allowed = (
        "mode", "selected_directories", "explicit_files", "selected_files",
        "excluded_directories", "excluded_files",
    )
    result = {}
    for key in allowed:
        value = payload.get(key)
        if key == "mode":
            value = safe_text(value, limit=64)
        else:
            value = _number(value)
        if _present(value):
            result[key] = value
    return result or None


def public_source(details) -> dict:
    details = details if isinstance(details, dict) else {}
    asset = details.get("asset") if isinstance(details.get("asset"), dict) else details.get("selected_asset")
    result = {
        "repository": safe_text(details.get("repository"), limit=300),
        "ref": safe_text(details.get("ref"), limit=300),
        "tag": safe_text(details.get("tag"), limit=300),
        "commit": safe_text(details.get("commit"), limit=128),
        "mode": safe_text(details.get("artifact_mode") or details.get("mode"), limit=64),
        "strategy": safe_text(details.get("strategy"), limit=64),
        "source": safe_text(details.get("archive_source") or details.get("source"), limit=64),
        "payload_kind": safe_text(details.get("payload_kind"), limit=64),
        "upstream_version": safe_text(details.get("upstream_version"), limit=256),
        "debian_version": safe_text(details.get("debian_version"), limit=256),
        "release_id": _identifier(details.get("release_id")),
        "file_count": _number(details.get("file_count")),
        "extracted_file_count": _number((details.get("extraction") or {}).get("files") if isinstance(details.get("extraction"), dict) else None),
        "asset": _public_asset(asset),
        "archive_payload": _public_archive_payload(details.get("archive_payload")),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_detection(details) -> dict:
    details = details if isinstance(details, dict) else {}
    selected = _public_asset(details.get("selected_asset"))
    result = {
        "project_type": safe_text(details.get("project_type"), limit=128),
        "display_name": safe_text(details.get("display_name"), limit=256),
        "detected_files": _bounded_list(details.get("detected_files"), limit=50),
        "build_tools": _bounded_list(details.get("build_tools"), limit=50),
        "warnings": _bounded_list(details.get("warnings"), limit=50),
        "payload_kind": safe_text(details.get("payload_kind"), limit=64),
        "file_count": _number(details.get("file_count")),
        "selected_asset": selected,
        "archive_payload": _public_archive_payload(details.get("archive_payload")),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_dependencies(details) -> dict:
    details = details if isinstance(details, dict) else {}
    result = {}
    for key in ("detected", "manually_added", "required", "available", "missing", "tools", "detected_tools", "missing_tools", "packages"):
        values = _bounded_list(details.get(key))
        if values:
            result[key] = values
    if isinstance(details.get("installation_attempted"), bool):
        result["installation_attempted"] = details["installation_attempted"]
    checks = []
    for row in (details.get("checks") or [])[:MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        checks.append({
            key: value for key, value in {
                "name": safe_text(row.get("name") or row.get("package") or row.get("tool"), limit=128),
                "status": safe_text(row.get("status"), limit=64),
                "available": row.get("available") if isinstance(row.get("available"), bool) else None,
                "requirement": safe_text(row.get("requirement"), limit=128),
            }.items() if _present(value)
        })
    if checks:
        result["checks"] = checks
    return result


def _public_change(row) -> dict:
    row = row if isinstance(row, dict) else {}
    result = {
        "index": _number(row.get("index")),
        "status": safe_text(row.get("status"), limit=32),
        "operation": safe_text(row.get("operation"), limit=64),
        "path": _relative_path(row.get("path")),
        "matches": _number(row.get("matches")),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_source_changes(details) -> dict:
    details = details if isinstance(details, dict) else {}
    result = {}
    applied = [_public_change(row) for row in (details.get("applied") or [])[:MAX_ITEMS] if isinstance(row, dict)]
    if applied:
        result["applied"] = applied
    failed = details.get("failed")
    if isinstance(failed, dict):
        result["failed"] = _public_change(failed)
    if _number(details.get("failed_index")) is not None:
        result["failed_index"] = details["failed_index"]
    return result


def _public_output(output) -> dict:
    output = output if isinstance(output, dict) else {}
    result = {"mode": safe_text(output.get("mode"), limit=64)}
    configured = _relative_path(output.get("configured_path"))
    if configured:
        result["configured_path"] = configured
    paths = []
    for row in (output.get("paths") or [])[:MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        path = _relative_path(row.get("configured_path"))
        if path:
            paths.append({"configured_path": path})
    if paths:
        result["paths"] = paths
    payload = _public_archive_payload(output.get("payload"))
    if payload:
        result["payload"] = payload
    return {key: value for key, value in result.items() if _present(value)}


def _public_build(details) -> dict:
    details = details if isinstance(details, dict) else {}
    plan = details.get("plan") if isinstance(details.get("plan"), dict) else {}
    configured_working_directory = _relative_path(plan.get("configured_working_directory"))
    environment = plan.get("environment") if isinstance(plan.get("environment"), dict) else {}
    environment_keys = _bounded_list(plan.get("environment_keys") or list(environment), limit=100, text_limit=128)
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    selection = plan.get("selection") if isinstance(plan.get("selection"), dict) else {}
    ensured = details.get("ensure_directories") if isinstance(details.get("ensure_directories"), dict) else {}
    public_plan = {
        "selection": {"source": safe_text(selection.get("source"), limit=64)} if selection.get("source") else {},
        "command_count": len(commands),
        "configured_working_directory": configured_working_directory,
        "environment_keys": environment_keys,
        "inactivity_timeout": _number(plan.get("inactivity_timeout")),
        "maximum_runtime": _number(plan.get("maximum_runtime")),
        "output": _public_output(plan.get("output")),
    }
    public_ensured = {
        key: value for key in ("requested", "created", "already_existed")
        if (value := _number(ensured.get(key))) is not None and value >= 0
    }
    return {
        "plan": {key: value for key, value in public_plan.items() if _present(value) and value != []},
        **({"ensure_directories": public_ensured} if public_ensured else {}),
    }


def _public_mapping(row) -> dict:
    row = row if isinstance(row, dict) else {}
    result = {
        "source": _relative_path(row.get("source")),
        "destination": safe_text(row.get("destination"), limit=500),
        "policy": safe_text(row.get("policy"), limit=64),
        "mode": safe_text(row.get("mode"), limit=32),
        "owner": safe_text(row.get("owner"), limit=128),
        "group": safe_text(row.get("group"), limit=128),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_systemd(details) -> dict:
    details = details if isinstance(details, dict) else {}
    result = {
        "configured": details.get("configured") is True,
        "name": safe_text(details.get("name"), limit=256),
        "path": safe_text(details.get("path"), limit=500),
    }
    return {key: value for key, value in result.items() if _present(value)}


def _public_staging(details) -> dict:
    details = details if isinstance(details, dict) else {}
    mappings = [_public_mapping(row) for row in (details.get("configurations") or [])[:MAX_ITEMS] if isinstance(row, dict)]
    directories = []
    for row in (details.get("directories") or [])[:MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        directories.append({key: value for key, value in {
            "path": safe_text(row.get("path"), limit=500),
            "owner": safe_text(row.get("owner"), limit=128),
            "group": safe_text(row.get("group"), limit=128),
            "mode": safe_text(row.get("mode"), limit=32),
        }.items() if _present(value)})
    result = {
        "version": safe_text(details.get("version"), limit=256),
        "content_available": details.get("content_available") if isinstance(details.get("content_available"), bool) else None,
        "content_file_count": _number(details.get("content_file_count")),
        "install_destination": safe_text(details.get("install_destination"), limit=500),
        "configurations": mappings,
        "directories": directories,
        "warnings": _bounded_list(details.get("warnings"), limit=50),
        "ownership": {key: safe_text((details.get("ownership") or {}).get(key), limit=128) for key in ("user", "group")} if isinstance(details.get("ownership"), dict) else {},
        "permissions": {key: safe_text((details.get("permissions") or {}).get(key), limit=32) for key in ("directories", "files")} if isinstance(details.get("permissions"), dict) else {},
        "account": {
            key: (safe_text(value, limit=128) if isinstance(value, str) else value)
            for key in ("user", "group", "create_user", "create_group")
            if isinstance((value := (details.get("account") or {}).get(key)), (str, bool))
        } if isinstance(details.get("account"), dict) else {},
        "systemd": _public_systemd(details.get("systemd")),
    }
    detection = details.get("runtime_dependency_detection")
    if isinstance(detection, dict):
        from .recipe_schema import DEBIAN_RELATION
        relations = detection.get("effective_depends")
        result["runtime_dependency_detection"] = {
            "status": detection.get("status") if detection.get("status") in {"disabled", "success"} else "unknown",
            "detected_count": min(len(detection.get("detected_packages") or []), 256),
            "bundled_count": _number(detection.get("bundled_requirements")),
            "unresolved_count": _number(detection.get("unresolved_requirements")),
            "overridden_count": _number(detection.get("overridden_requirements")),
            "effective_depends": [row for row in (relations or [])[:256] if isinstance(row, str) and DEBIAN_RELATION.fullmatch(row)],
        }
    return {key: value for key, value in result.items() if _present(value) and value != []}


def public_step(step) -> dict:
    step = step if isinstance(step, dict) else {}
    name = safe_text(step.get("name"), limit=64)
    details = step.get("details") if isinstance(step.get("details"), dict) else {}
    projectors = {
        "source": public_source,
        "detection": _public_detection,
        "dependencies": _public_dependencies,
        "source_changes": _public_source_changes,
        "build": _public_build,
        "staging": _public_staging,
        "debian_metadata": _public_staging,
        "systemd": _public_systemd,
        "artifact": public_artifact,
    }
    projected_details = projectors.get(name, lambda _value: {})(details)
    return {
        "name": name,
        "status": safe_text(step.get("status") or "pending", limit=32),
        "started_at": safe_text(step.get("started_at"), limit=64) or None,
        "finished_at": safe_text(step.get("finished_at"), limit=64) or None,
        "duration": _number(step.get("duration")),
        "summary": _safe_error_message(step.get("summary")),
        "error": public_error(step.get("error")),
        "details": projected_details or {},
    }


def public_validation(validation) -> dict:
    validation = validation if isinstance(validation, dict) else {}
    profile = validation.get("profile") if isinstance(validation.get("profile"), dict) else {}
    preparation = validation.get("dependency_preparation") if isinstance(validation.get("dependency_preparation"), dict) else {}
    checks = []
    for row in (validation.get("checks") or [])[:MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        checks.append({key: value for key, value in {
            "name": safe_text(row.get("name"), limit=128),
            "status": safe_text(row.get("status"), limit=32),
            "error": safe_text(row.get("error"), limit=240),
        }.items() if _present(value)})
    diagnostics = []
    for row in (validation.get("diagnostics") or [])[:MAX_ITEMS]:
        if not isinstance(row, dict):
            continue
        diagnostics.append({key: value for key, value in {
            "code": safe_text(row.get("code"), limit=128),
            "message": _safe_error_message(row.get("message")),
        }.items() if _present(value)})
    result = validation.get("result") if isinstance(validation.get("result"), dict) else None
    response = {
        "id": safe_text(validation.get("id") or validation.get("attempt_id"), limit=256),
        "attempt_id": safe_text(validation.get("attempt_id") or validation.get("id"), limit=256),
        "build_run_id": safe_text(validation.get("build_run_id"), limit=256),
        "status": safe_text(validation.get("status") or "unknown", limit=32),
        "phase": safe_text(validation.get("phase") or "lifecycle", limit=32),
        "created_at": safe_text(validation.get("created_at"), limit=64) or None,
        "started_at": safe_text(validation.get("started_at"), limit=64) or None,
        "finished_at": safe_text(validation.get("finished_at"), limit=64) or None,
        "profile": {"name": safe_text(profile.get("name"), limit=128)},
        "result": {"status": safe_text(result.get("status"), limit=32)} if result else None,
        "error": public_error(validation.get("error")),
        "checks": checks,
        "diagnostics": diagnostics,
        "dependency_preparation": {
            "status": safe_text(preparation.get("status"), limit=32),
            "package_count": _number(preparation.get("package_count")) or 0,
            "profile": safe_text(preparation.get("profile"), limit=128),
        },
        "cancellable": validation.get("cancellable") is True,
        "artifact_matches_run": validation.get("artifact_matches_run") is True,
        "status_url": _relative_api_url(validation.get("status_url")),
        "cancel_url": _relative_api_url(validation.get("cancel_url")),
    }
    blocker = validation.get("recovery_blocker")
    if isinstance(blocker, dict):
        response["recovery_blocker"] = {
            "code": safe_text(blocker.get("code"), limit=128),
            "message": safe_text(blocker.get("message"), limit=240),
        }
    return response


def public_publication(publication, *, run: dict | None = None) -> dict:
    publication = publication if isinstance(publication, dict) else {}
    repository = publication.get("repository") if isinstance(publication.get("repository"), dict) else {}
    readiness = publication.get("readiness") if isinstance(publication.get("readiness"), dict) else {}
    proof = artifact_publication.successful_publication_proof(publication, run=run)
    status = artifact_publication.publication_attempt_status(publication, run=run)
    proof_source = proof.get("source") if proof and isinstance(proof.get("source"), dict) else {}
    proof_distribution = proof.get("distribution") if proof and isinstance(proof.get("distribution"), dict) else {}
    proof_targets = proof.get("targets") if proof else []
    error = publication.get("error")
    if publication.get("status") == "success" and proof is None:
        error = {
            "code": "publication_proof_invalid",
            "message": "Publication success has no valid matching PublicationProofV1",
            "details": {},
        }
    return {
        "id": safe_text(publication.get("id"), limit=256),
        "type": safe_text(publication.get("type") or "publication", limit=64),
        "build_run_id": safe_text(publication.get("build_run_id"), limit=256),
        "status": safe_text(status, limit=32),
        "requested_at": safe_text(publication.get("requested_at"), limit=64) or None,
        "finished_at": safe_text(publication.get("finished_at"), limit=64) or None,
        "duration": _number(publication.get("duration")),
        "package": safe_text(publication.get("package"), limit=256),
        "version": safe_text(publication.get("version"), limit=256),
        "published_version": safe_text(publication.get("version"), limit=256) if status == "success" else "",
        "architecture": safe_text(publication.get("architecture"), limit=64),
        "repository": {
            "distribution": safe_text(repository.get("distribution"), limit=128),
            "component": safe_text(repository.get("component"), limit=128),
        },
        "readiness": {
            "ready": readiness.get("ready") is True,
            "reasons": _bounded_list(readiness.get("reasons"), limit=20, text_limit=128),
            "validation_id": safe_text(readiness.get("validation_id"), limit=256),
        },
        "proof": ({
            "available": True,
            "verified_at": safe_text(proof.get("verified_at"), limit=64) or None,
            "artifact": {
                "package": safe_text(proof.get("package"), limit=256),
                "version": safe_text(proof.get("version"), limit=256),
                "architecture": safe_text(proof.get("architecture"), limit=64),
                "size": _number(proof_source.get("size")),
                "sha256": safe_text(proof_source.get("sha256"), limit=128),
            },
            "target": {
                "distribution": safe_text(proof_distribution.get("requested"), limit=128),
                "component": safe_text(proof.get("component"), limit=128),
                "architectures": _bounded_list(
                    [target.get("database_architecture") for target in proof_targets if isinstance(target, dict)],
                    limit=64,
                    text_limit=64,
                ),
            },
        } if proof else {"available": False}),
        "error": public_error(error),
    }


def _source_step(run: dict) -> dict:
    return next((row for row in run.get("steps") or [] if isinstance(row, dict) and row.get("name") == "source"), {})


def public_summary(run: dict) -> dict:
    """Return the compact public Run contract used by every list/history view."""
    validations = [row for row in (run.get("_validation_attempts") or []) if isinstance(row, dict)]
    publications = [row for row in (run.get("publications") or []) if isinstance(row, dict)]
    validation_status = safe_text(validations[-1].get("status"), limit=32) if validations else "not_run"
    publication_status = (
        safe_text(artifact_publication.publication_attempt_status(publications[-1], run=run), limit=32)
        if publications else "not_run"
    )
    build_status = safe_text(run.get("status") or "pending", limit=32)
    lifecycle_status = package_store.derive_lifecycle_status(build_status, validation_status, publication_status)
    eligibility = run.get("publication_insertion_eligibility") if isinstance(run.get("publication_insertion_eligibility"), dict) else {
        "eligible": False, "reasons": ["current_validation_required"], "validation_id": "",
    }
    has_valid_publication = any(
        artifact_publication.successful_publication_proof(row, run=run) is not None
        for row in publications
    )
    already_published = run.get("already_published") is True and has_valid_publication
    reconciliation_available = run.get("publication_reconciliation_available") is True and has_valid_publication
    if already_published and publication_status == "success" and validation_status not in {"queued", "running", "cancelling"}:
        lifecycle_status = "published"
    elif lifecycle_status == "ready_to_publish" and eligibility.get("eligible") is not True:
        lifecycle_status = "validation_needed"
    actions = package_store.allowed_actions(lifecycle_status, str(run.get("recipe_id") or ""), run)
    version = run.get("version") if isinstance(run.get("version"), dict) else {}
    return {
        "id": safe_text(run.get("id"), limit=256),
        "run_id": safe_text(run.get("id"), limit=256),
        "recipe": safe_text(run.get("recipe_id"), limit=256),
        "recipe_id": safe_text(run.get("recipe_id"), limit=256),
        "package": safe_text(run.get("package") or run.get("recipe_id"), limit=256),
        "mode": safe_text(run.get("mode"), limit=32),
        "action": "dry-run" if run.get("mode") == "dry_run" else "build",
        "status": build_status,
        "build_status": build_status,
        "created_at": safe_text(run.get("created_at"), limit=64) or None,
        "started_at": safe_text(run.get("started_at"), limit=64) or None,
        "finished_at": safe_text(run.get("finished_at"), limit=64) or None,
        "updated": _number(run.get("created_at_epoch")),
        "duration": _number(run.get("duration")),
        "version": {
            "upstream": safe_text(version.get("upstream"), limit=256),
            "debian": safe_text(version.get("debian"), limit=256),
        },
        "origin": public_origin(run.get("origin")),
        "automation": public_automation(run.get("automation")),
        "validation_count": len(validations),
        "validation_status": validation_status,
        "publication_status": publication_status,
        "lifecycle_status": lifecycle_status,
        "lifecycle_active": build_status in {"pending", "queued", "running", "cancelling"} or validation_status in {"queued", "running", "cancelling"} or publication_status == "running",
        "ready_for_build": run.get("mode") == "dry_run" and build_status == "prepared",
        "publication_insertion_eligible": eligibility.get("eligible") is True,
        "publication_insertion_reasons": _bounded_list(eligibility.get("reasons"), limit=20, text_limit=128),
        "already_published": already_published,
        "publication_reconciliation_available": reconciliation_available,
        "allowed_actions": {"validate": actions["validate"], "publish": actions["publish"]},
    }


def public_detail(run: dict) -> dict:
    """Return the explicit full v1 Run DTO; unknown durable fields are ignored."""
    detail = public_summary(run)
    source_details = (_source_step(run).get("details") or {}) if isinstance(_source_step(run), dict) else {}
    detail.update({
        "artifact": public_artifact(run.get("artifact")),
        "source": public_source(source_details),
        "steps": [public_step(step) for step in (run.get("steps") or []) if isinstance(step, dict)][:MAX_ITEMS],
        "validations": [public_validation(row) for row in (run.get("_validation_attempts") or []) if isinstance(row, dict)][:MAX_ITEMS],
        "publications": [public_publication(row, run=run) for row in (run.get("publications") or []) if isinstance(row, dict)][:MAX_ITEMS],
        "error": public_error(run.get("error")),
        "resources": public_resource_limits(run.get("resource_limits")),
    })
    return detail
