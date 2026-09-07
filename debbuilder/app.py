#!/usr/bin/env python3
"""DebBuilder Repo UI.

Stdlib backend:
- serves the admin UI from ./static
- keeps shipped examples separate from user workflows
- executes Recipe v1 through auditable Build Runs
- validates artifacts before publication
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import artifact_publication, artifact_validation, auth_service, automation_service, build_pipeline, builtin_recipe, deb_inspector, execution_recovery, execution_service, notifications, package_service, recipe_store, release_cache, settings_service, storage, upstream_archive, workspace_cleanup
from .build_models import utc_now
from .build_store import BuildStore
from .execution_manager import ExecutionManager, ExecutionManagerError
from .http_handler import create_handler
from .recipe_schema import RecipeDocumentError, normalize_recipe, recipe_document_for_storage, recipe_for_storage, require_safe_name, validate_recipe_metadata
from .settings_store import cookie_secret, github_token, oidc_client_secret
from .runtime import RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


def application_data_dir(root: Path, environ: dict[str, str] | None = None) -> Path:
    """Resolve mutable application data independently from the code directory."""
    environment = os.environ if environ is None else environ
    return RuntimeConfig.from_environment(root, environment).data


RUNTIME = RuntimeConfig.from_environment(ROOT, os.environ)
STATIC = RUNTIME.static
EXAMPLES = RUNTIME.examples
DATA = RUNTIME.data
REPOSITORY_ROOT = RUNTIME.repository_root
USER_WORKFLOWS = RUNTIME.workflows

REPO_DEFAULT = RUNTIME.repository_url
SUITE_DEFAULT = RUNTIME.suite
COMPONENT_DEFAULT = RUNTIME.component
AUTH_MODE = RUNTIME.auth_mode  # none|header|oidc
AUTH_HEADER = RUNTIME.auth_header
OIDC_ISSUER = RUNTIME.oidc_issuer
OIDC_CLIENT_ID = RUNTIME.oidc_client_id
OIDC_REDIRECT_URI = RUNTIME.oidc_redirect_uri
SESSIONS: dict[str, dict] = {}

RUNTIME.prepare_data_directories()

PUBLIC_REPO_PREFIXES = ("/dists/", "/pool/")
PUBLIC_REPO_FILES = {"/repository.gpg", "/install.sh"}

NOTIFICATION_SERVICE = None
GITHUB_RELEASE_CACHE_SERVICE = None


class RunAdmissionError(RuntimeError):
    """Structured HTTP-facing failure while admitting a Build Run."""

    def __init__(self, code: str, message: str, *, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class RecipeStartupError(RuntimeError):
    """Fail-closed startup refusal when persisted Recipes are not all usable."""

    def __init__(self, report: recipe_store.RecipeDirectoryMigrationReport):
        super().__init__("Recipe migration failed; Build/Test admission remains closed")
        self.code = "recipe_migration_failed"
        self.report = report

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "migration": self.report.as_dict()}


def _bounded_startup_value(value, *, limit: int) -> str:
    bounded = str(value or "").replace("\r", " ").replace("\n", " ")
    bounded = re.sub(r"/[^\s'\"]+", "<path>", bounded)
    return bounded[:limit]


def recipe_startup_diagnostic(exc: Exception) -> dict:
    """Return operator-useful Recipe startup details without paths or contents."""
    failures: list[dict] = []
    if isinstance(exc, RecipeStartupError):
        for row in exc.report.files:
            if row.status != "failed" or not row.error:
                continue
            failures.append({
                "recipe": _bounded_startup_value(Path(row.file).name, limit=128),
                "code": _bounded_startup_value(row.error.get("code"), limit=80),
                "path": _bounded_startup_value(row.error.get("path") or "$", limit=160),
                "message": _bounded_startup_value(row.error.get("message"), limit=240),
            })
    elif isinstance(exc, recipe_store.RecipeStoreError):
        failures.append({
            "recipe": _bounded_startup_value(exc.file.name, limit=128),
            "code": _bounded_startup_value(exc.code, limit=80),
            "path": _bounded_startup_value(exc.path, limit=160),
            "message": _bounded_startup_value(exc, limit=240),
        })
    elif isinstance(exc, builtin_recipe.BuiltinRecipeError):
        failures.append({
            "recipe": builtin_recipe.BUILTIN_RECIPE_ID,
            "code": _bounded_startup_value(exc.code, limit=80),
            "path": _bounded_startup_value(exc.path, limit=160),
            "message": _bounded_startup_value(exc, limit=240),
        })
    return {
        "code": "recipe_startup_failed",
        "failures": failures[:20],
        "omitted_failures": max(0, len(failures) - 20),
    }


class ExecutionCancellationError(RuntimeError):
    """Structured application failure while cancelling a Build Run."""

    def __init__(self, code: str, message: str, *, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


def is_public_repo_path(path: str) -> bool:
    return path in PUBLIC_REPO_FILES or path.startswith(PUBLIC_REPO_PREFIXES)


def sanitize_id(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_.+-]", "-", value or "workflow").strip("-")
    return out or "workflow"


def json_response(handler: BaseHTTPRequestHandler, data, status=200):
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, status=200, ctype="text/plain; charset=utf-8", cache_control=None):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    handler.end_headers()
    handler.wfile.write(body)


def read_body(handler: BaseHTTPRequestHandler):
    n = int(handler.headers.get("Content-Length", "0") or "0")
    if n > 2_000_000:
        raise ValueError("body too large")
    raw = handler.rfile.read(n)
    return json.loads(raw.decode("utf-8") or "{}")


def _header_value(headers: dict, name: str) -> str:
    return auth_service.header_value(headers, name)


def parse_cookies(header: str) -> dict[str, str]:
    return auth_service.parse_cookies(header)


def sign_value(value: str) -> str:
    return auth_service.sign_value(value, cookie_secret(DATA))


def unsign_value(value: str) -> str | None:
    return auth_service.unsign_value(value, cookie_secret(DATA))


def oidc_session_user(headers: dict) -> str:
    return auth_service.oidc_session_user(headers, SESSIONS, cookie_secret(DATA))


def is_request_authorized(headers: dict, auth_mode: str | None = None) -> bool:
    return auth_service.is_request_authorized(
        headers,
        auth_mode=auth_mode,
        effective_security=effective_security(),
        auth_header=AUTH_HEADER,
        session_user=oidc_session_user,
    )


def notify_lifecycle(event: str, **payload) -> None:
    notification_service().notify_build_lifecycle(event, **payload)


def run_recipe_pipeline(workflow: dict, *, dry_run: bool = True) -> dict:
    """Delegate the connected pipeline stages to the build engine."""
    return build_pipeline.run_pipeline(
        workflow, store=BuildStore(DATA / "builds"), dry_run=dry_run,
        recipe_id=str(workflow.get("name") or "recipe"), github_token=github_token(DATA),
        lifecycle_callback=notify_lifecycle,
    )


def run_post_build_automation(run_id: str, *, dry_run: bool, settings: dict | None = None, store: BuildStore | None = None) -> dict:
    return automation_service.run_post_build(
        run_id,
        dry_run=dry_run,
        settings=settings or app_settings(),
        store=store or BuildStore(DATA / "builds"),
        validate=validate_build_artifact,
        publish=publish_build_artifact,
    )


def run_recipe_pipeline_with_automation(workflow: dict, *, dry_run: bool = True) -> dict:
    try:
        return automation_service.run_with_automation(
            workflow,
            dry_run=dry_run,
            pipeline=run_recipe_pipeline,
            automate=run_post_build_automation,
            notify_completion=lambda result: notification_service().notify_automatic_completion(result),
        )
    finally:
        cleanup_workspaces()


def execute_queued_recipe_run(run_id: str, *, store: BuildStore, expected_initial_status: str, cancellation_control=None) -> dict:
    """Execute one admitted Run and its existing post-build lifecycle."""
    run = store.load(run_id)
    dry_run = bool(run and run.get("mode") == "dry_run")
    try:
        result = build_pipeline.execute_pipeline_run(
            run_id,
            store=store,
            expected_initial_status=expected_initial_status,
            github_token=github_token(DATA),
            lifecycle_callback=notify_lifecycle,
            cancellation_control=cancellation_control,
        )
        return automation_service.complete_with_automation(
            result,
            dry_run=dry_run,
            automate=lambda submitted_run_id, *, dry_run: run_post_build_automation(
                submitted_run_id, dry_run=dry_run, store=store,
            ),
            notify_completion=lambda completed: notification_service().notify_automatic_completion(completed),
        )
    finally:
        cleanup_workspaces()


def create_execution_manager(*, store: BuildStore | None = None, queue_capacity: int = 8, execute=None) -> ExecutionManager:
    """Construct, but do not start, the server's single execution manager."""
    callback = execute or (lambda run_id, **kwargs: execute_queued_recipe_run(run_id, **kwargs))
    return ExecutionManager(store or BuildStore(DATA / "builds"), queue_capacity=queue_capacity, execute=callback)


