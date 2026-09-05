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

from . import artifact_publication, artifact_validation, auth_service, automation_service, build_pipeline, deb_inspector, execution_service, notifications, package_service, release_cache, settings_service, storage, upstream_archive, workspace_cleanup
from .build_store import BuildStore
from .http_handler import create_handler
from .recipe_schema import RecipeDocumentError, normalize_recipe, recipe_document_for_storage, recipe_for_storage, require_safe_name, validate_recipe_metadata
from .settings_store import cookie_secret, github_token, oidc_client_secret
from .runtime import RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]


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


def run_post_build_automation(run_id: str, *, dry_run: bool, settings: dict | None = None) -> dict:
    return automation_service.run_post_build(
        run_id,
        dry_run=dry_run,
        settings=settings or app_settings(),
        store=BuildStore(DATA / "builds"),
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
    data = json.loads(path.read_text())
    return validate_recipe_metadata(data)


def recipe_json_validation(recipe) -> dict:
    """Canonicalize Recipe JSON without writing it."""
    canonical = recipe_document_for_storage(recipe)
    existing = workflow_path(canonical["name"])
    collision = None
    if existing:
        collision = {
            "exists": True,
            "source": "user" if existing.resolve().parent == USER_WORKFLOWS.resolve() else "example",
            "replaceable": existing.resolve().parent == USER_WORKFLOWS.resolve(),
        }
    return {"ok": True, "recipe": canonical, "id": canonical["name"], "collision": collision}


def import_recipe_json(recipe, *, replace: bool = False) -> dict:
    """Create or explicitly replace a user Recipe from canonical JSON."""
    preflight = recipe_document_for_storage(recipe)
    workflow_id = preflight["name"]
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
        storage.save_json(destination, canonical)
    associate_workflow_package(workflow_id, canonical)
    return {"ok": True, "id": workflow_id, "recipe": canonical, "created": existing is None, "replaced": existing is not None}


def list_workflows() -> list[dict]:
    sources = (("user", USER_WORKFLOWS, True), ("example", EXAMPLES, False))
    return storage.list_workflows(sources, read_workflow_file)


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
        stop = threading.Event()
        retention = threading.Thread(target=workspace_retention_loop, args=(stop,), name="workspace-retention", daemon=True)
        retention.start()
        try:
            server.serve_forever()
        finally:
            stop.set()
            retention.join()


if __name__ == "__main__":
    main()
