"""Canonical route inventory for the authenticated admin API.

The descriptors in this module are deliberately limited to HTTP contract and
routing metadata.  Runtime handlers remain in :mod:`debbuilder.http_handler`,
and application/business services remain outside this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import urllib.parse


class AuthenticationPolicy(str, Enum):
    """Authentication boundary applied before an admin API handler runs.

    ``CONFIGURED_ADMIN`` means the current application-wide admin policy:
    unauthenticated local mode, trusted proxy/header mode, or OIDC session mode
    according to effective settings.  The registry describes that boundary;
    enforcement remains in ``Handler._authorized``.
    """

    CONFIGURED_ADMIN = "configured_admin"


class RouteEffect(str, Enum):
    """Persistence effect of a route's operation."""

    READ_ONLY = "read_only"
    EPHEMERAL_ACTION = "ephemeral_action"
    DURABLE_MUTATION = "durable_mutation"


@dataclass(frozen=True)
class ApiRoute:
    method: str
    path_template: str
    operation_id: str
    handler: str
    authentication: AuthenticationPolicy
    effect: RouteEffect
    tags: tuple[str, ...]
    summary: str
    request_schema: str | None = None
    success_response_schema: str | None = None
    error_schemas: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteMatch:
    route: ApiRoute
    path_variables: dict[str, str]


_VARIABLE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_METHODS = frozenset({"GET", "POST", "DELETE"})


def _route(
    method: str,
    path_template: str,
    operation_id: str,
    handler: str,
    effect: RouteEffect,
    tag: str,
    summary: str,
) -> ApiRoute:
    return ApiRoute(
        method=method,
        path_template=path_template,
        operation_id=operation_id,
        handler=handler,
        authentication=AuthenticationPolicy.CONFIGURED_ADMIN,
        effect=effect,
        tags=(tag,),
        summary=summary,
    )


R = RouteEffect.READ_ONLY
E = RouteEffect.EPHEMERAL_ACTION
D = RouteEffect.DURABLE_MUTATION


ADMIN_API_ROUTES: tuple[ApiRoute, ...] = (
    _route("GET", "/api/status", "system.status", "_get_status", R, "system", "Get application status"),
    _route("GET", "/api/auth/status", "auth.status", "_get_auth_status", R, "authentication", "Get authentication status"),
    _route("GET", "/api/dashboard", "dashboard.get", "_get_dashboard", R, "dashboard", "Get dashboard summary"),
    _route("GET", "/api/packages", "packages.list", "_get_packages", R, "packages", "List packages"),
    _route("GET", "/api/packages/{name}", "packages.get", "_get_package", R, "packages", "Get a package"),
    _route("GET", "/api/recipes", "recipes.list", "_get_recipes", R, "recipes", "List recipes"),
    _route("GET", "/api/recipes/{recipe_id}/automation", "automation.get", "_get_automation", R, "automation", "Get recipe automation status"),
    _route("GET", "/api/executions", "executions.list", "_get_executions", R, "executions", "List executions"),
    _route("GET", "/api/executions/{run_id}", "executions.get", "_get_execution", R, "executions", "Get an execution"),
    _route("GET", "/api/executions/{run_id}/logs", "executions.logs.get", "_get_execution_log", R, "executions", "Get execution logs"),
    _route("GET", "/api/executions/{run_id}/validations/{attempt_id}", "validation.get", "_get_validation", R, "validation", "Get a validation attempt"),
    _route("GET", "/api/settings", "settings.get", "_get_settings", R, "settings", "Get settings"),
    _route("GET", "/api/storage", "storage.get", "_get_storage", R, "storage", "Get storage inventory"),
    _route("GET", "/api/workflows", "workflows.list", "_get_workflows", R, "workflows", "List workflows"),
    _route("GET", "/api/workflows/{workflow_id}", "workflows.get", "_get_workflow", R, "workflows", "Get a workflow"),
    _route("POST", "/api/recipes/{recipe_id}/observation/refresh", "observations.refresh", "_post_observation_refresh", D, "observations", "Refresh upstream observation"),
    _route("POST", "/api/recipes/{recipe_id}/automation/check", "automation.check", "_post_automation_check", D, "automation", "Check recipe automation now"),
    _route("POST", "/api/recipes/{recipe_id}/automation/retry", "automation.retry", "_post_automation_retry", D, "automation", "Retry recipe automation"),
    _route("POST", "/api/executions/{run_id}/validations/{attempt_id}/cancel", "validation.cancel", "_post_validation_cancel", D, "validation", "Cancel a validation attempt"),
    _route("POST", "/api/executions/{run_id}/cancel", "executions.cancel", "_post_execution_cancel", D, "executions", "Cancel an execution"),
    _route("POST", "/api/recipes/validate", "recipes.validate", "_post_recipe_validate", E, "recipes", "Validate recipe JSON"),
    _route("POST", "/api/recipes/import", "recipes.import", "_post_recipe_import", D, "recipes", "Import a recipe"),
    _route("POST", "/api/run", "executions.run", "_post_run", D, "executions", "Start a recipe run"),
    _route("POST", "/api/upstream-archive/inspect", "archives.inspect", "_post_archive_inspect", E, "archives", "Inspect an upstream archive"),
    _route("POST", "/api/executions/{run_id}/validate", "validation.start", "_post_validation_start", D, "validation", "Start artifact validation"),
    _route("POST", "/api/executions/{run_id}/publish", "publication.publish", "_post_publication_publish", D, "publication", "Publish a build artifact"),
    _route("POST", "/api/executions/{run_id}/reconcile-publication", "publication.reconcile", "_post_publication_reconcile", D, "publication", "Reconcile artifact publication"),
    _route("POST", "/api/notifications/test", "notifications.test", "_post_notification_test", D, "notifications", "Send a test notification"),
    _route("POST", "/api/settings", "settings.update", "_post_settings", D, "settings", "Update settings"),
    _route("POST", "/api/executions/delete-logs", "executions.logs.delete_many", "_post_execution_logs_delete", D, "executions", "Delete execution logs"),
    _route("POST", "/api/packages", "packages.create", "_post_package_create", D, "packages", "Create a package"),
    _route("POST", "/api/packages/{name}", "packages.update", "_post_package_update", D, "packages", "Update a package"),
    _route("POST", "/api/workflows/{workflow_id}", "workflows.save", "_post_workflow_save", D, "workflows", "Save a workflow"),
    _route("DELETE", "/api/workflows/{workflow_id}", "workflows.delete", "_delete_workflow", D, "workflows", "Delete a workflow"),
    _route("DELETE", "/api/executions/{run_id}/logs", "executions.logs.delete", "_delete_execution_log", D, "executions", "Delete execution logs"),
    _route("DELETE", "/api/packages/{name}", "packages.delete", "_delete_package", D, "packages", "Delete a package"),
)


