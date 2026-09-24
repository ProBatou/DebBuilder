"""Canonical, safe error responses for the admin API."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping


_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


@dataclass(frozen=True)
class ApiError:
    """Stable machine-readable API failure independent of HTTP transport."""

    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


_MESSAGES = {
    "ambiguous_archive_source": "Choose an archive source explicitly",
    "ambiguous_release_asset": "Multiple release assets match the Recipe",
    "application_shutting_down": "Application shutdown has started; durable mutations are closed",
    "authentication_required": "Authentication is required",
    "authentication_unavailable": "Authentication is unavailable",
    "automation_disabled": "Recipe automation is disabled",
    "github_unavailable": "GitHub is temporarily unavailable",
    "build_run_not_found": "Build Run was not found",
    "builtin_recipe_managed_field": "The managed Recipe field cannot be changed",
    "builtin_recipe_reserved": "The reserved built-in Recipe cannot be changed",
    "execution_active": "The execution is active",
    "execution_cancellation_failed": "Execution cancellation failed",
    "execution_manager_unavailable": "Execution management is unavailable",
    "execution_not_cancellable": "The execution cannot be cancelled in its current state",
    "forbidden": "The request is not permitted",
    "internal_error": "An internal error occurred",
    "invalid_automation_check_request": "Check now does not accept request fields",
    "invalid_automation_policy": "The automation policy is invalid",
    "invalid_cancellation_request": "Execution cancellation does not accept request fields",
    "invalid_execution_id": "The execution identifier is invalid",
    "invalid_json": "The request body is not valid JSON",
    "invalid_observation_refresh_request": "Upstream refresh does not accept request fields",
    "invalid_package_id": "The package identifier is invalid",
    "invalid_recipe_id": "The Recipe identifier is invalid",
    "invalid_recipe_json": "The Recipe document is invalid",
    "invalid_request": "The request is invalid",
    "invalid_resource_limit": "The resource limit is invalid",
    "invalid_secrets_json": "The secrets document is invalid",
    "invalid_support_bundle_request": "The support bundle request is invalid",
    "invalid_validation_cancellation_request": "Validation cancellation does not accept request fields",
    "invalid_validation_identity": "The validation identifier is invalid",
    "invalid_workflow_id": "The workflow identifier is invalid",
    "not_found": "The requested resource was not found",
    "notification_failed": "The test notification failed",
    "missing_required_secret": "A required secret is missing",
    "publication_identity_conflict": "Published repository state conflicts with this artifact",
    "repository_mutation_busy": "The repository is busy with another mutation",
    "publication_failed": "Publication failed",
    "readonly_recipe": "The read-only Recipe cannot be changed",
    "recipe_disabled": "The Recipe is disabled",
    "recipe_exists": "The Recipe already exists",
    "recipe_identity_mismatch": "The Recipe identity does not match the request path",
    "recipe_not_found": "The Recipe was not found",
    "recipe_inspection_unavailable": "The Recipe cannot be inspected safely",
    "run_inspection_unavailable": "The Run cannot be inspected safely",
    "release_asset_not_found": "No release asset matches the Recipe",
    "request_failed": "The request could not be completed",
    "settings_unavailable": "Application settings are unavailable",
    "support_bundle_unavailable": "The support bundle cannot be generated",
    "upstream_unavailable": "The upstream service is unavailable",
    "unknown_field": "The request contains an unknown field",
    "unknown_settings_field": "The request contains an unknown settings field",
    "unsupported_recipe_schema": "The Recipe schema version is unsupported",
    "unsupported_version_source": "The Recipe version source is unsupported",
    "validation_failed": "Validation failed",
}

# Typed subsystem codes that already cross the HTTP boundary. They remain in
# the inventory without replacing the current status-based safe message.
DOMAIN_ERROR_CODES = frozenset({
    "artifact_not_available", "automation_attempt_not_found", "automation_managed",
    "automation_retry_not_allowed",
    "automation_retry_stale", "execution_enqueue_failed", "execution_queue_full",
    "invalid_automation_configuration", "invalid_automation_retry_request",
    "invalid_validation_request", "publication_confirmation_required",
    "publication_proof_failed", "reprepro_include_failed",
    "recipe_changed_during_detection", "recipe_inactive",
    "automation_scheduler_unavailable", "validation_attempt_not_found",
    "validation_manager_unavailable", "validation_queue_full",
    "validation_recovery_required",
})

STABLE_ERROR_CODES = frozenset(_MESSAGES) | DOMAIN_ERROR_CODES

_STATUS_MESSAGES = {
    400: "The request is invalid",
    401: "Authentication is required",
    403: "The request is not permitted",
    404: "The requested resource was not found",
    409: "The request conflicts with current state",
    422: "Request validation failed",
    500: "An internal error occurred",
    502: "An upstream service is unavailable",
    503: "The service is unavailable",
}


def _stable_code(value: object, fallback: str) -> str:
    code = str(value or "")
    return code if _CODE.fullmatch(code) else fallback


def _fallback_code(status: int, path: str, error: object) -> str:
    text = error if isinstance(error, str) else ""
    if status == 401:
        return "authentication_required"
    if status == 403:
        return "forbidden"
    if status == 404:
        if "/api/recipes/" in path or "/api/workflows/" in path:
            return "recipe_not_found"
        if "/api/executions/" in path:
            return "build_run_not_found"
        return "not_found"
    if status == 409 and text == "recipe is disabled":
        return "recipe_disabled"
    if status == 502 and path == "/api/notifications/test":
        return "notification_failed"
    if status == 400 and path.startswith("/api/packages/"):
        return "invalid_package_id"
    if status == 400 and path.startswith("/api/executions/"):
        return "invalid_execution_id"
    if status == 400 and path.startswith("/api/workflows/"):
        return "invalid_workflow_id"
    return "invalid_request" if status in {400, 422} else "request_failed"


def _safe_details(error: object) -> dict[str, object]:
    if not isinstance(error, dict):
        return {}
    nested = error.get("details")
    source = nested if isinstance(nested, dict) else {}
    details: dict[str, object] = {}
    path = error.get("path", source.get("path"))
    if isinstance(path, str) and path.startswith("$") and len(path) <= 240:
        details["path"] = path
    classification = error.get("classification", source.get("classification"))
    if isinstance(classification, str) and _CODE.fullmatch(classification):
        details["classification"] = classification
    stage = error.get("stage", source.get("stage"))
    if isinstance(stage, str) and _CODE.fullmatch(stage):
        details["stage"] = stage
    if error.get("code") == "ambiguous_archive_source":
        sources = source.get("sources")
        if isinstance(sources, list):
            safe_sources = []
            for row in sources[:50]:
                if not isinstance(row, dict):
                    continue
                safe_row = {
                    key: value[:240]
                    for key in ("source", "name", "archive_format", "payload_kind")
                    if isinstance((value := row.get(key)), str)
                }
                size = row.get("size")
                if isinstance(size, int) and 0 <= size <= 2**63 - 1:
                    safe_row["size"] = size
                safe_sources.append(safe_row)
            details["sources"] = safe_sources
    return details


def canonical_error_payload(payload: object, status: int, path: str) -> dict[str, object]:
    """Convert any legacy API failure body into the canonical safe envelope."""
    if isinstance(payload, ApiError):
        error = payload
    else:
        body = payload if isinstance(payload, dict) else {}
        source = body.get("error")
        if not source and isinstance(body.get("publication"), dict):
            source = body["publication"].get("error")
        if not source and path == "/api/notifications/test":
            source = {"code": "notification_failed"}
        if not isinstance(source, dict) and body.get("code"):
            source = body
        source_dict = source if isinstance(source, dict) else {}
        code = _stable_code(source_dict.get("code"), _fallback_code(status, path, source))
        message = _MESSAGES.get(code, _STATUS_MESSAGES.get(status, "The request could not be completed"))
        error = ApiError(code=code, message=message, details=_safe_details(source_dict))
    return {"ok": False, "error": error.as_dict()}
