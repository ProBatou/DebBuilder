#!/usr/bin/env python3
"""DebBuilder Repo UI.

Stdlib backend:
- serves the admin UI from ./static
- keeps shipped examples separate from user workflows
- generates auditable Bash scripts
- executes dry-run by default
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import apt_repo, artifact_publication, artifact_validation, auth_service, build_pipeline, deb_inspector, github_client, operations, package_lifecycle, package_store, repo_summary, script_generator, settings_service, storage, upstream_archive
from .build_store import BuildStore
from .http_handler import create_handler
from .recipe_schema import (
    SAFE_ARCH,
    STANDARD_STEP_TYPES,
    SUPPORTED_STEP_TYPES,
    normalize_github_version,
    normalize_recipe,
    normalize_steps,
    recipe_for_storage,
    require_safe_name,
    uses_automatic_pipeline,
    validate_recipe_metadata,
)
from .settings_store import cookie_secret, github_token, oidc_client_secret

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
EXAMPLES = ROOT / "examples"


def application_data_dir(root: Path, environ: dict[str, str] | None = None) -> Path:
    """Resolve mutable application data independently from the code directory."""
    environment = os.environ if environ is None else environ
    configured = str(environment.get("DEBBUILDER_DATA_DIR") or "").strip()
    return Path(configured).expanduser() if configured else root / "data"


DATA = application_data_dir(ROOT)
REPOSITORY_ROOT = Path(os.environ.get("DEBBUILDER_REPO_ROOT", "/var/www/html"))
USER_WORKFLOWS = DATA / "workflows"
RUNS = DATA / "runs"

REPO_DEFAULT = os.environ.get("DEBBUILDER_REPO_URL", "https://repo.example.invalid")
SUITE_DEFAULT = os.environ.get("DEBBUILDER_SUITE", "stable")
COMPONENT_DEFAULT = os.environ.get("DEBBUILDER_COMPONENT", "main")
AUTH_MODE = os.environ.get("DEBBUILDER_AUTH_MODE", "none").lower()  # none|header|oidc
AUTH_HEADER = os.environ.get("DEBBUILDER_AUTH_HEADER", "X-Forwarded-User")
OIDC_ISSUER = os.environ.get("DEBBUILDER_OIDC_ISSUER", "https://auth.example.invalid").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("DEBBUILDER_OIDC_CLIENT_ID", "")
OIDC_REDIRECT_URI = os.environ.get("DEBBUILDER_OIDC_REDIRECT_URI", "")
SESSIONS: dict[str, dict] = {}

for d in (USER_WORKFLOWS, RUNS):
    d.mkdir(parents=True, exist_ok=True)

PUBLIC_REPO_PREFIXES = ("/dists/", "/pool/")
PUBLIC_REPO_FILES = {"/repository.gpg", "/install.sh"}

PIPELINE_NOTIFIER = None
LIFECYCLE_NOTIFIER = None
GITHUB_RELEASE_CACHE: dict[str, tuple[float, dict]] = {}
GITHUB_RELEASE_CACHE_LOADED = False
GITHUB_RELEASE_REFRESHING: set[str] = set()
GITHUB_RELEASE_CACHE_LOCK = threading.Lock()
GITHUB_RELEASE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="github-refresh")


def is_public_repo_path(path: str) -> bool:
    return path in PUBLIC_REPO_FILES or path.startswith(PUBLIC_REPO_PREFIXES)


def sanitize_id(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_.+-]", "-", value or "workflow").strip("-")
    return out or "workflow"


def require_abs_safe_path(value: str, what: str = "path") -> str:
    if not value or not value.startswith("/") or ".." in Path(value).parts:
        raise ValueError(f"{what} must be an absolute safe path")
    return value


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


def fetch_packages(repo_url: str, suite: str, component: str, arch: str = "amd64") -> str:
    return repo_summary.fetch_packages(repo_url, suite, component, arch)


def parse_package_version(packages_text: str, package: str) -> str | None:
    return repo_summary.parse_package_version(packages_text, package)


def generate_script(workflow: dict, dry_run=True) -> str:
    script_generator.REPO_SETTINGS_PROVIDER = repo_settings
    return script_generator.generate_script(normalize_recipe(workflow), dry_run=dry_run)


def summarize(workflow: dict) -> dict:
    return repo_summary.summarize(workflow, repo_settings())


def resolve_recipe_release(workflow: dict) -> dict:
    workflow = validate_recipe_metadata(workflow)
    if workflow.get("version_tracking", "latest_release") != "latest_release":
        raise ValueError("automatic builds currently require latest_release tracking")
    release = github_client.latest_release(str(workflow.get("github_repository") or ""), token=github_token(DATA))
    mode = str(workflow.get("version_source") or "tag")
    if mode == "build":
        raise ValueError("build-provided versions require an explicit migration before automatic publication")
    raw = str(release.get("tag") if mode == "tag" else release.get("name") or "")
    if mode == "regex":
        expression = str(workflow.get("version_expression") or "")
        match = re.search(expression, str(release.get("tag") or "") or str(release.get("name") or ""))
        raw = (match.group(1) if match and match.groups() else match.group(0) if match else "")
    version = normalize_github_version(raw)
    archive_url = str(release.get("archive_url") or "")
    if not archive_url.startswith("https://"):
        raise ValueError("GitHub release archive URL is missing or unsafe")
    return {**release, "version": version}


def published_recipe_version(workflow: dict) -> str:
    workflow = normalize_recipe(workflow)
    apt = repo_settings()
    rows = apt_repo.fetch_packages_index(apt["repository"], apt["distribution"], apt["component"], apt["architecture"])
    return apt_repo.latest_published_version(rows, str(workflow.get("package_name") or ""), apt["architecture"])


def version_is_newer(candidate: str, published: str) -> bool:
    if not published:
        return True
    result = subprocess.run(["dpkg", "--compare-versions", candidate, "gt", published], check=False)
    return result.returncode == 0


def notify_pipeline(status: str, package: str, version: str, detail: str = "") -> None:
    if callable(PIPELINE_NOTIFIER):
        PIPELINE_NOTIFIER(status=status, package=package, version=version, detail=detail)


def notify_lifecycle(event: str, **payload) -> None:
    if callable(LIFECYCLE_NOTIFIER):
        LIFECYCLE_NOTIFIER(event, **payload)


def run_recipe_pipeline(workflow: dict, *, dry_run: bool = True) -> dict:
    """Delegate the connected pipeline stages to the build engine."""
    return build_pipeline.run_pipeline(
        workflow, store=BuildStore(DATA / "builds"), dry_run=dry_run,
        recipe_id=str(workflow.get("name") or "recipe"), github_token=github_token(DATA),
        lifecycle_callback=notify_lifecycle,
    )


def _publication_confirmation_for_run(run: dict) -> str:
    artifact = run.get("artifact") or {}
    inspection = artifact.get("inspection") or {}
    package = inspection.get("package") or run.get("package") or run.get("recipe_id") or ""
    run_version = run.get("version") or {}
    version = inspection.get("version") or (run_version.get("debian") if isinstance(run_version, dict) else run_version)
    return f"publish:{package}:{version}"


def run_post_build_automation(run_id: str, *, dry_run: bool, settings: dict | None = None) -> dict:
    """Run optional post-build actions for the exact Build Run that just completed."""
    automation = (settings or app_settings()).get("automation") or {}
    summary = {
        "auto_validate_after_successful_build": bool(automation.get("auto_validate_after_successful_build", False)),
        "auto_publish_after_successful_validation": bool(automation.get("auto_publish_after_successful_validation", False)),
        "validation": None,
        "publication": None,
    }
    if dry_run or not summary["auto_validate_after_successful_build"]:
        return summary
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    if not run or run.get("mode") != "build" or run.get("status") != "success" or not (run.get("artifact") or {}).get("path"):
        return summary
    validation = validate_build_artifact(run_id, {})
    summary["validation"] = validation
    if validation.get("status") != "success" or not summary["auto_publish_after_successful_validation"]:
        return summary
    current = store.load(run_id) or run
    summary["publication"] = publish_build_artifact(run_id, {"confirm": _publication_confirmation_for_run(current)})
    return summary


def run_recipe_pipeline_with_automation(workflow: dict, *, dry_run: bool = True) -> dict:
    result = run_recipe_pipeline(workflow, dry_run=dry_run)
    run_id = str(result.get("run_id") or "")
    if run_id and result.get("status") == "success":
        automation = run_post_build_automation(run_id, dry_run=dry_run)
        if automation.get("validation"):
            result["validation"] = automation["validation"]
        if automation.get("publication"):
            result["publication"] = automation["publication"]
        result["automation"] = automation
    return result


def validate_build_artifact(run_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    return artifact_validation.validate_artifact(
        run_id,
        store=BuildStore(DATA / "builds"),
        previous_artifact=str(payload.get("previous_artifact") or ""),
        profile=str(payload.get("profile") or "bookworm"),
        allowed_previous_roots=(REPOSITORY_ROOT / "pool",),
    )


def publish_build_artifact(run_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    apt = repo_settings()
    return artifact_publication.publish_artifact(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
        confirm=str(payload.get("confirm") or ""),
    )


def reconcile_build_publication(run_id: str, payload: dict | None = None) -> dict:
    apt = repo_settings()
    return artifact_publication.reconcile_publication(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
    )


def read_workflow_file(path: Path) -> dict:
    data = json.loads(path.read_text())
    return validate_recipe_metadata(data)


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
    overrides = load_package_overrides()
    changed = False
    for name, package in list(overrides.items()):
        if isinstance(package, dict) and package.get("recipe") == wid:
            package = dict(package)
            package.pop("recipe", None)
            package["status"] = "recipe_missing"
            overrides[name] = package
            changed = True
    if changed:
        save_package_overrides(overrides)


def list_runs(limit: int = 20) -> list[dict]:
    structured = [
        {"run_id": run["id"], "source": run["workspace"], "updated": run.get("created_at_epoch", 0), "size": 0, "status": run["status"]}
        for run in BuildStore(DATA / "builds").list(limit=limit)
    ]
    legacy = storage.list_runs((RUNS,), limit)
    return sorted(structured + legacy, key=lambda row: row.get("updated") or 0, reverse=True)[:limit]



def packages_file() -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA / "packages.json"


def inventory_file() -> Path:
    return DATA / "repo-current-packages-inventory.json"


def executions_file() -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA / "executions.json"


def load_json_file(path: Path, default):
    return storage.load_json(path, default)


def save_json_file(path: Path, data) -> None:
    storage.save_json(path, data)


def github_repo_from_homepage(homepage: str | None) -> str:
    homepage = homepage or ""
    m = re.search(r"github\.com/([^/]+/[^/#?]+)", homepage)
    return m.group(1).rstrip(".git") if m else ""


def recipe_package_name(recipe: dict) -> str:
    package = recipe.get("package")
    if isinstance(package, dict) and package.get("name"):
        return str(package["name"])
    if recipe.get("package_name"):
        return str(recipe["package_name"])
    for step in recipe.get("steps", []):
        if step.get("type") in {"init_deb_package", "compare_apt_version"} and step.get("package"):
            return str(step.get("package"))
    return ""


def normalized_package_name(value: str) -> str:
    return str(value or "").strip().lower()


def executions_for_package(executions: list[dict], package_name: str) -> list[dict]:
    return [row for row in executions if row.get("package") == package_name or package_name in row.get("id", "")]


def recipe_records_by_package() -> dict[str, dict]:
    """Index Recipe v1 by package.name and report active collisions explicitly."""
    candidates: dict[str, list[dict]] = {}
    for listed in list_workflows():
        rid = str(listed.get("id") or "")
        if not rid or rid.startswith("repo-current-"):
            continue
        path = workflow_path(rid)
        if not path:
            continue
        try:
            workflow = read_workflow_file(path)
        except Exception:
            continue
        package_key = normalized_package_name(recipe_package_name(workflow))
        if not package_key:
            continue
        candidates.setdefault(package_key, []).append({"id": rid, "workflow": workflow})
    records: dict[str, dict] = {}
    for package_key, rows in candidates.items():
        active = [row for row in rows if row["workflow"].get("active") is not False]
        selectable = active if active else rows
        if len(selectable) == 1:
            records[package_key] = selectable[0]
        else:
            records[package_key] = {"id": "", "workflow": None, "ambiguous": True, "candidates": [row["id"] for row in selectable]}
    return records


def detect_recipe_for_package(name: str) -> str:
    record = recipe_records_by_package().get(normalized_package_name(name))
    return str(record.get("id") or "") if record else ""


def github_release_cache_file() -> Path:
    return DATA / "github-release-cache.json"


def load_github_release_cache() -> None:
    global GITHUB_RELEASE_CACHE_LOADED
    with GITHUB_RELEASE_CACHE_LOCK:
        if GITHUB_RELEASE_CACHE_LOADED:
            return
        rows = load_json_file(github_release_cache_file(), {})
        for repository, row in rows.items() if isinstance(rows, dict) else []:
            if isinstance(row, dict) and isinstance(row.get("release"), dict):
                GITHUB_RELEASE_CACHE[str(repository)] = (float(row.get("expires_at") or 0), row["release"])
        GITHUB_RELEASE_CACHE_LOADED = True


def refresh_github_release(repository: str, ttl: int = 300) -> None:
    try:
        release = github_client.latest_release(repository, token=github_token(DATA))
        with GITHUB_RELEASE_CACHE_LOCK:
            GITHUB_RELEASE_CACHE[repository] = (time.time() + ttl, release)
            snapshot = dict(GITHUB_RELEASE_CACHE)
        rows = {name: {"expires_at": expires_at, "release": value} for name, (expires_at, value) in snapshot.items()}
        save_json_file(github_release_cache_file(), rows)
    finally:
        with GITHUB_RELEASE_CACHE_LOCK:
            GITHUB_RELEASE_REFRESHING.discard(repository)


def cached_github_release(repository: str, ttl: int = 300) -> dict | None:
    """Return cached data immediately and refresh stale entries off-request."""
    load_github_release_cache()
    with GITHUB_RELEASE_CACHE_LOCK:
        cached = GITHUB_RELEASE_CACHE.get(repository)
        stale = not cached or cached[0] <= time.time()
        if stale and repository not in GITHUB_RELEASE_REFRESHING:
            GITHUB_RELEASE_REFRESHING.add(repository)
            GITHUB_RELEASE_EXECUTOR.submit(refresh_github_release, repository, ttl)
        return cached[1] if cached else None


def recipe_release_version(recipe: dict, release: dict) -> str:
    source = recipe.get("source") or {}
    version = source.get("version") or {}
    mode = str(version.get("source") or recipe.get("version_source") or "tag")
    if mode == "build":
        return ""
    raw = str(release.get("tag") if mode in {"tag", "regex"} else release.get("name") or "")
    if mode == "regex":
        match = re.search(str(version.get("expression") or recipe.get("version_expression") or ""), raw or str(release.get("name") or ""))
        raw = match.group(1) if match and match.groups() else match.group(0) if match else ""
    return normalize_github_version(raw) if raw else ""


def merge_recipe_metadata(package: dict, record: dict | None) -> dict:
    if not record:
        return package
    if record.get("ambiguous"):
        return {**package, "recipe": "", "recipe_error": {"code": "ambiguous_recipe", "candidates": record["candidates"]}}
    recipe = record["workflow"]
    package_data = recipe["package"]
    source_data = recipe["source"]
    merged = {
        **package, "recipe": record["id"], "description": package_data["description"],
        "architecture": package_data["architecture"], "depends": ", ".join(package_data["runtime_dependencies"]),
        "tracking": source_data["tracking"], "source_ref": source_data["ref"],
    }
    repository = str(source_data.get("repository") or recipe.get("github_repository") or "").strip()
    if not repository:
        return merged
    old_source = dict(merged.get("source") or {})
    same_github_source = old_source.get("type") == "github" and old_source.get("repository") == repository
    source = {
        **old_source,
        "type": "github",
        "repository": repository,
        "url": f"https://github.com/{repository}",
        "ref_type": "release",
    }
    upstream = str(merged.get("upstream_version") or "") if same_github_source else ""
    if source_data.get("tracking", "latest_release") == "latest_release":
        try:
            release = cached_github_release(repository)
            if release:
                upstream = recipe_release_version(recipe, release)
                source.update({
                    "latest_release": str(release.get("tag") or ""),
                    "release": str(release.get("tag") or ""),
                    "release_url": str(release.get("url") or ""),
                })
        except Exception:
            pass
    merged.update({
        "source": source,
        "upstream_version": upstream,
        "version_strategy": f"github_{(source_data.get('version') or {}).get('source') or recipe.get('version_source') or 'tag'}",
    })
    return merged


def load_package_overrides() -> dict:
    rows = load_json_file(packages_file(), [])
    return {str(p.get("name")): p for p in rows if p.get("name")}


def save_package_overrides(packages: dict) -> None:
    save_json_file(packages_file(), sorted(packages.values(), key=lambda p: p.get("name", "")))


def package_from_inventory(row: dict) -> dict:
    name = row.get("Package", "")
    source_repo = github_repo_from_homepage(row.get("Homepage"))
    recipe = detect_recipe_for_package(name)
    return {
        "name": name,
        "apt_version": row.get("Version"),
        "upstream_version": None,
        "source": {"type": "github", "repository": source_repo} if source_repo else {"type": "apt-inventory"},
        "architecture": row.get("Architecture") or "all",
        "status": "ready" if recipe else "recipe_missing",
        "recipe": recipe,
        "last_build": None,
        "description": row.get("Description") or "",
        "filename": row.get("Filename"),
        "depends": row.get("Depends"),
    }


def live_published_index() -> list[dict]:
    apt = repo_settings()
    if "example.invalid" in apt["repository"]:
        return []
    try:
        return apt_repo.fetch_packages_index(apt["repository"], apt["distribution"], apt["component"], apt["architecture"], timeout=5)
    except Exception:
        return []


def build_run_package(run: dict) -> str:
    snapshot = Path(str(run.get("workspace") or "")) / "recipe.json"
    try:
        return recipe_package_name(json.loads(snapshot.read_text()))
    except Exception:
        return str(run.get("recipe_id") or "")


def list_packages() -> list[dict]:
    packages: dict[str, dict] = {}
    recipes = recipe_records_by_package()
    for row in load_json_file(inventory_file(), []):
        if row.get("Package"):
            packages[normalized_package_name(row["Package"])] = package_from_inventory(row)
    live_rows = live_published_index()
    for row in live_rows:
        key = normalized_package_name(row.get("Package"))
        if key:
            packages.setdefault(key, package_from_inventory(row))
    for key, record in recipes.items():
        workflow = record.get("workflow") if record else None
        name = recipe_package_name(workflow) if workflow else key
        packages.setdefault(key, {"name": name, "apt_version": "", "upstream_version": "", "source": {"type": "manual"}, "architecture": "all", "recipe": "", "last_build": None, "description": "", "depends": ""})
    for name, override in load_package_overrides().items():
        package_key = normalized_package_name(name)
        if override.get("deleted"):
            packages.pop(package_key, None)
            continue
        base = packages.get(package_key, {"name": name, "apt_version": None, "upstream_version": None, "source": {"type": "manual"}, "architecture": "all", "recipe": "", "last_build": None, "description": "", "depends": None})
        merged = {**base, **override}
        merged = merge_recipe_metadata(merged, recipes.get(package_key))
        if not merged.get("status") or merged.get("status") == "unknown":
            merged["status"] = "ready" if merged.get("recipe") else "recipe_missing"
        packages[package_key] = merged
    for package_key, package in list(packages.items()):
        packages[package_key] = merge_recipe_metadata(package, recipes.get(package_key))
    live_by_name = {normalized_package_name(row.get("Package")): row for row in live_rows if row.get("Package")}
    build_runs = BuildStore(DATA / "builds").list(limit=1000)
    for run in build_runs:
        key = normalized_package_name(build_run_package(run))
        if key:
            packages.setdefault(key, {"name": key, "apt_version": "", "upstream_version": "", "source": {"type": "manual"}, "architecture": "all", "recipe": "", "last_build": None, "description": "", "depends": ""})
    all_executions = list_executions(limit=1000)
    enriched = []
    apt = repo_settings()
    for pkg in packages.values():
        live = live_by_name.get(normalized_package_name(pkg.get("name"))) or {}
        published_version = live.get("Version") or pkg.get("apt_version", "")
        if live.get("Version"):
            pkg = {**pkg, "apt_version": live.get("Version"), "architecture": live.get("Architecture") or pkg.get("architecture"), "published_filename": live.get("Filename", "")}
        package_key = normalized_package_name(pkg.get("name"))
        matching_runs = [run for run in build_runs if normalized_package_name(build_run_package(run)) == package_key]
        run_state = package_store.summarize_runs(matching_runs, build_pipeline.execution_summary)
        successful, resolved = run_state["successful"], run_state["resolved"]
        candidate = (successful.get("version") or {}).get("debian", "") if successful else ""
        latest_validation = run_state["latest_validation"]
        latest_publication = run_state["latest_publication"]
        verified = bool(successful and latest_validation and latest_validation.get("status") == "success" and latest_validation.get("artifact") == successful["artifact"].get("path"))
        structured_history = run_state["history"]
        structured_ids = {row["id"] for row in structured_history}
        legacy_history = [row for row in executions_for_package(all_executions, str(pkg.get("name") or "")) if row.get("id") not in structured_ids]
        legacy_dry = next((row for row in legacy_history if row.get("action") == "dry-run"), None)
        pkg["history"] = sorted(structured_history + legacy_history, key=lambda row: row.get("updated") or 0, reverse=True)[:200]
        if successful:
            artifact = successful["artifact"]
            inspection = artifact.get("inspection") or {}
            pkg = {
                **pkg, "last_artifact": artifact.get("path", ""), "last_build": build_pipeline.execution_summary(successful),
                "artifact_source": artifact.get("source", "local_build"), "artifact_sha256": artifact.get("sha256", ""),
                "artifact_filename": artifact.get("name", ""),
                "build_method": "upstream_artifact" if artifact.get("source") == "upstream_release" else "recipe",
                "depends": inspection.get("depends") or pkg.get("depends", ""),
                "description": inspection.get("description") or pkg.get("description", ""),
            }
        upstream = (resolved.get("version") or {}).get("upstream", "") if resolved else pkg.get("upstream_version", "")
        built_upstream = (successful.get("version") or {}).get("upstream", "") if successful else ""
        if resolved:
            source_step = next((step for step in resolved.get("steps", []) if step.get("name") == "source"), {})
            source_details = source_step.get("details") or {}
            pkg["source"] = {**(pkg.get("source") or {}), "type": "github", "repository": source_details.get("repository") or (pkg.get("source") or {}).get("repository", ""), "release": source_details.get("ref", ""), "tag": source_details.get("tag", ""), "latest_release": source_details.get("tag", ""), "release_url": source_details.get("release_url", "")}
            if (successful or {}).get("artifact", {}).get("source") == "upstream_release":
                pkg["source"].update({"type": "github_release_asset", "asset_pattern": ((successful["artifact"].get("release_asset") or {}).get("name", ""))})
        state_source = candidate if upstream and built_upstream == upstream else upstream
        candidate_newer = version_is_newer(candidate, published_version) if candidate and published_version and candidate != published_version else None
        item = package_store.enrich_package(pkg, published_version=published_version, source_version=upstream, built_version=candidate, has_verified_build=verified, state_source_version=state_source, candidate_is_newer=candidate_newer)
        item["build"].update({
            "validated": verified,
            "latest_run": run_state["last_real"],
            "latest_run_id": (run_state["last_real"] or {}).get("id", ""),
            "latest_status": (run_state["last_real"] or {}).get("status", ""),
            "last_real": run_state["last_real"],
            "last_dry_run": run_state["last_dry_run"] or legacy_dry,
        })
        item["validation"] = latest_validation
        item["publication"] = latest_publication
        item["lifecycle_display_status"] = (run_state["last_real"] or {}).get("lifecycle_status") or item["lifecycle_state"]
        item["build"]["ready_to_publish"] = item["lifecycle_display_status"] == "ready_to_publish"
        item["repository"].update({"url": apt["repository"], "distribution": apt["distribution"], "component": apt["component"]})
        enriched.append(item)
    return sorted(enriched, key=lambda p: p.get("name", ""))


def get_package(name: str) -> dict | None:
    require_safe_name(name, "package")
    for pkg in list_packages():
        if normalized_package_name(pkg.get("name")) == normalized_package_name(name):
            return dict(pkg)
    return None


def normalize_package(data: dict, existing: dict | None = None) -> dict:
    name = require_safe_name(data.get("name") or (existing or {}).get("name"), "package")
    pkg = dict(existing or {"name": name})
    pkg["name"] = name
    if "architecture" in data:
        arch = data.get("architecture") or "all"
        if arch not in SAFE_ARCH:
            raise ValueError("unsupported architecture")
        pkg["architecture"] = arch
    if "source" in data:
        src = data.get("source") or {"type": "manual"}
        if not isinstance(src, dict):
            raise ValueError("source must be an object")
        if src.get("repository") and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", str(src.get("repository"))):
            raise ValueError("invalid github repository")
        pkg["source"] = src
    if "recipe" in data:
        recipe = data.get("recipe") or ""
        if recipe:
            require_safe_name(recipe, "recipe")
            if not workflow_path(recipe):
                raise ValueError("recipe not found")
        pkg["recipe"] = recipe
    for key in ("apt_version", "upstream_version", "status", "description"):
        if key in data:
            pkg[key] = data.get(key)
    if not pkg.get("status"):
        pkg["status"] = "ready" if pkg.get("recipe") else "recipe_missing"
    return pkg


def create_or_update_package(data: dict, name: str | None = None) -> dict:
    overrides = load_package_overrides()
    current = get_package(name) if name else None
    if name and not current and name not in overrides:
        raise KeyError("package not found")
    pkg = normalize_package({**data, **({"name": name} if name else {})}, current)
    overrides[pkg["name"]] = pkg
    save_package_overrides(overrides)
    return pkg


def associate_workflow_package(wid: str, workflow: dict, previous_id: str = "") -> None:
    package_name = recipe_package_name(workflow).strip()
    if not package_name:
        return
    require_safe_name(package_name, "package name")
    package_key = normalized_package_name(package_name)
    overrides = load_package_overrides()
    for name, row in list(overrides.items()):
        if isinstance(row, dict) and row.get("recipe") in {wid, previous_id} and name != package_name:
            updated = dict(row)
            updated.pop("recipe", None)
            updated["status"] = "recipe_missing"
            overrides[name] = updated
    stored_name = next((name for name in overrides if normalized_package_name(name) == package_key), "")
    current = dict(overrides.get(stored_name) or {})
    inventory_row = next((row for row in load_json_file(inventory_file(), []) if normalized_package_name(row.get("Package")) == package_key), None)
    if not current:
        current = package_from_inventory(inventory_row) if inventory_row else {"name": package_name, "architecture": repo_settings()["architecture"]}
    canonical_name = str((inventory_row or {}).get("Package") or current.get("name") or package_name)
    stored = dict(overrides.get(stored_name) or {})
    source = dict(stored.get("source") or current.get("source") or {})
    repository = str(((workflow.get("source") or {}).get("repository") or workflow.get("github_repository") or "")).strip()
    if repository:
        source.update({"type": "github", "repository": repository})
    if stored_name and stored_name != canonical_name:
        overrides.pop(stored_name, None)
    overrides[canonical_name] = {
        **current,
        **stored,
        "name": canonical_name,
        "recipe": wid,
        "source": source,
        "status": "ready",
    }
    save_package_overrides(overrides)


def inspect_upstream_archive(workflow: dict) -> dict:
    recipe = normalize_recipe(workflow)
    if recipe["artifact"]["mode"] != "upstream_archive":
        raise ValueError("archive inspection requires upstream_archive artifact mode")
    return upstream_archive.inspect(recipe, token=github_token(DATA))


def delete_package(name: str, delete_repo: bool = False, confirm: str = "") -> None:
    require_safe_name(name, "package")
    if delete_repo and confirm != name:
        raise PermissionError("refusing to delete from APT repository without explicit confirmation")
    overrides = load_package_overrides()
    overrides[name] = {"name": name, "deleted": True}
    save_package_overrides(overrides)


def list_recipes() -> list[dict]:
    out = []
    package_by_recipe = {p.get("recipe"): p.get("name") for p in list_packages() if p.get("recipe")}
    for wf in list_workflows():
        rid = wf["id"]
        path = workflow_path(rid)
        valid = False
        package = package_by_recipe.get(rid, "")
        steps = 0
        try:
            recipe = read_workflow_file(path) if path else {}
            steps = len(recipe.get("steps", []))
            valid = True
            package = package or recipe_package_name(recipe)
            out_metadata = {"github_repository": recipe.get("github_repository", ""), "version_tracking": recipe.get("version_tracking", "latest_release"), "active": recipe.get("active", True)}
        except Exception:
            out_metadata = {}
        out.append({**wf, **out_metadata, "package": package, "valid": valid, "steps": steps, "skeleton": rid.startswith("repo-current-")})
    return out


def infer_execution_status(out_text: str, meta: dict | None = None) -> str:
    return storage.infer_execution_status(out_text, meta)


def list_executions(limit: int = 50) -> list[dict]:
    structured_runs = BuildStore(DATA / "builds").list(limit=limit)
    structured = [{**build_pipeline.execution_summary(run), "package": build_run_package(run), "recipe": run.get("recipe_id", "")} for run in structured_runs]
    metadata = load_json_file(executions_file(), [])
    legacy_runs = storage.list_runs((RUNS,), limit=1000)
    legacy = storage.list_executions(legacy_runs, metadata, limit=limit)
    seen = {row["id"] for row in structured}
    return sorted(structured + [row for row in legacy if row.get("id") not in seen], key=lambda row: row.get("updated") or 0, reverse=True)[:limit]


def get_execution(run_id: str) -> dict | None:
    require_safe_name(run_id, "execution")
    build_store = BuildStore(DATA / "builds")
    structured = build_store.load(run_id)
    if structured:
        return build_pipeline.execution_detail(structured, build_store)
    return storage.execution_detail(run_id, list_executions(limit=1000), (RUNS,))


def _execution_error_lines(run: dict) -> list[str]:
    lines = []
    if run.get("error"):
        error = run["error"]
        lines.append(error.get("message", str(error)) if isinstance(error, dict) else str(error))
    for step in run.get("steps", []):
        if step.get("error"):
            error = step["error"]
            lines.append(f"{step.get('name')}: {error.get('message', str(error)) if isinstance(error, dict) else str(error)}")
    return lines


def format_execution_log(run: dict, *, verbosity: str = "normal") -> str:
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    if verbosity == "compact":
        rows = [f"{step['name']}: {step['status']}" for step in run.get("steps", []) if step.get("status") != "pending"]
        rows.extend(f"error: {line}" for line in _execution_error_lines(run))
        return "\n".join(rows) + ("\n" if rows else "")
    if verbosity == "normal":
        rows = [f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}" for step in run.get("steps", []) if step.get("status") != "pending"]
        events = [str(event.get("message") or "") for event in run.get("events", []) if any(marker in str(event.get("message") or "") for marker in ("Build tools", "Dependencies", "Build command", "validation", "publication"))]
        rows.extend(events)
        rows.extend(f"error: {line}" for line in _execution_error_lines(run))
        return "\n".join(row for row in rows if row) + ("\n" if rows else "")
    rows = []
    for step in run.get("steps", []):
        if step.get("status") == "pending":
            continue
        rows.append(f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}")
        details = step.get("details") or {}
        for command in details.get("commands") or []:
            rows.append(f"Build command {command.get('index')}: {command.get('status')} cwd={command.get('working_directory', '')} duration={command.get('duration', '')}s")
            if command.get("stdout"):
                rows.append("stdout:\n" + command["stdout"].rstrip())
            if command.get("stderr"):
                rows.append("stderr:\n" + command["stderr"].rstrip())
    for validation in run.get("validations") or []:
        rows.append(f"validation {validation.get('id', '')}: {validation.get('status', '')}")
        if validation.get("error"):
            rows.append(json.dumps(validation["error"], indent=2, ensure_ascii=False))
    for publication in run.get("publications") or []:
        rows.append(f"publication {publication.get('id', '')}: {publication.get('status', '')}")
    rows.extend(f"error: {line}" for line in _execution_error_lines(run))
    return "\n".join(row for row in rows if row) + ("\n" if rows else "")


def get_execution_log(run_id: str, *, verbosity: str = "normal", after: int = 0) -> dict | None:
    require_safe_name(run_id, "execution")
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    if run:
        if verbosity == "raw":
            chunk = store.log_slice(run_id, after)
            return {**chunk, "complete": run.get("status") not in {"pending", "running"}, "verbosity": verbosity}
        text = format_execution_log(run, verbosity=verbosity)
        start = max(0, min(int(after or 0), len(text)))
        return {"text": text[start:], "offset": len(text), "size": len(text), "complete": run.get("status") not in {"pending", "running"}, "verbosity": verbosity}
    legacy = storage.execution_detail(run_id, list_executions(limit=1000), (RUNS,))
    if not legacy:
        return None
    text = legacy.get("log", "")
    start = max(0, min(int(after or 0), len(text)))
    return {"text": text[start:], "offset": len(text), "size": len(text), "complete": True, "verbosity": "raw" if verbosity == "raw" else verbosity}


def delete_execution_log(run_id: str) -> dict:
    require_safe_name(run_id, "execution")
    store = BuildStore(DATA / "builds")
    if store.load(run_id):
        return store.clear_log_history(run_id)
    original_metadata = load_json_file(executions_file(), [])
    metadata = [row for row in original_metadata if row.get("id") != run_id]
    save_json_file(executions_file(), metadata)
    removed = []
    if len(metadata) != len(original_metadata):
        removed.append("executions.json")
    for suffix in (".out", ".sh"):
        path = RUNS / f"{run_id}{suffix}"
        if path.exists():
            path.unlink()
            removed.append(path.name)
    if not removed:
        raise FileNotFoundError("execution not found")
    return {"id": run_id, "deleted": "legacy_log_history", "removed": removed}


def execution_log_cleanup_candidates() -> list[str]:
    store = BuildStore(DATA / "builds")
    structured = [str(run["id"]) for run in store.list(limit=1_000_000)]
    legacy_rows = storage.list_runs((RUNS,), limit=1_000_000)
    metadata = load_json_file(executions_file(), [])
    legacy = storage.list_executions(legacy_rows, metadata, limit=1_000_000)
    seen = set()
    ids = []
    for run_id in structured + [str(row.get("id")) for row in legacy if row.get("id")]:
        if run_id not in seen:
            ids.append(run_id)
            seen.add(run_id)
    return ids


def delete_execution_logs(run_ids: list[str] | None = None, *, all_runs: bool = False, dry_run: bool = False) -> dict:
    if all_runs:
        run_ids = execution_log_cleanup_candidates()
    run_ids = list(run_ids or [])
    if dry_run:
        return {"count": len(run_ids), "ids": run_ids}
    deleted, errors = [], []
    for run_id in run_ids:
        try:
            deleted.append(delete_execution_log(str(run_id)))
        except Exception as exc:
            errors.append({"id": str(run_id), "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


def execution_steps(log: str) -> list[dict]:
    return storage.execution_steps(log)


def record_execution(run_id: str, workflow: dict, returncode: int, started: float, ended: float, *, status: str | None = None, version: str = "", action: str = "build", notification: bool = True) -> None:
    storage.record_execution(executions_file(), {
        "id": run_id,
        "package": recipe_package_name(workflow),
        "action": action,
        "version": version,
        "status": status or ("success" if returncode == 0 else "failed"),
        "updated": ended,
        "duration": round(ended - started, 3),
    })


def dashboard_summary() -> dict:
    packages = list_packages()
    executions = list_executions(limit=20)
    state_counts: dict[str, int] = {}
    packages_by_state: dict[str, list[dict]] = {}
    for pkg in packages:
        state = pkg.get("lifecycle_display_status") or pkg.get("lifecycle_state") or pkg.get("status") or "unknown"
        state_counts[state] = state_counts.get(state, 0) + 1
        packages_by_state.setdefault(state, []).append({
            "name": pkg.get("name"),
            "source": pkg.get("source"),
            "version": pkg.get("version"),
            "build": pkg.get("build"),
            "repository": pkg.get("repository"),
            "lifecycle_state": state,
            "lifecycle_display_status": state,
        })
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
        "packages_by_state": packages_by_state,
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
    )


def app_settings() -> dict:
    return settings_service.load_app_settings(DATA, settings_defaults())


def repo_settings() -> dict:
    return app_settings()["apt"]


def effective_security() -> dict:
    return app_settings()["security"]


def settings_view() -> dict:
    return settings_service.public_settings_view(
        data_dir=DATA,
        root=ROOT,
        settings=app_settings(),
        port=int(os.environ.get("DEBBUILDER_PORT", "8099")),
    )


def update_settings(payload: dict) -> dict:
    return settings_service.update_settings(DATA, payload, app_settings(), settings_view)

def package_lifecycle_operation(name: str, action: str, payload: dict) -> dict:
    return package_lifecycle.package_lifecycle_operation(
        name,
        action,
        payload,
        get_package=get_package,
        github_client=github_client,
        package_store=package_store,
        deb_inspector=deb_inspector,
        operations=operations,
        repo_settings=repo_settings,
    )


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
    host = os.environ.get("DEBBUILDER_HOST", "0.0.0.0")
    port = int(os.environ.get("DEBBUILDER_PORT", "8099"))
    print(f"DebBuilder Repo UI listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