def _template_parts(path_template: str) -> tuple[str, ...]:
    return tuple(path_template.lstrip("/").split("/"))


def validate_routes(routes: tuple[ApiRoute, ...], handler_owner=None) -> None:
    """Fail fast for invalid developer-defined route configuration."""
    method_paths: set[tuple[str, str]] = set()
    operation_ids: set[str] = set()
    for route in routes:
        if route.method not in _METHODS:
            raise ValueError(f"invalid API route method: {route.method!r}")
        if not route.path_template.startswith("/api/") or route.path_template.endswith("/"):
            raise ValueError(f"invalid API route path template: {route.path_template!r}")
        if "//" in route.path_template or "?" in route.path_template or "#" in route.path_template:
            raise ValueError(f"invalid API route path template: {route.path_template!r}")
        variables: set[str] = set()
        for part in _template_parts(route.path_template):
            if "{" not in part and "}" not in part:
                continue
            match = _VARIABLE.fullmatch(part)
            if match is None:
                raise ValueError(f"invalid API route path template: {route.path_template!r}")
            name = match.group(1)
            if name in variables:
                raise ValueError(f"duplicate path variable {name!r} in {route.path_template!r}")
            variables.add(name)
        method_path = (route.method, route.path_template)
        if method_path in method_paths:
            raise ValueError(f"duplicate API route: {route.method} {route.path_template}")
        if route.operation_id in operation_ids:
            raise ValueError(f"duplicate API operation id: {route.operation_id}")
        if not route.operation_id or not route.handler:
            raise ValueError("API routes require operation and handler identities")
        if route.method == "GET" and route.effect is not RouteEffect.READ_ONLY:
            raise ValueError(f"GET route must be read-only: {route.operation_id}")
        if route.method != "GET" and route.effect is RouteEffect.READ_ONLY:
            raise ValueError(f"non-GET route cannot be read-only: {route.operation_id}")
        if route.effect is RouteEffect.EPHEMERAL_ACTION and route.method != "POST":
            raise ValueError(f"ephemeral action must use POST: {route.operation_id}")
        if handler_owner is not None and not callable(getattr(handler_owner, route.handler, None)):
            raise ValueError(f"API route handler is missing: {route.handler}")
        method_paths.add(method_path)
        operation_ids.add(route.operation_id)


def match_route(method: str, path: str, routes: tuple[ApiRoute, ...] = ADMIN_API_ROUTES) -> RouteMatch | None:
    """Match one decoded-variable route using deterministic segment templates."""
    request_parts = _template_parts(path)
    for route in routes:
        if route.method != method:
            continue
        template_parts = _template_parts(route.path_template)
        if len(template_parts) != len(request_parts):
            continue
        variables: dict[str, str] = {}
        for template_part, request_part in zip(template_parts, request_parts):
            variable = _VARIABLE.fullmatch(template_part)
            if variable is None:
                if template_part != request_part:
                    break
            else:
                variables[variable.group(1)] = urllib.parse.unquote(request_part)
        else:
            return RouteMatch(route=route, path_variables=variables)
    return None


validate_routes(ADMIN_API_ROUTES)
