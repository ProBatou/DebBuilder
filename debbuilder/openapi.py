"""Deterministic OpenAPI projection of the registered admin HTTP contract.

Routes, operation identities, authentication policy and effects come from
``api_routes``. Only representation details absent from that registry are
declared here. Business validation remains in the existing services.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import __version__
from .api_errors import STABLE_ERROR_CODES
from .api_routes import ADMIN_API_ROUTES, AuthenticationPolicy, RouteEffect
from .build_models import RUN_STATUSES
from .package_store import derive_lifecycle_status
from .recipe_schema import (
    ARCHIVE_SOURCES, AUTOMATION_POLICIES, OUTPUT_MODES, SAFE_ARCH,
    SCHEMA_VERSION, SOURCE_CHANGE_TYPES, VERSION_SOURCES,
)
from .system_diagnostics import CHECK_IDS, SCHEMA_VERSION_DIAGNOSTICS, STATUSES
from .validation_service import ACTIVE_STATUSES as ACTIVE_VALIDATION_STATUSES
from .validation_service import TERMINAL_STATUSES as TERMINAL_VALIDATION_STATUSES


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _object(properties: dict | None = None, required: tuple[str, ...] = (), *, extra=False, description="") -> dict:
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": extra}
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


S = {"type": "string"}
B = {"type": "boolean"}
I = {"type": "integer"}
STRINGS = {"type": "array", "items": S}
VALIDATION_STATUSES = ACTIVE_VALIDATION_STATUSES | TERMINAL_VALIDATION_STATUSES
PUBLICATION_STATUSES = ("not_run", "running", "success", "failed")
LIFECYCLE_STATUSES = sorted({
    derive_lifecycle_status(run, validation, publication)
    for run in RUN_STATUSES
    for validation in ("not_run", *sorted(VALIDATION_STATUSES))
    for publication in PUBLICATION_STATUSES
} | {"published"})


SCHEMAS = {
    "ApiError": _object({
        "ok": {"const": False},
        "error": _object({
            "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,127}$", "maxLength": 128},
            "message": {"type": "string", "minLength": 1, "maxLength": 240},
            "details": _object({
                "path": {"type": "string", "pattern": "^\\$", "maxLength": 240},
                "classification": {"type": "string", "maxLength": 128},
                "stage": {"type": "string", "maxLength": 128},
                "line": I, "column": I,
                "sources": {"type": "array", "items": _object({
                    "source": S, "name": S, "archive_format": S,
                    "payload_kind": S, "size": I,
                }), "maxItems": 50},
            }, description="Only allowlisted, bounded diagnostic fields are exposed."),
        }, ("code", "message", "details")),
    }, ("ok", "error")),
    "Recipe": _object({
        "schema_version": {"const": SCHEMA_VERSION},
        "name": {"type": "string", "pattern": "^[a-zA-Z0-9_.+-]+$"},
        "active": B,
        "package": _object({"name": S, "architecture": {"type": "string", "enum": sorted(SAFE_ARCH)},
                            "maintainer": S, "description": S, "runtime_dependencies": STRINGS}, extra=True),
        "source": _object({"provider": S, "repository": S, "tracking": S,
                           "version": _object({"source": {"type": "string", "enum": sorted(VERSION_SOURCES)},
                                               "expression": S}, extra=True)}, extra=True),
        "automation": _ref("AutomationPolicy"),
        "build": _object({"commands": {"type": "array", "items": S},
                          "environment": {"type": "object", "additionalProperties": S},
                          "output": _object({"mode": {"type": "string", "enum": sorted(OUTPUT_MODES)},
                                             "path": S, "paths": STRINGS}, extra=True),
                          "source_changes": {"type": "array", "items": _object({
                              "operation": {"type": "string", "enum": sorted(SOURCE_CHANGE_TYPES)},
                              "path": S, "search": S, "content": S,
                          }, extra=True)}}, extra=True),
        "install": _object({"destination": S, "config_files": {"type": "array", "items": _object(extra=True)},
                            "directories": {"type": "array", "items": _object(extra=True)}}, extra=True),
        "service": _object({"enabled": B, "name": S, "command": S}, extra=True),
        "resource_limits": {"type": "object", "description": "Resource policy validated by resource_limits.py."},
        "artifact": _object({"mode": {"type": "string", "enum": ["source_build", "upstream_deb", "upstream_archive"]},
                             "archive_source": {"type": "string", "enum": sorted(ARCHIVE_SOURCES)}}, extra=True),
    }, ("schema_version", "name"), extra=True,
        description="Authored Recipe v5. The complete nested business rules are enforced by recipe_schema.py; this schema documents stable public fields without claiming to replace that validator."),
    "AutomationPolicy": _object({"enabled": B, "policy": {"type": "string", "enum": sorted(AUTOMATION_POLICIES)}}, extra=False),
    "RecipeInput": {"oneOf": [_ref("Recipe"), _object({"recipe": _ref("Recipe")}, ("recipe",))],
                    "description": "Recipe directly or wrapped in a recipe property."},
    "RecipeImportInput": _object({"recipe": _ref("Recipe"), "replace": B}, ("recipe",)),
    "RunInput": {"oneOf": [_ref("Recipe"), _object({"workflow": _ref("Recipe"), "dry_run": B}, ("workflow",))]},
    "WorkflowSaveInput": {"oneOf": [_ref("Recipe"), _object({"workflow": _ref("Recipe"), "previous_id": S}, ("workflow",))]},
    "ArchiveInspectInput": {"oneOf": [_ref("Recipe"), _object({"workflow": _ref("Recipe")}, ("workflow",))]},
    "EmptyRequest": _object(description="An omitted JSON body is also accepted and treated as {}."),
    "IgnoredRequest": _object(extra=True, description="The handler currently ignores request fields; use {}."),
    "AutomationRetryInput": _object({"generation": I, "revision": {"type": "string", "maxLength": 200}},
                                     ("generation", "revision")),
    "ValidationStartInput": _object({"profile": {"type": "string", "maxLength": 128},
                                     "previous_artifact": {"type": "string", "maxLength": 4096}}),
    "PublicationInput": _object({"confirm": S}, ("confirm",)),
    "SettingsInput": _object({
        "general": _object({"app_name": S, "url": S}),
        "apt": _object({"repository": S, "distribution": S, "component": S, "architecture": S}),
        "notifications": _object({"type": S, "server_url": S, "topic": S, "token": {"type": "string", "writeOnly": True}}),
        "automation": _object({"auto_validate_after_successful_build": B,
                               "auto_publish_after_successful_validation": B,
                               "upstream_checks_enabled": B, "upstream_check_interval_seconds": I,
                               "upstream_check_concurrency": I}),
        "resource_limits": {"type": "object", "description": "Resource policy; validated by resource_limits.py."},
        "workspace_cleanup": {"type": "object", "description": "Retention policy; validated by workspace_cleanup.py."},
        "security": _object({"auth_mode": {"type": "string", "enum": ["none", "header", "oidc"]},
                             "oidc_issuer": S, "oidc_client_id": S, "oidc_redirect_uri": S,
                             "oidc_client_secret": {"type": "string", "writeOnly": True}}),
        "github": _object({"token": {"type": "string", "writeOnly": True}}),
    }, description="Partial settings update; secret replacements are write-only and never returned."),
    "PackageInput": _object({"name": S, "recipe": S, "status": S, "description": S,
                             "apt_version": S, "upstream_version": S,
                             "architecture": {"type": "string", "enum": sorted(SAFE_ARCH)},
                             "source": _object({"type": S, "repository": S}, extra=True)}, extra=True,
                            description="Package override fields are normalized by package_service.py."),
    "PackageCreateInput": {"allOf": [_ref("PackageInput"), {"required": ["name"]}]},
    "DeleteLogsInput": _object({"ids": STRINGS, "all": B, "dry_run": B}),
    "ExecutionLogDeletion": _object({"id": S, "deleted": {"const": "log_history"},
                                     "removed": STRINGS, "already_deleted": B,
                                     "deleted_at": S, "history_deleted": {"const": True},
                                     "visible": {"const": False}},
                                    ("id", "history_deleted", "visible"), extra=True),
    "Package": _object({"name": S, "status": S, "recipe": S, "version": _object(extra=True)},
                       ("name",), extra=True, description="Projection of Recipe, Run and APT repository state."),
    "Execution": _object({"id": S, "run_id": S, "recipe_id": S,
                          "status": {"type": "string", "enum": sorted(RUN_STATUSES)},
                          "lifecycle_status": {"type": "string", "enum": LIFECYCLE_STATUSES},
                          "validation_status": {"type": "string", "enum": ["not_run", *sorted(VALIDATION_STATUSES)]},
                          "publication_status": {"type": "string", "enum": list(PUBLICATION_STATUSES)},
                          "lifecycle_active": B,
                          "version": _object({"upstream": S, "debian": S}),
                          "allowed_actions": _object({"validate": B, "publish": B})},
                         ("id", "status", "lifecycle_status"), extra=True,
                         description="Public Build Run lifecycle projection; some fields depend on available evidence."),
    "Validation": _object({"attempt_id": S, "build_run_id": S,
                           "status": {"type": "string", "enum": sorted(VALIDATION_STATUSES)},
                           "phase": S, "cancellable": B}, extra=True,
                          description="Public validation attempt projection."),
    "Publication": _object({"status": {"const": "success"},
                           "build_run_id": S, "package": S, "version": S}, ("status",), extra=True,
                          description="Public publication attempt projection on successful HTTP responses."),
    "Automation": _object({"recipe_id": S, "recipe_active": B,
                           "automation": _ref("AutomationPolicy"), "eligible": B,
                           "state": S, "stage": S, "result": S,
                           "can_check_now": B, "can_retry": B,
                           "generation": {"type": ["integer", "null"]}, "revision": S},
                          ("recipe_id", "automation", "state"), extra=True,
                          description="Current Recipe automation status and scheduler evidence."),
    "AutomationAction": _object({"accepted": B, "created": B,
                                 "generation": I, "status": _ref("Automation")},
                                ("status",), extra=True,
                                description="Scheduler acceptance plus the updated Recipe automation status."),
    "Settings": _object({"general": _object({"app_name": S, "url": S}),
                         "apt": _object({"repository": S, "distribution": S,
                                         "component": S, "architecture": S}),
                         "github": _object({"token_configured": B}),
                         "notifications": _object({"type": S, "server_url": S,
                                                   "topic": S, "token_configured": B}),
                         "automation": _object({"upstream_checks_enabled": B,
                                                 "upstream_check_interval_seconds": I}),
                         "security": _object({"auth_mode": S,
                                              "oidc_client_secret_configured": B}),
                         "resource_limits_status": _object({"valid": B})},
                        extra=True, description="Redacted operator settings view; secret values are omitted."),
    "Storage": _object({"state": S}, extra=True, description="Cached storage inventory; GET does not collect or mutate it."),
    "ExecutionLog": _object({"text": S, "offset": I, "size": I,
                             "complete": B, "verbosity": S},
                            ("text", "offset", "size")),
    "Observation": _object(extra=True, description="Bounded upstream observation result."),
    "ArchiveInspection": _object(extra=True, description="Selected archive inventory and payload summary."),
    "Cancellation": _object({"status": {"type": "string", "enum": ["cancelled", "cancelling"]}}, extra=True),
    "Notification": _object(extra=True, description="Test notification result."),
    "DeleteLogsResult": {"oneOf": [
        _object({"count": I, "ids": STRINGS}, ("count", "ids")),
        _object({"deleted": {"type": "array", "items": _ref("ExecutionLogDeletion")},
                 "errors": {"type": "array", "items": _object({"id": S, "error": S}, ("id", "error"))}},
                ("deleted", "errors")),
    ], "description": "Deletion report or dry-run preview."},
}


def _wrapped(field: str, name: str, *, ok: bool = False) -> dict:
    properties = {field: _ref(name)}
    if ok:
        properties["ok"] = {"const": True}
    return _object(properties, (field,))


SCHEMAS.update({
    "SystemDiagnosticDetails": _object({
        "version": {"type": "string", "maxLength": 64},
        "recipe_schema_version": I, "run_schema_version": I,
        "python_version": {"type": "string", "maxLength": 32},
        "debian_architecture": {"type": "string", "maxLength": 32},
        "auth_mode": {"type": "string", "enum": ["none", "header", "oidc"]},
        "listener_active": B, "configuration_valid": B,
        "signed_release_present": B, "public_key_present": B,
        "signing_fingerprint": {"type": "string", "pattern": "^(?:[0-9A-F]{40}|[0-9A-F]{64})$"},
        "podman_installed": B, "runtime_verified": B, "admission_blocked": B,
        "open": B, "recovery_blocked": B,
        "backend": {"type": "string", "enum": ["systemd", "systemd_cgroup", "process_group", "unresolved", "unknown"]},
        "available": B, "running": B, "checks_enabled": B,
    }, description="Allowlisted scalar fields only. Fields vary by check; no paths, commands, exception text, or secrets."),
    "SystemDiagnosticCheck": _object({
        "id": {"type": "string", "enum": list(CHECK_IDS)},
        "status": {"type": "string", "enum": list(STATUSES)},
        "message": {"type": "string", "maxLength": 120},
        "details": _ref("SystemDiagnosticDetails"),
    }, ("id", "status", "message", "details")),
    "SystemDiagnosticsResponse": _object({
        "schema_version": {"const": SCHEMA_VERSION_DIAGNOSTICS},
        "status": {"type": "string", "enum": ["ok", "warning", "failed"]},
        "checks": {"type": "array", "items": _ref("SystemDiagnosticCheck"),
                   "minItems": len(CHECK_IDS), "maxItems": len(CHECK_IDS)},
    }, ("schema_version", "status", "checks"),
        description="Global failed if any check failed, otherwise warning if any warning/unknown, otherwise ok. Individual probe failures return unknown and HTTP 200."),
    "StatusResponse": _object({"ok": {"const": True}, "auth_mode": S,
                               "repo_default": S, "suite_default": S, "component_default": S,
                               "arch_default": S, "notification_type": S,
                               "workflow_dirs": _object({"examples": S, "user": S})},
                              ("ok", "auth_mode", "workflow_dirs")),
    "AuthStatusResponse": _object({"ok": {"const": True}, "auth_mode": S, "user": S}, ("ok", "auth_mode", "user")),
    "DashboardResponse": _object({"dashboard": _object(extra=True)}, ("dashboard",)),
    "PackagesResponse": _object({"packages": {"type": "array", "items": _ref("Package")}}, ("packages",)),
    "PackageResponse": _wrapped("package", "Package"),
    "PackageMutationResponse": _object({"ok": {"const": True}, "package": _ref("Package")}, ("ok", "package")),
    "RecipesResponse": _object({"recipes": {"type": "array", "items": _object(extra=True)}}, ("recipes",)),
    "AutomationResponse": _wrapped("automation", "Automation"),
    "AutomationActionResponse": _wrapped("automation", "AutomationAction", ok=True),
    "ExecutionsResponse": _object({"executions": {"type": "array", "items": _ref("Execution")}}, ("executions",)),
    "ExecutionResponse": _wrapped("execution", "Execution"),
    "ExecutionLogResponse": _wrapped("log", "ExecutionLog"),
    "ValidationResponse": _wrapped("validation", "Validation"),
    "ValidationCancellationResponse": _object({"ok": {"const": True}, "accepted": B,
                                               "validation": _ref("Validation")},
                                              ("ok", "accepted", "validation")),
    "SettingsResponse": _wrapped("settings", "Settings"),
    "SettingsMutationResponse": _object({"ok": {"const": True}, "settings": _ref("Settings")}, ("ok", "settings")),
    "StorageResponse": _wrapped("storage", "Storage"),
    "WorkflowListResponse": _object({"workflows": {"type": "array", "items": _object(extra=True)},
                                     "errors": {"type": "array", "items": _object(extra=True)}},
                                    ("workflows", "errors")),
    "ObservationResponse": _object({"ok": {"const": True}, "observation": _ref("Observation")}, ("ok", "observation")),
    "RecipeValidationResponse": _object({"ok": {"const": True}, "recipe": _ref("Recipe"), "id": S,
                                         "collision": {"oneOf": [_object(extra=True), {"type": "null"}]}},
                                        ("ok", "recipe", "id", "collision")),
    "RecipeImportResponse": _object({"ok": {"const": True}, "id": S, "recipe": _ref("Recipe"),
                                     "created": B, "replaced": B}, ("ok", "id", "recipe", "created", "replaced")),
    "RunAdmissionResponse": _object({"run_id": S, "status": {"const": "queued"}},
                                    ("run_id", "status")),
    "ArchiveInspectionResponse": _wrapped("inspection", "ArchiveInspection"),
    "PublicationResponse": _wrapped("publication", "Publication"),
    "NotificationResponse": _object({"ok": {"const": True}, "notification": _ref("Notification")}, ("ok", "notification")),
    "ExecutionCancellationResponse": _object({"ok": {"const": True}, "cancellation": _ref("Cancellation")}, ("ok", "cancellation")),
    "DeleteLogsResponse": _ref("DeleteLogsResult"),
    "WorkflowSaveResponse": _object({"ok": {"const": True}, "id": S, "path": S}, ("ok", "id", "path")),
    "DeleteResponse": _object({"ok": {"const": True}, "id": S,
                               "deleted_from_repository": B, "deleted_from_repo": B}, ("ok", "id")),
    "ExecutionLogDeleteResponse": _object({"ok": {"const": True}, "deletion": _ref("ExecutionLogDeletion")}, ("ok", "deletion")),
})


@dataclass(frozen=True)
class OperationDoc:
    success: tuple[tuple[int, str], ...]
    request: str | None = None
    errors: tuple[tuple[int, tuple[str, ...]], ...] = ()
    query: tuple[str, ...] = ()


def _doc(status: int, response: str, request: str | None = None, *,
         also: tuple[int, ...] = (), errors: dict[int, tuple[str, ...]] | None = None,
         query: tuple[str, ...] = ()) -> OperationDoc:
    return OperationDoc(((status, response), *((other, response) for other in also)),
                        request, tuple(sorted((errors or {}).items())), query)


# One operation-specific representation table. Paths, methods, IDs, tags,
# summaries, auth policy and effects are deliberately absent: the registry owns them.
OPERATION_DOCS = {
    "system.status": _doc(200, "StatusResponse"),
    "system.diagnostics": _doc(200, "SystemDiagnosticsResponse"),
    "system.openapi": _doc(200, "OpenApiDocument"),
    "auth.status": _doc(200, "AuthStatusResponse"),
    "dashboard.get": _doc(200, "DashboardResponse"),
    "packages.list": _doc(200, "PackagesResponse"),
    "packages.get": _doc(200, "PackageResponse", errors={400: ("invalid_package_id",), 404: ("not_found",)}),
    "recipes.list": _doc(200, "RecipesResponse"),
    "automation.get": _doc(200, "AutomationResponse", errors={400: ("invalid_recipe_id",), 404: ("recipe_not_found",), 422: ("invalid_automation_configuration",)}),
    "executions.list": _doc(200, "ExecutionsResponse"),
    "executions.get": _doc(200, "ExecutionResponse", errors={400: ("invalid_execution_id",), 404: ("build_run_not_found",)}),
    "executions.logs.get": _doc(200, "ExecutionLogResponse", query=("after", "verbosity"), errors={400: ("invalid_execution_id",), 404: ("build_run_not_found",)}),
    "validation.get": _doc(200, "ValidationResponse", errors={400: ("invalid_validation_identity",), 404: ("build_run_not_found", "validation_attempt_not_found")}),
    "settings.get": _doc(200, "SettingsResponse"),
    "storage.get": _doc(200, "StorageResponse"),
    "workflows.list": _doc(200, "WorkflowListResponse"),
    "workflows.get": _doc(200, "Recipe", errors={400: ("invalid_workflow_id",), 404: ("recipe_not_found",)}),
    "observations.refresh": _doc(200, "ObservationResponse", "EmptyRequest", errors={400: ("invalid_observation_refresh_request", "invalid_recipe_id"), 404: ("recipe_not_found",), 409: ("recipe_changed_during_detection",), 502: ("upstream_unavailable", "github_unavailable"), 503: ("github_unavailable",)}),
    "automation.check": _doc(202, "AutomationActionResponse", "EmptyRequest", errors={400: ("invalid_automation_check_request", "invalid_recipe_id"), 403: ("automation_managed",), 404: ("recipe_not_found",), 409: ("automation_disabled", "recipe_inactive"), 503: ("automation_scheduler_unavailable",)}),
    "automation.retry": _doc(202, "AutomationActionResponse", "AutomationRetryInput", errors={400: ("invalid_recipe_id", "invalid_automation_retry_request"), 404: ("recipe_not_found", "automation_attempt_not_found"), 409: ("automation_disabled", "automation_retry_not_allowed", "automation_retry_stale")}),
    "validation.cancel": _doc(200, "ValidationCancellationResponse", "EmptyRequest", also=(202,), errors={400: ("invalid_validation_cancellation_request", "invalid_validation_identity"), 404: ("build_run_not_found",), 409: ("validation_recovery_required",)}),
    "executions.cancel": _doc(200, "ExecutionCancellationResponse", "EmptyRequest", also=(202,), errors={400: ("invalid_cancellation_request", "invalid_execution_id"), 404: ("build_run_not_found",), 409: ("execution_not_cancellable",), 500: ("execution_cancellation_failed",), 503: ("execution_manager_unavailable",)}),
    "recipes.validate": _doc(200, "RecipeValidationResponse", "RecipeInput", errors={422: ("invalid_recipe_json", "unknown_field", "unsupported_recipe_schema", "unsupported_version_source", "invalid_automation_policy")}),
    "recipes.import": _doc(200, "RecipeImportResponse", "RecipeImportInput", errors={403: ("readonly_recipe",), 409: ("recipe_exists", "builtin_recipe_reserved"), 422: ("invalid_recipe_json", "unsupported_recipe_schema", "unsupported_version_source", "unknown_field")}),
    "executions.run": _doc(202, "RunAdmissionResponse", "RunInput", errors={400: ("invalid_request",), 409: ("recipe_disabled",), 422: ("unsupported_recipe_schema", "unsupported_version_source", "unknown_field"), 429: ("execution_queue_full",), 500: ("execution_enqueue_failed",), 503: ("execution_manager_unavailable", "github_unavailable")}),
    "archives.inspect": _doc(200, "ArchiveInspectionResponse", "ArchiveInspectInput", errors={422: ("invalid_recipe_json", "ambiguous_archive_source", "ambiguous_release_asset", "release_asset_not_found", "github_unavailable")}),
    "validation.start": _doc(202, "ValidationResponse", "ValidationStartInput", errors={400: ("invalid_validation_request",), 404: ("build_run_not_found",), 409: ("artifact_not_available",), 429: ("validation_queue_full",), 503: ("validation_manager_unavailable",)}),
    "publication.publish": _doc(200, "PublicationResponse", "PublicationInput", errors={400: ("publication_confirmation_required", "build_run_not_found", "artifact_not_available", "publication_identity_conflict"), 409: ("repository_mutation_busy", "publication_identity_conflict"), 422: ("publication_proof_failed", "reprepro_include_failed")}),
    "publication.reconcile": _doc(200, "PublicationResponse", "IgnoredRequest", errors={409: ("repository_mutation_busy", "publication_identity_conflict"), 422: ("publication_proof_failed",)}),
    "notifications.test": _doc(200, "NotificationResponse", "IgnoredRequest", errors={502: ("notification_failed",)}),
    "settings.update": _doc(200, "SettingsMutationResponse", "SettingsInput", errors={422: ("unknown_settings_field", "invalid_resource_limit", "missing_required_secret", "invalid_secrets_json")}),
    "executions.logs.delete_many": _doc(200, "DeleteLogsResponse", "DeleteLogsInput", errors={409: ("execution_active",)}),
    "packages.create": _doc(200, "PackageMutationResponse", "PackageCreateInput", errors={400: ("invalid_request",)}),
    "packages.update": _doc(200, "PackageMutationResponse", "PackageInput", errors={400: ("invalid_package_id",), 404: ("not_found",)}),
    "workflows.save": _doc(200, "WorkflowSaveResponse", "WorkflowSaveInput", errors={403: ("forbidden",), 409: ("builtin_recipe_managed_field", "builtin_recipe_reserved"), 422: ("invalid_recipe_json", "recipe_identity_mismatch", "unsupported_version_source", "unknown_field")}),
    "workflows.delete": _doc(200, "DeleteResponse", errors={403: ("forbidden", "readonly_recipe"), 404: ("recipe_not_found",)}),
    "executions.logs.delete": _doc(200, "ExecutionLogDeleteResponse", errors={404: ("build_run_not_found",), 409: ("execution_active",)}),
    "packages.delete": _doc(200, "DeleteResponse", errors={400: ("invalid_request",)}),
}


SCHEMAS["OpenApiDocument"] = _object({"openapi": S, "info": _object(extra=True),
                                       "paths": _object(extra=True), "components": _object(extra=True)},
                                      ("openapi", "info", "paths", "components"),
                                      description="This OpenAPI 3.1 document.")


_PATH_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_QUERY_PARAMETERS = {
    "after": {"name": "after", "in": "query", "required": False,
              "schema": {"type": "integer", "default": 0},
              "description": "Log offset; negative values are clamped to zero."},
    "verbosity": {"name": "verbosity", "in": "query", "required": False,
                  "schema": {"type": "string", "enum": ["compact", "normal", "verbose", "raw"], "default": "normal"},
                  "description": "Unknown values fall back to normal in the current runtime."},
}


def _error_response(status: int, codes: tuple[str, ...]) -> dict:
    unknown = set(codes) - STABLE_ERROR_CODES
    if unknown:
        raise ValueError(f"OpenAPI references unregistered API error codes: {sorted(unknown)}")
    return {
        "description": f"HTTP {status} admin API error. Documented codes: {', '.join(codes)}.",
        "content": {"application/json": {"schema": _ref("ApiError")}},
        "x-debbuilder-error-codes": list(codes),
    }


def _operation(route) -> dict:
    doc = OPERATION_DOCS[route.operation_id]
    responses = {
        str(status): {"description": "Successful response", "content": {"application/json": {"schema": _ref(schema)}}}
        for status, schema in doc.success
    }
    common_errors = {401: ("authentication_required",),
                     503: ("authentication_unavailable", "settings_unavailable")}
    if route.method == "GET":
        common_errors[500] = ("internal_error",)
    else:
        common_errors[400] = ("invalid_request", "invalid_json", "internal_error")
        if route.effect is RouteEffect.DURABLE_MUTATION:
            common_errors[503] += ("application_shutting_down",)
    for status, codes in doc.errors:
        common_errors[status] = tuple(dict.fromkeys((*common_errors.get(status, ()), *codes)))
    for status, codes in sorted(common_errors.items()):
        responses[str(status)] = _error_response(status, codes)
    parameters = [
        {"name": name, "in": "path", "required": True, "schema": {"type": "string", "minLength": 1}}
        for name in _PATH_VARIABLE.findall(route.path_template)
    ]
    parameters.extend(_QUERY_PARAMETERS[name] for name in doc.query)
    operation = {
        "operationId": route.operation_id,
        "summary": route.summary,
        "tags": list(route.tags),
        "x-debbuilder-effect": route.effect.value,
        "responses": responses,
    }
    if parameters:
        operation["parameters"] = parameters
    if doc.request is not None:
        operation["requestBody"] = {
            "required": False,
            "description": "JSON body; an omitted body is parsed as {} by the current server.",
            "content": {"application/json": {"schema": _ref(doc.request)}},
        }
    if route.authentication is AuthenticationPolicy.CONFIGURED_ADMIN:
        operation["security"] = [{}, {"ProxyIdentity": []}, {"OidcSession": []}]
    return operation


def openapi_document() -> dict:
    """Build the complete offline-safe document from canonical route metadata."""
    route_ids = {route.operation_id for route in ADMIN_API_ROUTES}
    if route_ids != set(OPERATION_DOCS):
        raise ValueError(f"OpenAPI operation metadata differs from registry: missing={sorted(route_ids - set(OPERATION_DOCS))}, extra={sorted(set(OPERATION_DOCS) - route_ids)}")
    paths: dict[str, dict] = {}
    for route in ADMIN_API_ROUTES:
        paths.setdefault(route.path_template, {})[route.method.lower()] = _operation(route)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DebBuilder API",
            "version": __version__,
            "description": "Authenticated admin API. Success bodies retain their existing shapes; non-2xx JSON uses ApiError. HTTP methods outside this named registry and the public APT listener are outside this contract.",
        },
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ProxyIdentity": {
                    "type": "apiKey", "in": "header", "name": "X-Forwarded-User",
                    "description": "Trusted reverse proxy identity in header mode. The header name can be configured with DEBBUILDER_AUTH_HEADER. Never expose this header directly to untrusted clients.",
                },
                "OidcSession": {
                    "type": "apiKey", "in": "cookie", "name": "debbuilder_session",
                    "description": "Interactive OIDC session cookie established by the separate browser login flow.",
                },
            },
            "schemas": SCHEMAS,
        },
        "x-debbuilder-auth-modes": {
            "none": "Configured local mode requires no credential.",
            "header": "Configured trusted proxy identity header is required.",
            "oidc": "Configured OIDC browser session cookie is required; API failures are JSON rather than redirects.",
        },
    }


def openapi_json() -> str:
    """Canonical checked-in artifact format; no clock, host or runtime settings."""
    return json.dumps(openapi_document(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(openapi_json(), end="")