def prepare_application_directories() -> None:
    """Ensure the mutable Recipe directory exists before startup orchestration."""
    USER_WORKFLOWS.mkdir(parents=True, exist_ok=True)


def prepare_recipes_for_startup():
    """Eagerly migrate user Recipes, then reconcile the managed built-in."""
    migration = recipe_store.migrate_recipe_directory(USER_WORKFLOWS)
    if not migration.ok:
        raise RecipeStartupError(migration)
    reconciliation = builtin_recipe.reconcile_builtin_recipe(USER_WORKFLOWS)
    return migration, reconciliation


def start_execution_manager(http_server, manager: ExecutionManager | None = None) -> ExecutionManager:
    """Recover Runs, prepare Recipes, then attach/start one gated manager."""
    if getattr(http_server, "execution_manager", None) is not None:
        raise RuntimeError("HTTP server already has an execution manager")
    prepare_application_directories()
    selected = manager or create_execution_manager()
    recovery = execution_recovery.recover_startup(selected.store)
    try:
        migration, reconciliation = prepare_recipes_for_startup()
    except (RecipeStartupError, recipe_store.RecipeStoreError, builtin_recipe.BuiltinRecipeError) as exc:
        LOGGER.error("Recipe startup failure: %s", json.dumps(recipe_startup_diagnostic(exc), sort_keys=True))
        raise
    selected.start(admission_blocker=recovery.admission_blocker)
    http_server.recipe_migration = migration.as_dict()
    http_server.builtin_recipe_reconciliation = {
        "action": reconciliation.action,
        "definition_version": reconciliation.definition_version,
        "previous_definition_version": reconciliation.previous_definition_version,
    }
    http_server.execution_recovery = recovery.as_dict()
    http_server.execution_manager = selected
    return selected


def stop_execution_manager(http_server, *, timeout: float | None = None) -> None:
    """Stop and detach the HTTP server's manager after submitted work drains."""
    manager = getattr(http_server, "execution_manager", None)
    if manager is None:
        return
    manager.stop(timeout=timeout)
    http_server.execution_manager = None


def _enqueue_failure_details(exc: Exception, run_id: str) -> dict:
    details = {"run_id": run_id, "exception_type": type(exc).__name__}
    if isinstance(exc, ExecutionManagerError):
        details["manager_code"] = exc.code
    return details


def _mark_enqueue_failed(store: BuildStore, run_id: str, exc: Exception) -> None:
    safe_error = {
        "stage": "queue",
        "code": "execution_enqueue_failed",
        "message": "The Build Run could not be submitted for execution",
        "details": _enqueue_failure_details(exc, run_id),
    }
    with store.locked_run(run_id):
        run = store.load(run_id)
        if not run or run.get("status") not in {"pending", "queued"}:
            return
        run.update({"status": "failed", "finished_at": utc_now(), "duration": 0.0, "error": safe_error})
        store.save(run)
    try:
        store.append_log_line(run_id, "Execution enqueue failed; Run marked failed.", level="error")
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not append enqueue failure log for Run %s (%s)", run_id, type(exc).__name__)


def enqueue_recipe_run(manager: ExecutionManager | None, workflow: dict, *, dry_run: bool = True) -> dict:
    """Reserve capacity, persist exactly one Run, and submit it asynchronously."""
    if manager is None:
        raise RunAdmissionError(
            "execution_manager_unavailable", "Execution manager is unavailable", status=503,
        )
    try:
        with manager.reserve() as reservation:
            run = build_pipeline.create_pipeline_run(
                workflow,
                store=manager.store,
                dry_run=dry_run,
                recipe_id=str(workflow.get("name") or "recipe"),
            )
            try:
                reservation.submit(str(run["id"]))
            except Exception as exc:
                _mark_enqueue_failed(manager.store, str(run["id"]), exc)
                if isinstance(exc, ExecutionManagerError) and exc.code in {
                    "execution_manager_not_accepting", "execution_manager_stopped", "execution_reservation_inactive",
                }:
                    raise RunAdmissionError(
                        "execution_manager_unavailable", "Execution manager is unavailable", status=503,
                        details={"run_id": run["id"]},
                    ) from exc
                raise RunAdmissionError(
                    "execution_enqueue_failed", "The Build Run could not be submitted for execution", status=500,
                    details={"run_id": run["id"]},
                ) from exc
    except ExecutionManagerError as exc:
        if exc.code == "execution_queue_full":
            raise RunAdmissionError(
                exc.code, "The Build/Test queue is full; try again later", status=429, details=exc.details,
            ) from exc
        if exc.code == execution_recovery.BLOCKER_CODE:
            raise RunAdmissionError(
                exc.code,
                "Build/Test admission is blocked until interrupted workload recovery is resolved",
                status=503,
                details=exc.details,
            ) from exc
        raise RunAdmissionError(
            "execution_manager_unavailable", "Execution manager is unavailable", status=503,
        ) from exc
    return {"run_id": run["id"], "status": "queued"}


def cleanup_workspaces() -> dict:
    """Use the current DATA/settings; cleanup failures never change a Run result."""
    try:
        result = workspace_cleanup.apply_retention(
            BuildStore(DATA / "builds"), app_settings().get("workspace_cleanup"),
        )
        for error in result["errors"]:
            logging.getLogger(__name__).warning("Workspace cleanup: %s", error)
        return result
    except Exception as exc:
        logging.getLogger(__name__).exception("Workspace retention sweep failed")
        return {"cleaned": [], "retained": [], "skipped": [], "errors": [{"error": str(exc)}]}


def _cancellation_result(run_id: str, status: str, metadata: dict) -> dict:
    allowed = ("code", "reason", "phase", "stage", "requested_at", "completed_at")
    return {
        "run_id": run_id,
        "status": status,
        **{key: metadata[key] for key in allowed if metadata.get(key) is not None},
    }


def cancel_execution(manager: ExecutionManager | None, run_id: str, *, cleanup=None) -> dict:
    """Delegate cancellation ownership and map it to the public application contract."""
    require_safe_name(run_id, "execution")
    if manager is None or not manager.accepting:
        raise ExecutionCancellationError(
            "execution_manager_unavailable", "Execution manager is unavailable", status=503,
            details={"run_id": run_id},
        )
    try:
        result = manager.cancel(run_id)
    except ExecutionManagerError as exc:
        if exc.code == "build_run_not_found":
            raise ExecutionCancellationError(
                "build_run_not_found", "Build Run was not found", status=404,
                details={"run_id": run_id},
            ) from exc
        if exc.code in {"execution_manager_not_accepting", "execution_manager_stopped"}:
            raise ExecutionCancellationError(
                "execution_manager_unavailable", "Execution manager is unavailable", status=503,
                details={"run_id": run_id},
            ) from exc
        raise ExecutionCancellationError(
            "execution_not_cancellable", "Build Run cannot be cancelled", status=409,
            details={"run_id": run_id, **exc.details},
        ) from exc
    except OSError as exc:
        raise ExecutionCancellationError(
            "execution_cancellation_failed", "Build Run cancellation could not be persisted", status=500,
            details={"run_id": run_id, "exception_type": type(exc).__name__},
        ) from exc

    outcome = result["outcome"]
    if outcome == "not_found":
        raise ExecutionCancellationError(
            "build_run_not_found", "Build Run was not found", status=404,
            details={"run_id": run_id},
        )
    if outcome == "terminal_not_cancellable":
        persisted = manager.store.load(run_id)
        status = str((persisted or {}).get("status") or result.get("status") or "unknown")
        raise ExecutionCancellationError(
            "execution_not_cancellable", f"Build Run cannot be cancelled from status {status}", status=409,
            details={"run_id": run_id, "status": status},
        )
    if outcome == "queued_cancelled":
        sweep = cleanup or cleanup_workspaces
        try:
            sweep()
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Workspace retention sweep failed after queued cancellation of Run %s", run_id,
            )
        return _cancellation_result(run_id, "cancelled", result.get("cancellation") or {})
    if outcome == "active_cancel_requested":
        return _cancellation_result(run_id, "cancelling", result.get("cancellation") or {})
    if outcome == "already_cancelled":
        return _cancellation_result(run_id, "cancelled", result.get("cancellation") or {})
    raise ExecutionCancellationError(
        "execution_cancellation_failed", "Execution manager returned an unknown cancellation result", status=500,
        details={"run_id": run_id},
    )


def workspace_retention_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        cleanup_workspaces()
        stop.wait(300)


def validate_build_artifact(run_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    result = artifact_validation.validate_artifact(
        run_id,
        store=BuildStore(DATA / "builds"),
        previous_artifact=str(payload.get("previous_artifact") or ""),
        profile=str(payload.get("profile") or "bookworm"),
        allowed_previous_roots=(REPOSITORY_ROOT / "pool",),
    )
    notification_service().notify_validation_result(result)
    return result


def publish_build_artifact(run_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    apt = repo_settings()
    result = artifact_publication.publish_artifact(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
        confirm=str(payload.get("confirm") or ""),
    )
    notification_service().notify_publication_result(result)
    return result


def reconcile_build_publication(run_id: str, payload: dict | None = None) -> dict:
    apt = repo_settings()
    return artifact_publication.reconcile_publication(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
    )


def read_workflow_file(path: Path) -> dict:
    write_back = path.parent.resolve() == USER_WORKFLOWS.resolve()
    return validate_recipe_metadata(recipe_store.load_recipe(path, write_back=write_back))


def recipe_json_validation(recipe) -> dict:
    """Canonicalize Recipe JSON without writing it."""
    canonical = recipe_document_for_storage(recipe)
    existing = workflow_path(canonical["name"])
    collision = None
    if existing:
        builtin = builtin_recipe.is_builtin_recipe_id(canonical["name"])
        collision = {
            "exists": True,
            "source": "builtin" if builtin else "user" if existing.resolve().parent == USER_WORKFLOWS.resolve() else "example",
            "replaceable": not builtin and existing.resolve().parent == USER_WORKFLOWS.resolve(),
        }
    return {"ok": True, "recipe": canonical, "id": canonical["name"], "collision": collision}


def import_recipe_json(recipe, *, replace: bool = False) -> dict:
    """Create or explicitly replace a user Recipe from canonical JSON."""
    preflight = recipe_document_for_storage(recipe)
    workflow_id = preflight["name"]
    builtin_recipe.require_user_recipe_id(workflow_id)
    destination = workflow_path(workflow_id, for_write=True)
    assert destination is not None
    with storage.locked_path(destination):
        canonical = recipe_document_for_storage(recipe)
        existing = workflow_path(workflow_id)
        if existing:
            is_user_recipe = existing.resolve().parent == USER_WORKFLOWS.resolve()
            if not is_user_recipe:
                raise PermissionError("shipped recipes are read-only and cannot be replaced")
            if not replace:
                raise FileExistsError("recipe id already exists; explicit replacement is required")
        canonical = recipe_store.save_recipe(destination, canonical)
    associate_workflow_package(workflow_id, canonical)
    return {"ok": True, "id": workflow_id, "recipe": canonical, "created": existing is None, "replaced": existing is not None}


def save_workflow_recipe(workflow_id: str, workflow: dict, *, previous_id: str = "") -> dict:
    """Persist a normal Recipe or an allowlisted edit to the managed built-in."""
    require_safe_name(workflow_id, "workflow id")
    if previous_id:
        require_safe_name(previous_id, "previous workflow id")
    canonical = recipe_document_for_storage(workflow)
    if canonical["name"] != workflow_id:
        raise RecipeDocumentError(
            "recipe_identity_mismatch",
            "Recipe name must match the requested workflow ID",
            path="$.name",
        )
    if builtin_recipe.is_builtin_recipe_id(previous_id) and previous_id != workflow_id:
        raise builtin_recipe.BuiltinRecipeError(
            "builtin_recipe_reserved", "The application-managed DebBuilder Recipe cannot be renamed",
            path="$.previous_id",
        )
    destination = workflow_path(workflow_id, for_write=True)
    assert destination is not None
    if builtin_recipe.is_builtin_recipe_id(workflow_id):
        stored = builtin_recipe.update_builtin_recipe(destination, canonical)
        normalized = validate_recipe_metadata(stored)
    else:
        normalized = validate_recipe_metadata(canonical)
        stored = recipe_for_storage(normalized)
        with storage.locked_path(destination):
            existing = workflow_path(workflow_id)
            if existing and existing.resolve().parent != USER_WORKFLOWS.resolve():
                raise PermissionError("shipped recipes are read-only")
            stored = recipe_store.save_recipe(destination, stored)
    if previous_id and previous_id != workflow_id:
        previous = workflow_path(previous_id)
        if previous and previous.parent.resolve() == USER_WORKFLOWS.resolve():
            previous.unlink()
    associate_workflow_package(workflow_id, normalized, previous_id)
    return {"ok": True, "id": workflow_id, "path": str(destination)}


def workflow_listing() -> dict:
    """List usable Recipes while reporting individual unreadable entries."""
    sources = (("user", USER_WORKFLOWS, True), ("example", EXAMPLES, False))
    items: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for source, folder, writable in sources:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.stem in seen:
                continue
            try:
                workflow = read_workflow_file(path)
                updated = path.stat().st_mtime
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                detail = exc.as_dict() if isinstance(exc, recipe_store.RecipeStoreError) else {
                    "code": "recipe_load_failed",
                    "message": "Recipe could not be loaded",
                    "path": "$",
                }
                errors.append({
                    "id": path.stem,
                    "source": source,
                    "error": {
                        "code": str(detail.get("code") or "recipe_load_failed"),
                        "message": str(detail.get("message") or "Recipe could not be loaded")[:240],
                        "path": str(detail.get("path") or "$"),
                    },
                })
                continue
            seen.add(path.stem)
            item = {
                "id": path.stem,
                "name": workflow.get("name", path.stem),
                "source": source,
                "writable": writable,
                "updated": updated,
            }
            item.update(builtin_recipe.ui_projection(workflow))
            items.append(item)
    return {"workflows": items, "errors": errors}


def list_workflows() -> list[dict]:
    return workflow_listing()["workflows"]


def workflow_path(wid: str, for_write: bool = False) -> Path | None:
    return storage.workflow_path(
        wid,
        USER_WORKFLOWS,
        (USER_WORKFLOWS, EXAMPLES),
        require_safe_name,
        for_write=for_write,
    )


def delete_workflow(wid: str) -> None:
    """Delete only a user-owned recipe and clear local package associations."""
    builtin_recipe.require_user_recipe_id(wid)
    path = workflow_path(wid)
    if not path:
        raise FileNotFoundError("recipe not found")
    if path.resolve().parent != USER_WORKFLOWS.resolve():
        raise PermissionError("shipped recipes are read-only")
    path.unlink()
    package_projection_service().unlink_recipe(wid)


def github_release_cache():
    global GITHUB_RELEASE_CACHE_SERVICE
    if GITHUB_RELEASE_CACHE_SERVICE is None or GITHUB_RELEASE_CACHE_SERVICE.data_dir.resolve() != DATA.resolve():
        GITHUB_RELEASE_CACHE_SERVICE = release_cache.GitHubReleaseCache(DATA, lambda: github_token(DATA))
    return GITHUB_RELEASE_CACHE_SERVICE


def package_projection_service() -> package_service.PackageService:
    return package_service.PackageService(
        data_dir=DATA,
        workspace_root=ROOT,
        list_workflows=list_workflows,
        workflow_path=workflow_path,
        read_workflow=read_workflow_file,
        repo_settings=repo_settings,
        release_lookup=lambda repository: github_release_cache().get(repository),
    )


def recipe_package_name(recipe: dict) -> str:
    return package_service.recipe_package_name(recipe)


def live_published_index() -> list[dict]:
    return package_projection_service().fetch_live_index()


def build_run_package(run: dict) -> str:
    return package_service.build_run_package(run)


def list_packages(*, include_history: bool = False) -> list[dict]:
    return package_projection_service().list_packages(
        include_history=include_history,
        live_rows=live_published_index(),
    )


def get_package(name: str) -> dict | None:
    require_safe_name(name, "package")
    for pkg in list_packages(include_history=True):
        if package_service.normalized_package_name(pkg.get("name")) == package_service.normalized_package_name(name):
            return dict(pkg)
    return None


def create_or_update_package(data: dict, name: str | None = None) -> dict:
    current = get_package(name) if name else None
    return package_projection_service().create_or_update(data, name=name, current=current)


def associate_workflow_package(wid: str, workflow: dict, previous_id: str = "") -> None:
    package_projection_service().associate_workflow(wid, workflow, previous_id)


def inspect_upstream_archive(workflow: dict) -> dict:
    recipe = normalize_recipe(workflow)
    if recipe["artifact"]["mode"] != "upstream_archive":
        raise ValueError("archive inspection requires upstream_archive artifact mode")
    return upstream_archive.inspect(recipe, token=github_token(DATA))


def delete_package(name: str) -> None:
    package_projection_service().mark_deleted(name)


def list_recipes() -> list[dict]:
    out = []
    package_by_recipe = {p.get("recipe"): p.get("name") for p in list_packages() if p.get("recipe")}
    for wf in list_workflows():
        rid = wf["id"]
        path = workflow_path(rid)
        valid = False
        package = package_by_recipe.get(rid, "")
        try:
            recipe = read_workflow_file(path) if path else {}
            valid = True
            package = package or recipe_package_name(recipe)
            source = recipe.get("source") or {}
            out_metadata = {"repository": source.get("repository", ""), "tracking": source.get("tracking", "latest_release"), "active": recipe.get("active", True)}
        except Exception:
            out_metadata = {}
        out.append({**wf, **out_metadata, "package": package, "valid": valid})
    return out


def list_executions(limit: int = 50, *, structured_runs: list[dict] | None = None) -> list[dict]:
    return execution_service.list_executions(
        BuildStore(DATA / "builds"),
        build_run_package,
        limit=limit,
        runs=structured_runs,
    )


def get_execution(run_id: str) -> dict | None:
    execution = execution_service.get_execution(BuildStore(DATA / "builds"), run_id)
    if execution:
        execution["package"] = build_run_package(execution)
    return execution


def get_execution_log(run_id: str, *, verbosity: str = "normal", after: int = 0) -> dict | None:
    return execution_service.get_log(BuildStore(DATA / "builds"), run_id, verbosity=verbosity, after=after)


def delete_execution_log(run_id: str) -> dict:
    return execution_service.delete_log(BuildStore(DATA / "builds"), run_id)


def delete_execution_logs(run_ids: list[str] | None = None, *, all_runs: bool = False, dry_run: bool = False) -> dict:
    return execution_service.delete_logs(
        BuildStore(DATA / "builds"),
        run_ids,
        all_runs=all_runs,
        dry_run=dry_run,
    )


def dashboard_summary() -> dict:
    packages = list_packages(include_history=False)
    executions = list_executions(limit=20)
    state_counts: dict[str, int] = {}
    for pkg in packages:
        state = pkg.get("lifecycle_display_status") or pkg.get("lifecycle_state") or pkg.get("status") or "unknown"
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "packages": len(packages),
        "updates": state_counts.get("update_available", 0),
        "ready_to_publish": state_counts.get("ready_to_publish", 0) + state_counts.get("publication_available", 0),
        "builds": len(executions),
        "errors": sum(1 for e in executions if e.get("status") == "failed"),
        "package_errors": sum(state_counts.get(state, 0) for state in ("failed", "build_failed", "validation_failed", "publication_failed")),
        "linked_recipes": sum(1 for pkg in packages if pkg.get("recipe")),
        "github_sources": sum(1 for pkg in packages if (pkg.get("source") or {}).get("repository")),
        "local_sources": sum(1 for pkg in packages if not (pkg.get("source") or {}).get("repository")),
        "state_counts": state_counts,
        "package_rows": [{
            "name": pkg.get("name"), "recipe": pkg.get("recipe"), "architecture": pkg.get("architecture"),
            "apt_version": pkg.get("apt_version"), "upstream_version": pkg.get("upstream_version"),
            "source": pkg.get("source"), "version": pkg.get("version"), "build": pkg.get("build"),
            "lifecycle_state": pkg.get("lifecycle_state"),
            "lifecycle_display_status": pkg.get("lifecycle_display_status") or pkg.get("lifecycle_state"),
        } for pkg in packages],
        "latest_operations": executions[:8],
    }


def settings_defaults() -> dict:
    return settings_service.defaults_from_environment(
        repo_default=REPO_DEFAULT,
        suite_default=SUITE_DEFAULT,
        component_default=COMPONENT_DEFAULT,
        auth_mode=AUTH_MODE,
        oidc_issuer=OIDC_ISSUER,
        oidc_client_id=OIDC_CLIENT_ID,
        oidc_redirect_uri=OIDC_REDIRECT_URI,
        public_url=RUNTIME.public_url,
    )


def app_settings() -> dict:
    return settings_service.load_app_settings(DATA, settings_defaults())


def repo_settings() -> dict:
    return app_settings()["apt"]


def effective_security() -> dict:
    return app_settings()["security"]


def settings_view() -> dict:
    view = settings_service.public_settings_view(
        data_dir=DATA,
        root=ROOT,
        settings=app_settings(),
        port=RUNTIME.port,
    )
    notification_settings = dict(view.get("notifications") or {})
    notification_settings["token"] = "masked"
    notification_settings["token_configured"] = notifications.ntfy_token_configured(DATA)
    view["notifications"] = notification_settings
    return view


def update_settings(payload: dict) -> dict:
    notification_settings = payload.get("notifications") if isinstance(payload, dict) else None
    if isinstance(notification_settings, dict) and notification_settings.get("token"):
        notifications.save_ntfy_token(DATA, str(notification_settings["token"]))
    settings_service.update_settings(DATA, payload, app_settings(), settings_view)
    return settings_view()


def notification_service():
    global NOTIFICATION_SERVICE
    service_data_dir = Path(getattr(NOTIFICATION_SERVICE, "data_dir", DATA))
    if NOTIFICATION_SERVICE is None or service_data_dir.resolve() != DATA.resolve():
        store = BuildStore(DATA / "builds")
        NOTIFICATION_SERVICE = notifications.NotificationService(
            DATA,
            app_settings,
            run_loader=store.load,
            package_resolver=lambda run, recipe: recipe_package_name(recipe) if recipe else build_run_package(run or {}),
        )
    return NOTIFICATION_SERVICE


def test_notification() -> dict:
    return notification_service().send_test()

def oidc_discovery() -> dict:
    return auth_service.oidc_discovery(effective_security(), urlopen=urllib.request.urlopen)


def oidc_authorize_url(return_to: str = "/") -> tuple[str, str]:
    return auth_service.oidc_authorize_url(
        return_to,
        config=effective_security(),
        discovery=oidc_discovery(),
        sessions=SESSIONS,
    )


def _b64json(value: str) -> dict:
    return auth_service.b64json(value)


def _validate_rs256(jwt: str, jwks_uri: str, *, issuer: str, audience: str, nonce: str) -> dict:
    return auth_service.validate_rs256(
        jwt,
        jwks_uri,
        issuer=issuer,
        audience=audience,
        nonce=nonce,
        urlopen=urllib.request.urlopen,
    )


def exchange_oidc_code(code: str, nonce: str, code_verifier: str) -> dict:
    return auth_service.exchange_oidc_code(
        code,
        nonce,
        code_verifier,
        config=effective_security(),
        discovery=oidc_discovery(),
        client_secret=oidc_client_secret(DATA),
        validate_id_token=_validate_rs256,
        urlopen=urllib.request.urlopen,
    )


def create_session(userinfo: dict) -> str:
    return auth_service.create_session(userinfo, SESSIONS, sign_value)


Handler = create_handler(sys.modules[__name__])


def main():
    print(f"DebBuilder Repo UI listening on http://{RUNTIME.host}:{RUNTIME.port}")
    with ThreadingHTTPServer((RUNTIME.host, RUNTIME.port), Handler) as server:
        start_execution_manager(server)
        stop = threading.Event()
        retention = threading.Thread(target=workspace_retention_loop, args=(stop,), name="workspace-retention", daemon=True)
        retention.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            retention.join()
            stop_execution_manager(server)


if __name__ == "__main__":
    main()
