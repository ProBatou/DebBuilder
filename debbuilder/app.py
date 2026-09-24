#!/usr/bin/env python3
"""DebBuilder Repo UI.

Stdlib backend:
- serves the admin UI from ./static
- keeps shipped examples separate from user workflows
- executes Recipe v5 through auditable Build Runs
- validates artifacts before publication
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import signal
import stat
import sys
import time
import threading
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import apt_repo, artifact_publication, artifact_validation, auth_service, automation_orchestrator, automation_scheduler, automation_service, automation_status, build_pipeline, builtin_recipe, command_containment, deb_inspector, dependency_preparation, execution_projection, execution_recovery, execution_service, inspectors, maintenance, notifications, package_service, recipe_store, resource_limits, settings_service, storage, storage_inventory, storage_pruning, system_diagnostics, upstream_archive, upstream_detection, upstream_observation, validation_oci, validation_service, workspace_cleanup
from .api_errors import canonical_error_payload
from .automation_ledger import AutomationLedger
from .upstream_detection import AutomationDetectionService
from .build_models import utc_now
from .build_store import BuildStore, canonical_recipe_sha256
from .execution_manager import DEFAULT_SHUTDOWN_TIMEOUT, ExecutionManager, ExecutionManagerError
from .http_handler import create_handler
from .lifecycle import MutationGate, MutationGateClosed
from .recipe_schema import RecipeDocumentError, recipe_document_for_storage, require_safe_name, validate_recipe_metadata
from .settings_store import SettingsDocumentError, SessionSecretError, cookie_secret, github_token, load_secrets, load_settings, oidc_client_secret, prepare_cookie_secret, resource_repair_security, secrets_path, settings_path
from .runtime import RuntimeConfig, listeners_overlap

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


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
PUBLIC_REPOSITORY_ACTIVE = False

PUBLIC_REPO_PREFIXES = ("/dists/", "/pool/")
PUBLIC_REPO_FILES = {"/repository.gpg", "/install.sh"}

NOTIFICATION_SERVICE = None
UPSTREAM_OBSERVATION_SERVICE = None
APPLICATION_MUTATION_GATE = None
APPLICATION_MAINTENANCE_SERVICE = None
APPLICATION_VALIDATION_MANAGER = None
APPLICATION_AUTOMATION_SCHEDULER = None
APPLICATION_AUTOMATION_ORCHESTRATOR = None


class RunAdmissionError(RuntimeError):
    """Structured HTTP-facing failure while admitting a Build Run."""

    def __init__(self, code: str, message: str, *, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class RecipeStartupValidationError(RuntimeError):
    """Fail-closed startup refusal when persisted Recipes are not all usable."""

    def __init__(self, report: recipe_store.RecipeDirectoryValidationReport):
        super().__init__("Persisted Recipe validation failed; Build/Test admission remains closed")
        self.code = "recipe_validation_failed"
        self.report = report

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "validation": self.report.as_dict()}


def _bounded_startup_value(value, *, limit: int) -> str:
    bounded = str(value or "").replace("\r", " ").replace("\n", " ")
    bounded = re.sub(r"/[^\s'\"]+", "<path>", bounded)
    return bounded[:limit]


def recipe_startup_diagnostic(exc: Exception) -> dict:
    """Return operator-useful Recipe startup details without paths or contents."""
    failures: list[dict] = []
    if isinstance(exc, RecipeStartupValidationError):
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
    path = urllib.parse.urlparse(handler.path).path
    if status >= 400 and path.startswith("/api/"):
        data = canonical_error_payload(data, status, path)
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
    security = effective_security() if auth_mode is None else {"auth_mode": auth_mode}
    return auth_service.is_request_authorized(
        headers,
        auth_mode=auth_mode,
        effective_security=security,
        auth_header=AUTH_HEADER,
        session_user=oidc_session_user,
    )


def is_settings_repair_authorized(headers: dict) -> bool:
    """Authorize only the bounded resource repair path from current-v1 state."""
    security = resource_repair_security(DATA)
    return is_request_authorized(headers, auth_mode=security["auth_mode"])


def notify_lifecycle(event: str, **payload) -> None:
    notification_service().notify_build_lifecycle(event, **payload)


def run_post_build_automation(run_id: str, *, dry_run: bool, settings: dict | None = None, store: BuildStore | None = None) -> dict:
    selected_settings = settings or app_settings()

    def admit_automatic_validation(validation_run_id: str, payload: dict) -> dict:
        manager = APPLICATION_VALIDATION_MANAGER
        if manager is None:
            return {
                "status": "failed",
                "error": {"code": "validation_manager_unavailable", "message": "Validation manager is unavailable", "details": {}},
            }
        try:
            return manager.admit(
                validation_run_id,
                payload,
                automatic=True,
                publish_after_success=bool(
                    (selected_settings.get("automation") or {}).get("auto_publish_after_successful_validation", False)
                ),
            )
        except validation_service.ValidationAdmissionError as exc:
            return {"status": "failed", "error": exc.as_dict()}

    def publish_with_lifecycle_lease(publication_run_id: str, payload: dict) -> dict:
        try:
            lease = APPLICATION_MUTATION_GATE.lease() if APPLICATION_MUTATION_GATE is not None else None
            if lease is None:
                return publish_build_artifact(publication_run_id, payload)
            with lease:
                return publish_build_artifact(publication_run_id, payload)
        except MutationGateClosed as exc:
            return {
                "status": "failed",
                "error": {"code": exc.code, "message": str(exc), "details": {}},
            }

    return automation_service.run_post_build(
        run_id,
        dry_run=dry_run,
        settings=selected_settings,
        store=store or BuildStore(DATA / "builds"),
        validate=admit_automatic_validation,
        publish=publish_with_lifecycle_lease,
    )


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
        completed_run = store.load(run_id)
        if isinstance((completed_run or {}).get("automation"), dict):
            orchestrator = APPLICATION_AUTOMATION_ORCHESTRATOR
            if orchestrator is not None:
                orchestrator.on_run_terminal(run_id)
            return result
        return automation_service.complete_with_automation(
            result,
            dry_run=dry_run,
            automate=lambda submitted_run_id, *, dry_run: run_post_build_automation(
                submitted_run_id, dry_run=dry_run, store=store,
            ),
            notify_completion=lambda completed: notification_service().notify_automatic_completion(completed),
        )
    finally:
        request_maintenance(cleanup=True)


def create_execution_manager(*, store: BuildStore | None = None, queue_capacity: int = 8, execute=None) -> ExecutionManager:
    """Construct, but do not start, the server's single execution manager."""
    callback = execute or (lambda run_id, **kwargs: execute_queued_recipe_run(run_id, **kwargs))
    return ExecutionManager(store or BuildStore(DATA / "builds"), queue_capacity=queue_capacity, execute=callback)


def create_validation_manager(*, store: BuildStore | None = None, queue_capacity: int = 8, execute=None):
    callback = execute or execute_validation_attempt
    selected_store = store or BuildStore(DATA / "builds")
    return validation_service.ValidationManager(
        selected_store,
        execute=callback,
        registry_root=DATA / "validation-containers",
        workspace_root=ROOT,
        allowed_previous_roots=(REPOSITORY_ROOT / "pool",),
        queue_capacity=queue_capacity,
        resume_publication=lambda run_id, attempt_id: continue_validation_publication(
            run_id, attempt_id, store=selected_store,
        ),
        on_terminal=lambda run_id, attempt_id: (
            APPLICATION_AUTOMATION_ORCHESTRATOR.on_validation_terminal(run_id, attempt_id)
            if APPLICATION_AUTOMATION_ORCHESTRATOR is not None else None
        ),
    )


def prepare_application_directories() -> None:
    """Prepare mutable application data through the one startup-owned path."""
    RUNTIME.prepare_data_directories()


def prepare_recipes_for_startup(*, shutdown_check=None):
    """Validate canonical v5 user Recipes, then reconcile the managed built-in."""
    validation = recipe_store.validate_recipe_directory(USER_WORKFLOWS)
    if not validation.ok:
        raise RecipeStartupValidationError(validation)
    if shutdown_check is not None:
        shutdown_check()
    reconciliation = builtin_recipe.reconcile_builtin_recipe(USER_WORKFLOWS)
    if shutdown_check is not None:
        shutdown_check()
    return validation, reconciliation


def prepare_authentication_for_startup() -> None:
    """Prepare OIDC signing state after recovery and before HTTP serving."""
    if effective_security().get("auth_mode") == "oidc":
        prepare_cookie_secret(DATA)


def prepare_settings_for_startup() -> dict:
    """Validate canonical v1 Settings and secrets without writing either store."""
    return settings_service.validate_app_settings_storage(DATA, settings_defaults())


def start_execution_manager(
    http_server,
    manager: ExecutionManager | None = None,
    *,
    validation_manager=None,
    prepare_directories: bool = True,
    shutdown_check=None,
) -> ExecutionManager:
    """Recover Runs, prepare Recipes, then attach/start one gated manager."""
    if getattr(http_server, "execution_manager", None) is not None:
        raise RuntimeError("HTTP server already has an execution manager")
    if getattr(http_server, "mutation_gate", None) is None:
        http_server.mutation_gate = MutationGate()
    if prepare_directories:
        prepare_application_directories()
        if shutdown_check is not None:
            shutdown_check()
    global APPLICATION_VALIDATION_MANAGER
    selected = manager or create_execution_manager()
    selected_validation = validation_manager or create_validation_manager(store=selected.store)
    recovery = execution_recovery.recover_startup(selected.store)
    container_recovery = validation_oci.recover_owned_containers(
        DATA / "validation-containers", workspace=ROOT,
    )
    attempt_recovery = dependency_preparation.recover_interrupted_attempts(
        selected.store, container_recovery.resolved_identities,
        inventory_trustworthy=container_recovery.inventory_trustworthy,
    )
    attempt_blocker = None
    if attempt_recovery["blockers"]:
        attempt_blocker = {
            "code": "validation_attempt_recovery_unresolved",
            "message": "Build/Test admission is blocked until validation attempt recovery is resolved",
            "details": {"unresolved_count": len(attempt_recovery["blockers"])},
        }
    blockers = [value for value in (
        recovery.admission_blocker, container_recovery.admission_blocker, attempt_blocker,
    ) if value]
    admission_blocker = None
    if len(blockers) == 1:
        admission_blocker = blockers[0]
    elif blockers:
        admission_blocker = {
            "code": "startup_recovery_unresolved",
            "message": "Build/Test admission is blocked until workload recovery is resolved",
            "details": {"blockers": blockers},
        }
    authorization = getattr(http_server, "cleanup_authorization", None)
    if authorization is None:
        authorization = workspace_cleanup.CleanupAuthorization()
        http_server.cleanup_authorization = authorization
    authorization.update_global_blocker(admission_blocker)
    if getattr(http_server, "storage_inventory", None) is None:
        http_server.storage_inventory = create_storage_inventory()
    if shutdown_check is not None:
        shutdown_check()
    prepare_settings_for_startup()
    if shutdown_check is not None:
        shutdown_check()
    prepare_authentication_for_startup()
    if shutdown_check is not None:
        shutdown_check()
    try:
        recipe_validation, reconciliation = prepare_recipes_for_startup(shutdown_check=shutdown_check)
    except (RecipeStartupValidationError, recipe_store.RecipeStoreError, builtin_recipe.BuiltinRecipeError) as exc:
        LOGGER.error("Recipe startup failure: %s", json.dumps(recipe_startup_diagnostic(exc), sort_keys=True))
        raise
    if admission_blocker is None:
        dependency_preparation.SUPERVISOR.open_admission()
    else:
        dependency_preparation.SUPERVISOR.block("startup recovery is unresolved")
    # Validation admission must exist before recovered Builds can finish and
    # request automatic validation.  Publish ownership first so the outer
    # lifecycle can always stop this worker during a partial startup.
    http_server.validation_manager = selected_validation
    selected_validation.start(admission_blocker=admission_blocker)
    APPLICATION_VALIDATION_MANAGER = selected_validation
    if shutdown_check is not None:
        shutdown_check()
    selected.start(admission_blocker=admission_blocker)
    if shutdown_check is not None:
        shutdown_check()
    http_server.recipe_validation = recipe_validation.as_dict()
    http_server.builtin_recipe_reconciliation = {
        "action": reconciliation.action,
        "definition_version": reconciliation.definition_version,
        "previous_definition_version": reconciliation.previous_definition_version,
    }
    http_server.execution_recovery = recovery.as_dict()
    http_server.validation_container_recovery = container_recovery.as_dict()
    http_server.validation_attempt_recovery = attempt_recovery
    http_server.execution_manager = selected
    http_server.validation_manager = selected_validation
    return selected


def _enqueue_failure_details(exc: BaseException, run_id: str) -> dict:
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


def _prepare_run_admission(manager: ExecutionManager, workflow: dict) -> tuple[dict, dict]:
    """Perform slow shared checks without holding the containment gate."""
    try:
        canonical = recipe_document_for_storage(workflow)
    except RecipeDocumentError as exc:
        raise RunAdmissionError(exc.code, str(exc), status=422, details={"path": exc.path}) from exc
    try:
        selected_settings = app_settings()
    except (SettingsDocumentError, resource_limits.ResourceLimitError, ValueError) as exc:
        diagnostic = exc.as_dict() if hasattr(exc, "as_dict") else {
            "code": "invalid_settings", "message": str(exc), "path": "$", "details": {},
        }
        raise RunAdmissionError(
            "settings_invalid",
            "Build/Test admission is blocked because stored Settings are invalid",
            status=503,
            details=diagnostic,
        ) from exc
    effective, _origins = resource_limits.resolve_policy(
        selected_settings["resource_limits"], canonical["resource_limits"],
    )
    reconciled = _reconcile_cleanup_for_admission(False)
    probe_root = manager.store.root if manager.store.root.is_dir() else manager.store.root.parent
    capability = command_containment.resource_limit_capability(
        effective, workspace=probe_root, refresh=resource_limits.enforcement_required(effective),
    )
    _reconcile_cleanup_for_admission(reconciled)
    if resource_limits.enforcement_required(effective) and not capability["available"]:
        raise RunAdmissionError(
            "resource_limits_unavailable",
            "Requested resource limits cannot be enforced on this host",
            status=503,
            details={
                "backend": capability["backend"],
                "requested_controls": capability["requested_controls"],
                "reason": capability["reason"],
            },
        )
    return canonical, resource_limits.admission_contract(
        selected_settings["resource_limits"], canonical["resource_limits"], capability,
    )


def _cleanup_admission_error(reason: str) -> RunAdmissionError:
    return RunAdmissionError(
        "execution_recovery_unresolved",
        "Build/Test admission is blocked because containment cleanup is unresolved",
        status=503,
        details={"backend": "systemd_cgroup", "reason": reason},
    )


def _reconcile_cleanup_for_admission(already_reconciled: bool) -> bool:
    """Attempt at most one bounded reconciliation for this admission."""
    cleanup_blocker = command_containment.containment_cleanup_blocker()
    if cleanup_blocker and not already_reconciled:
        try:
            command_containment.reconcile_cleanup_blockers()
        except Exception:
            # The registry remains the authority.  Any unexpected failure is
            # fail-closed and exposed only through the canonical bounded error.
            LOGGER.exception("Containment cleanup reconciliation failed during admission")
        already_reconciled = True
        cleanup_blocker = command_containment.containment_cleanup_blocker()
    if cleanup_blocker:
        raise _cleanup_admission_error(cleanup_blocker)
    return already_reconciled


def _require_cleanup_admission_clear() -> None:
    """Final gate-held check immediately before durable Run admission."""
    cleanup_blocker = command_containment.containment_cleanup_blocker()
    if cleanup_blocker:
        raise _cleanup_admission_error(cleanup_blocker)


def enqueue_recipe_run(manager: ExecutionManager | None, workflow: dict, *, dry_run: bool = True) -> dict:
    """Reserve capacity, persist exactly one Run, and submit it asynchronously."""
    if manager is None:
        raise RunAdmissionError(
            "execution_manager_unavailable", "Execution manager is unavailable", status=503,
        )
    canonical, resource_contract = _prepare_run_admission(manager, workflow)
    manual_source_provenance = _resolve_manual_source_provenance(canonical)
    return _enqueue_prepared_recipe_run(
        manager, canonical, resource_contract, dry_run=dry_run,
        manual_source_provenance=manual_source_provenance,
    )


def _resolve_manual_source_provenance(canonical: dict) -> dict | None:
    """Resolve exact latest-release identity without consulting display observations."""
    if canonical["source"]["tracking"] != "latest_release":
        return None
    try:
        return upstream_detection.detect_upstream(
            canonical, token=github_token(DATA),
        )["identity"]
    except upstream_detection.UpstreamDetectionError as exc:
        unavailable = exc.classification in {"rate_limited", "upstream_unavailable"}
        raise RunAdmissionError(
            exc.code,
            "Build/Test admission could not establish immutable upstream provenance",
            status=503 if unavailable else 422,
            details={"classification": exc.classification},
        ) from exc


@command_containment.containment_safety_serialized
def _enqueue_prepared_recipe_run(
    manager: ExecutionManager,
    canonical: dict,
    resource_contract: dict,
    *,
    dry_run: bool,
    manual_source_provenance: dict | None = None,
) -> dict:
    """Atomically check blocker state and perform the normal admission path."""
    _require_cleanup_admission_clear()
    try:
        with manager.reserve() as reservation:
            run_id = manager.store.allocate_run_id()
            reservation.track(run_id)
            try:
                run = build_pipeline.create_pipeline_run(
                    canonical,
                    store=manager.store,
                    dry_run=dry_run,
                    recipe_id=canonical["name"],
                    run_id=run_id,
                    resource_contract=resource_contract,
                    manual_source_provenance=manual_source_provenance,
                )
            except BaseException as exc:
                failure = {
                    "code": "execution_creation_persistence_unresolved",
                    "message": "Build Run creation failed after durable state may have changed",
                    "ownership_phase": "creation",
                    **_enqueue_failure_details(exc, run_id),
                }
                resolved = reservation.confirm_terminal(run_id) or reservation.confirm_absent(run_id)
                if not resolved:
                    resolved = reservation.resolve_failed_creation(run_id, failure)
                if not resolved:
                    reservation.transfer_unresolved(run_id, failure)
                raise
            try:
                reservation.submit(run_id)
            except Exception as exc:
                terminalization_error = None
                try:
                    _mark_enqueue_failed(manager.store, run_id, exc)
                except BaseException as failure:
                    terminalization_error = failure
                if not reservation.confirm_terminal(run_id):
                    failure_details = _enqueue_failure_details(exc, run_id)
                    if terminalization_error is not None:
                        failure_details["terminalization_exception_type"] = type(terminalization_error).__name__
                    reservation.transfer_unresolved(run_id, {
                        "code": "execution_enqueue_persistence_unresolved",
                        "message": "Build Run admission failure remains non-terminal",
                        **failure_details,
                    })
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


def enqueue_preallocated_recipe_run(
    manager: ExecutionManager | None,
    workflow: dict,
    *,
    run_id: str,
    dry_run: bool,
    origin: dict,
    automation: dict,
    created_callback=None,
) -> dict:
    """Create/link/submit one preallocated automated Run through canonical owners.

    A queue-full or shutdown rejection deliberately leaves the exact pending
    Run intact so durable orchestration can retry admission without creating a
    second workspace.
    """
    if manager is None:
        raise RunAdmissionError("execution_manager_unavailable", "Execution manager is unavailable", status=503)
    canonical, resource_contract = _prepare_run_admission(manager, workflow)
    return _enqueue_prepared_preallocated_recipe_run(
        manager, canonical, resource_contract,
        run_id=run_id, dry_run=dry_run, origin=origin, automation=automation,
        created_callback=created_callback,
    )


@command_containment.containment_safety_serialized
def _enqueue_prepared_preallocated_recipe_run(
    manager: ExecutionManager,
    canonical: dict,
    resource_contract: dict,
    *,
    run_id: str,
    dry_run: bool,
    origin: dict,
    automation: dict,
    created_callback=None,
) -> dict:
    """Atomically check blocker state and admit the exact preallocated Run."""
    _require_cleanup_admission_clear()
    existing = manager.store.load(run_id)
    if existing is None:
        try:
            run = build_pipeline.create_pipeline_run(
                canonical,
                store=manager.store,
                dry_run=dry_run,
                recipe_id=canonical["name"],
                run_id=run_id,
                resource_contract=resource_contract,
                origin=origin,
                automation=automation,
            )
        except FileExistsError as exc:
            existing = manager.store.load(run_id)
            if existing is None:
                raise RunAdmissionError(
                    "execution_creation_persistence_unresolved",
                    "Preallocated Run workspace exists without a readable Run",
                    status=503,
                    details={"run_id": run_id},
                ) from exc
            run = existing
    else:
        run = existing
    expected_mode = "dry_run" if dry_run else "build"
    if (
        run.get("id") != run_id
        or run.get("recipe_id") != canonical["name"]
        or run.get("recipe_sha256") != canonical_recipe_sha256(canonical)
        or run.get("mode") != expected_mode
        or run.get("origin") != origin
        or run.get("automation") != automation
    ):
        raise RunAdmissionError(
            "automation_run_binding_invalid",
            "Preallocated Run does not match its immutable automation admission",
            status=409,
            details={"run_id": run_id},
        )
    if run.get("status") not in {"pending", "queued", "running", "cancelling", "prepared", "success", "failed", "cancelled"}:
        raise RunAdmissionError("automation_run_state_invalid", "Preallocated Run state is invalid", status=409)
    if created_callback is not None:
        created_callback(run)
    if run["status"] != "pending":
        return {"run_id": run_id, "status": run["status"], "duplicate": True}
    try:
        manager.submit(run_id)
    except ExecutionManagerError as exc:
        if exc.code == "execution_queue_full":
            raise RunAdmissionError(exc.code, "The Build/Test queue is full; retry is delayed", status=429, details=exc.details) from exc
        if exc.code in {"execution_manager_not_accepting", "execution_manager_stopped"}:
            raise RunAdmissionError("execution_manager_unavailable", "Execution manager is unavailable", status=503) from exc
        if exc.code == "build_run_already_submitted":
            return {"run_id": run_id, "status": (manager.store.load(run_id) or run)["status"], "duplicate": True}
        raise RunAdmissionError("execution_enqueue_failed", "The Build Run could not be submitted", status=500) from exc
    return {"run_id": run_id, "status": "queued", "duplicate": False}


def cleanup_workspaces(*, authorization=None, should_stop=None) -> dict:
    """Use the current DATA/settings; cleanup failures never change a Run result."""
    try:
        result = workspace_cleanup.apply_retention(
            BuildStore(DATA / "builds"), app_settings().get("workspace_cleanup"),
            authorization=authorization or workspace_cleanup.OPEN_CLEANUP_AUTHORIZATION,
            should_stop=should_stop,
        )
        for error in result["errors"]:
            logging.getLogger(__name__).warning("Workspace cleanup: %s", error)
        return result
    except Exception as exc:
        logging.getLogger(__name__).exception("Workspace retention sweep failed")
        return {"cleaned": [], "retained": [], "skipped": [], "errors": [{"error": str(exc)}]}


def maintain_run_storage(*, authorization=None, should_stop=None) -> dict:
    """Run the ordered destructive pass owned by the maintenance worker."""
    stop_requested = should_stop or (lambda: False)
    cleanup = cleanup_workspaces(
        authorization=authorization,
        should_stop=stop_requested,
    )
    pruning = {"pruned": [], "recovered": [], "already_pruned": [], "skipped": [], "manifests_pruned": [], "errors": []}
    if not stop_requested():
        try:
            apt = repo_settings()
            pruning = storage_pruning.apply_pruning(
                BuildStore(DATA / "builds"),
                repo_root=REPOSITORY_ROOT,
                distribution=apt["distribution"],
                component=apt["component"],
                policy=app_settings().get("workspace_cleanup"),
                authorization=authorization or workspace_cleanup.OPEN_CLEANUP_AUTHORIZATION,
                should_stop=stop_requested,
            )
            for error in pruning["errors"]:
                LOGGER.warning("Run storage pruning: %s", error)
        except Exception as exc:
            LOGGER.exception("Run storage pruning sweep failed")
            pruning["errors"].append({"error": str(exc)})
    return {"workspace_cleanup": cleanup, "storage_pruning": pruning}


def _cancellation_result(run_id: str, status: str, metadata: dict) -> dict:
    allowed = ("code", "reason", "phase", "stage", "requested_at", "completed_at")
    return {
        "run_id": execution_projection.safe_text(run_id, limit=256),
        "status": execution_projection.safe_text(status, limit=32),
        **{
            key: execution_projection.safe_text(metadata[key], limit=128)
            for key in allowed if metadata.get(key) is not None
        },
    }


def cancel_execution(manager: ExecutionManager | None, run_id: str, *, maintenance_request=None) -> dict:
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
        orchestrator = APPLICATION_AUTOMATION_ORCHESTRATOR
        if orchestrator is not None:
            try:
                orchestrator.on_run_terminal(run_id)
            except Exception:
                LOGGER.exception("Automation continuation failed after queued Run cancellation")
        try:
            (maintenance_request or request_maintenance)(cleanup=True)
        except Exception:
            # Cancellation is already durable.  A broken notification boundary
            # must not turn that successful state transition into an HTTP error.
            LOGGER.exception("Maintenance request failed after queued cancellation")
        return _cancellation_result(run_id, "cancelled", result.get("cancellation") or {})
    if outcome == "active_cancel_requested":
        return _cancellation_result(run_id, "cancelling", result.get("cancellation") or {})
    if outcome == "already_cancelled":
        orchestrator = APPLICATION_AUTOMATION_ORCHESTRATOR
        if orchestrator is not None:
            try:
                orchestrator.on_run_terminal(run_id)
            except Exception:
                LOGGER.exception("Automation continuation failed after cancelled Run replay")
        return _cancellation_result(run_id, "cancelled", result.get("cancellation") or {})
    raise ExecutionCancellationError(
        "execution_cancellation_failed", "Execution manager returned an unknown cancellation result", status=500,
        details={"run_id": run_id},
    )


def request_maintenance(*, refresh: bool = True, cleanup: bool = False) -> None:
    """Signal the one application-owned worker without waiting for filesystem work."""
    service = APPLICATION_MAINTENANCE_SERVICE
    if service is not None:
        service.request(refresh=refresh, cleanup=cleanup)


def create_maintenance_service(http_server):
    authorization = http_server.cleanup_authorization
    mutation_gate = getattr(http_server, "mutation_gate", None)
    service = None

    def cleanup():
        try:
            lease = mutation_gate.lease() if mutation_gate is not None else None
            if lease is None:
                return maintain_run_storage(
                    authorization=authorization,
                    should_stop=service.stop_requested,
                )
            with lease:
                return maintain_run_storage(
                    authorization=authorization,
                    should_stop=service.stop_requested,
                )
        except MutationGateClosed:
            return {"status": "skipped", "reason": "application_shutting_down"}

    service = maintenance.MaintenanceService(
        http_server.storage_inventory,
        cleanup=cleanup,
    )
    return service


def create_automation_scheduler(http_server):
    """Construct the sole application-owned periodic detection scheduler."""
    global APPLICATION_AUTOMATION_ORCHESTRATOR
    automation = app_settings().get("automation", {})
    ledger = AutomationLedger(DATA)
    detector = AutomationDetectionService(
        USER_WORKFLOWS, ledger, observation_service=upstream_observation_service(),
    )
    manager = getattr(http_server, "execution_manager", None)

    orchestrator = automation_orchestrator.AutomationOrchestrator(
        USER_WORKFLOWS,
        ledger,
        manager.store if manager is not None else BuildStore(DATA / "builds"),
        execution_manager=lambda: getattr(http_server, "execution_manager", None),
        enqueue_run=enqueue_preallocated_recipe_run,
        validation_manager=lambda: getattr(http_server, "validation_manager", None),
        publish=publish_build_artifact,
        notify_completion=lambda result: notification_service().notify_automatic_completion(result),
        admission_open=lambda: bool(
            http_server.mutation_gate.accepting
            and getattr(http_server, "execution_manager", None) is not None
            and getattr(http_server.execution_manager, "accepting", False)
        ),
        mutation_lease=http_server.mutation_gate.lease,
    )
    APPLICATION_AUTOMATION_ORCHESTRATOR = orchestrator
    return automation_scheduler.AutomationScheduler(
        USER_WORKFLOWS, detector, ledger, automation_scheduler.AutomationRetryStore(DATA),
        mutation_gate=http_server.mutation_gate,
        admission_open=lambda: bool(
            http_server.mutation_gate.accepting
            and manager is not None
            and getattr(manager, "accepting", False)
        ),
        token_provider=lambda: github_token(DATA),
        enabled=automation.get("upstream_checks_enabled", True),
        interval_seconds=automation.get("upstream_check_interval_seconds", 3600),
        concurrency=automation.get("upstream_check_concurrency", 4),
        orchestrator=orchestrator,
    )


def create_storage_inventory():
    return storage_inventory.StorageInventory(
        DATA,
        REPOSITORY_ROOT,
        policy_provider=lambda: app_settings().get("workspace_cleanup", {}),
    )


def admit_validation_attempt(manager, run_id: str, payload: dict | None = None) -> dict:
    if manager is None:
        raise validation_service.ValidationAdmissionError(
            "validation_manager_unavailable", "Validation manager is unavailable", status=503,
        )
    return manager.admit(run_id, payload)


def get_validation_attempt(run_id: str, attempt_id: str, *, manager=None) -> dict:
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    if not run:
        raise validation_service.ValidationAdmissionError(
            "build_run_not_found", "Build Run was not found", status=404,
        )
    attempt = validation_service.load_attempt(store, run_id, attempt_id)
    blocker = manager.blocker if manager is not None else None
    return validation_service.public_attempt(attempt, run=run, store=store, blocker=blocker)


def cancel_validation_attempt(manager, run_id: str, attempt_id: str) -> dict:
    if manager is None:
        raise validation_service.ValidationCancellationError(
            "validation_manager_unavailable", "Validation manager is unavailable", status=503,
        )
    return manager.cancel(run_id, attempt_id)


def _notify_validation_best_effort(result: dict) -> None:
    try:
        notification_service().notify_validation_result(result)
    except Exception:
        LOGGER.exception("Validation notification failed after durable completion")


def _append_validation_log_best_effort(store: BuildStore, run_id: str, attempt_id: str, status: str) -> None:
    try:
        store.append_log_line(run_id, f"validation {attempt_id}: {status}")
    except Exception:
        LOGGER.exception("Validation log append failed after durable completion")


def continue_validation_publication(run_id: str, attempt_id: str, *, store: BuildStore | None = None):
    """Consume one durable automatic-publication intent; safe to replay after a crash."""
    selected_store = store or BuildStore(DATA / "builds")
    try:
        attempt = validation_service.load_attempt(selected_store, run_id, attempt_id)
        automation = validation_service.load_automation(selected_store, run_id, attempt_id)
        if not isinstance(attempt, dict) or not isinstance(automation, dict):
            return None
        publication_state = automation.get(
            "publication_state",
            validation_service.PUBLICATION_PENDING
            if automation.get("publish_after_success")
            else validation_service.PUBLICATION_NOT_REQUESTED,
        )
        if (
            attempt.get("status") != "success"
            or publication_state != validation_service.PUBLICATION_PENDING
        ):
            return None
        run = selected_store.load(run_id)
        if not run:
            return None
        if isinstance(run.get("automation"), dict):
            orchestrator = APPLICATION_AUTOMATION_ORCHESTRATOR
            if orchestrator is not None:
                orchestrator.on_validation_terminal(run_id, attempt_id)
            return None
        payload = {"confirm": automation_service.publication_confirmation(run)}
        gate = APPLICATION_MUTATION_GATE
        lease = gate.lease() if gate is not None else None
        if lease is None:
            result = publish_build_artifact(run_id, payload)
        else:
            with lease:
                result = publish_build_artifact(run_id, payload)
        if result.get("status") == "success":
            validation_service.complete_publication_intent(selected_store, run_id, attempt_id)
        return result
    except MutationGateClosed:
        LOGGER.info("Automatic publication remains pending because shutdown has started")
    except Exception:
        # The pending bit deliberately remains durable. Startup replay is safe:
        # publication verifies an already-present exact repository identity.
        LOGGER.exception("Automatic publication continuation failed for %s/%s", run_id, attempt_id)
    return None


def execute_validation_attempt(run_id: str, attempt_id: str, event: threading.Event, automation: dict | None = None) -> dict:
    """Execute one already-admitted attempt through preparation and offline lifecycle."""
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    if not run:
        raise artifact_validation.ValidationError("build_run_not_found", "Build Run was not found")
    attempt = validation_service.load_attempt(store, run_id, attempt_id)
    artifact = Path(str((run.get("artifact") or {}).get("path") or ""))
    if run.get("status") != "success" or not artifact.is_file():
        raise artifact_validation.ValidationError("artifact_not_available", "A successful Build Run with an artifact is required")
    profile = attempt["inputs"]["profile"]
    preparation_previous = (
        Path(run["workspace"]) / "validation" / attempt_id / "preparation-previous.deb"
        if attempt["inputs"].get("previous_artifact") else None
    )
    store.append_log_line(run_id, f"validation {attempt_id}: running dependency preparation")
    try:
        recipe = validate_recipe_metadata(json.loads((Path(run["workspace"]) / "recipe.json").read_text()))
        attempt = dependency_preparation.prepare_runtime_dependencies(
            run_id,
            attempt_id,
            store=store,
            current_artifact=artifact,
            previous_artifact=preparation_previous,
            profile_name=profile,
            repositories=recipe.get("runtime_apt_repositories") or [],
            registry_root=DATA / "validation-containers",
            cancellation_event=event,
        )
    except Exception as exc:
        current = validation_service.load_attempt(store, run_id, attempt_id)
        projected = validation_service.public_attempt(current, run=store.load(run_id), store=store)
        _notify_validation_best_effort(projected)
        request_maintenance(refresh=True, cleanup=True)
        if current["status"] in {"failed", "cancelled", "cancelling"}:
            _append_validation_log_best_effort(store, run_id, attempt_id, current["status"])
            return projected
        code = str(getattr(exc, "code", "validation_preparation_failed"))
        raise artifact_validation.ValidationError(code, str(exc), details=getattr(exc, "details", {})) from exc
    attempt = validation_service.load_attempt(store, run_id, attempt_id)
    prepared = validation_service.load_prepared(store, run_id, attempt_id, attempt=attempt)
    registered = False
    lifecycle_started = False
    lifecycle_completion_attempted = False
    try:
        dependency_preparation.SUPERVISOR.register(attempt_id, event)
        registered = True
        dependency_preparation.begin_lifecycle_attempt(store, run_id, attempt_id, prepared)
        lifecycle_started = True
        store.append_log_line(run_id, f"validation {attempt_id}: running offline lifecycle")
        result = artifact_validation.validate_artifact(
            run_id,
            store=store,
            previous_artifact=str(preparation_previous or ""),
            profile=profile,
            allowed_previous_roots=(REPOSITORY_ROOT / "pool",),
            prepared_dependencies=prepared,
            attempt_id=attempt_id,
            registry_root=DATA / "validation-containers",
            cancellation_event=event,
        )
        lifecycle_completion_attempted = True
        completed = dependency_preparation.complete_lifecycle_attempt(store, run_id, attempt_id, result)
        if completed["status"] == "cancelled" and result.get("status") != "cancelled":
            result["status"] = "cancelled"
            result["error"] = {
                "code": "validation_lifecycle_cancelled",
                "message": "Offline lifecycle validation was cancelled",
                "details": {},
            }
    except Exception as exc:
        if lifecycle_started and not lifecycle_completion_attempted:
            lifecycle_completion_attempted = True
            code = str(getattr(exc, "code", "validation_lifecycle_failed"))
            completed = dependency_preparation.complete_lifecycle_attempt(store, run_id, attempt_id, {
                "status": "failed",
                "error": {"code": code, "message": str(exc), "details": getattr(exc, "details", {})},
            })
            if completed["status"] == "cancelled":
                projected = validation_service.public_attempt(completed, run=store.load(run_id), store=store)
                _notify_validation_best_effort(projected)
                _append_validation_log_best_effort(store, run_id, attempt_id, "cancelled")
                request_maintenance(refresh=True, cleanup=True)
                return projected
        if isinstance(exc, artifact_validation.ValidationError):
            raise
        raise artifact_validation.ValidationError(
            str(getattr(exc, "code", "validation_lifecycle_failed")),
            str(exc),
            details=getattr(exc, "details", {}),
        ) from exc
    finally:
        if registered:
            dependency_preparation.SUPERVISOR.unregister(attempt_id)
    result["attempt_id"] = attempt_id
    result["dependency_preparation"] = {
        "status": "success",
        "package_count": len(prepared["packages"]),
        "profile": prepared["profile_name"],
        "image_id": prepared["image"]["id"],
    }
    _notify_validation_best_effort(result)
    _append_validation_log_best_effort(store, run_id, attempt_id, result.get("status", "failed"))
    # Durable pending intent survives shutdown, crashes, and best-effort side
    # effect failures. Startup asks the same idempotent continuation to resume.
    continue_validation_publication(run_id, attempt_id, store=store)
    request_maintenance(refresh=True, cleanup=True)
    return result


def publish_build_artifact(run_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    apt = repo_settings()
    result = artifact_publication.publish_artifact(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
        confirm=str(payload.get("confirm") or ""),
        validation_recovery_blocker=(
            APPLICATION_VALIDATION_MANAGER.blocker
            if APPLICATION_VALIDATION_MANAGER is not None else None
        ),
    )
    try:
        notification_service().notify_publication_result(result)
    except Exception:
        LOGGER.exception("Publication notification failed after durable completion")
    request_maintenance(refresh=True, cleanup=True)
    return execution_projection.public_publication(result)


def reconcile_build_publication(run_id: str, payload: dict | None = None) -> dict:
    apt = repo_settings()
    result = artifact_publication.reconcile_publication(
        run_id, store=BuildStore(DATA / "builds"), repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"], component=apt["component"],
    )
    request_maintenance(refresh=True, cleanup=True)
    return execution_projection.public_publication(result)


def read_workflow_file(path: Path) -> dict:
    return recipe_store.load_recipe(path)


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


def _wake_automation_after_recipe_save(previous: dict | None, current: dict) -> None:
    """Coalesce advisory observation after a canonical Recipe changes."""
    if previous and canonical_recipe_sha256(previous) == canonical_recipe_sha256(current):
        return
    scheduler = APPLICATION_AUTOMATION_SCHEDULER
    if scheduler is None:
        return
    try:
        scheduler.request_observation(current["name"])
    except (ValueError, automation_scheduler.AutomationSchedulerError):
        # The Recipe is already durable. A stopped/busy scheduler will
        # rediscover it on the next normal startup/pass.
        pass


def import_recipe_json(recipe, *, replace: bool = False) -> dict:
    """Create or explicitly replace a user Recipe from canonical JSON."""
    preflight = recipe_document_for_storage(recipe)
    workflow_id = preflight["name"]
    builtin_recipe.require_user_recipe_id(workflow_id)
    destination = workflow_path(workflow_id, for_write=True)
    assert destination is not None
    previous = None
    with storage.locked_path(destination):
        canonical = recipe_document_for_storage(recipe)
        existing = workflow_path(workflow_id)
        if existing:
            is_user_recipe = existing.resolve().parent == USER_WORKFLOWS.resolve()
            if not is_user_recipe:
                raise PermissionError("shipped recipes are read-only and cannot be replaced")
            if not replace:
                raise FileExistsError("recipe id already exists; explicit replacement is required")
            previous = read_workflow_file(existing)
        canonical = recipe_store.save_recipe(destination, canonical)
    associate_workflow_package(workflow_id, canonical)
    _wake_automation_after_recipe_save(previous, canonical)
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
    previous_recipe = None
    previous_path = workflow_path(previous_id or workflow_id)
    if previous_path is not None:
        try:
            previous_recipe = read_workflow_file(previous_path)
        except (OSError, TypeError, ValueError, recipe_store.RecipeStoreError):
            previous_recipe = None
    destination = workflow_path(workflow_id, for_write=True)
    assert destination is not None
    if builtin_recipe.is_builtin_recipe_id(workflow_id):
        stored = builtin_recipe.update_builtin_recipe(destination, canonical)
        normalized = validate_recipe_metadata(stored)
    else:
        normalized = validate_recipe_metadata(canonical)
        stored = canonical
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
    _wake_automation_after_recipe_save(previous_recipe, normalized)
    return {"ok": True, "id": workflow_id, "path": str(destination)}


def automation_projection_service() -> automation_status.AutomationStatusService:
    scheduler = APPLICATION_AUTOMATION_SCHEDULER
    ledger = getattr(scheduler, "ledger", None) or AutomationLedger(DATA)
    retry_store = getattr(scheduler, "retry_store", None) or automation_scheduler.AutomationRetryStore(DATA)
    return automation_status.AutomationStatusService(
        USER_WORKFLOWS,
        ledger,
        retry_store,
        BuildStore(DATA / "builds"),
        scheduler=lambda: APPLICATION_AUTOMATION_SCHEDULER,
    )


def get_automation_status(recipe_id: str) -> dict:
    return automation_projection_service().status(recipe_id)


def check_automation_now(recipe_id: str) -> dict:
    return automation_projection_service().check_now(recipe_id)


def retry_automation(recipe_id: str, payload: dict) -> dict:
    return automation_projection_service().retry(recipe_id, payload)


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


class InspectionReadError(RuntimeError):
    """Safe, typed failure while reading a persisted inspector input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _inspection_file_bound(path: Path, maximum: int, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InspectionReadError(code) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise InspectionReadError(code)


def get_recipe_inspection(recipe_id: str) -> dict | None:
    """Read one bounded canonical Recipe and project its safe operator view."""
    from .recipe_schema import require_safe_name
    require_safe_name(recipe_id, "Recipe ID")
    path = workflow_path(recipe_id)
    if path is None:
        return None
    _inspection_file_bound(path, inspectors.MAX_RECIPE_BYTES, "recipe_inspection_unavailable")
    try:
        recipe = recipe_store.load_recipe(path)
        if recipe["name"] != recipe_id:
            raise ValueError("Recipe identity mismatch")
        observation = upstream_observation.UpstreamObservationStore(DATA).projection(
            recipe_id, canonical_recipe_sha256(recipe),
        )
        source = "user" if path.parent == USER_WORKFLOWS else "example"
        return inspectors.inspect_recipe(recipe, source=source, observation=observation)
    except (OSError, ValueError, KeyError, TypeError, recipe_store.RecipeStoreError) as exc:
        raise InspectionReadError("recipe_inspection_unavailable") from exc


def _latest_validation_inspection(store: BuildStore, run_id: str) -> tuple[dict | None, int, bool]:
    """Count bounded directory entries; load only one canonical attempt record."""
    root = store.run_dir(run_id) / "manifests" / "validation-attempts"
    parent = root.parent
    if parent.exists() or parent.is_symlink():
        _inspection_directory_bound(parent, "run_inspection_unavailable")
    if not root.exists() and not root.is_symlink():
        return None, 0, False
    if root.is_symlink() or not root.is_dir():
        raise InspectionReadError("run_inspection_unavailable")
    count = 0
    latest = None
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                count += 1
                if count > inspectors.MAX_VALIDATION_ATTEMPTS:
                    return None, inspectors.MAX_VALIDATION_ATTEMPTS, True
                if not entry.is_dir(follow_symlinks=False):
                    raise InspectionReadError("run_inspection_unavailable")
                require_safe_name(entry.name, "Validation attempt ID")
                if latest is None or entry.name > latest:
                    latest = entry.name
        attempt = validation_service.load_attempt(store, run_id, latest) if latest else None
        return attempt, count, False
    except (OSError, ValueError, validation_service.ValidationAdmissionError) as exc:
        raise InspectionReadError("run_inspection_unavailable") from exc


def _inspection_directory_bound(path: Path, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InspectionReadError(code) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InspectionReadError(code)


def get_run_inspection(run_id: str, manager=None) -> dict | None:
    """Read one bounded Run, not its logs, commands, or full attempt history."""
    require_safe_name(run_id, "Build Run ID")
    store = BuildStore(DATA / "builds")
    run_directory = store.run_dir(run_id)
    run_path = run_directory / "run.json"
    if not run_directory.exists() and not run_directory.is_symlink():
        return None
    _inspection_directory_bound(run_directory, "run_inspection_unavailable")
    if not run_path.exists() and not run_path.is_symlink():
        return None
    _inspection_file_bound(run_path, inspectors.MAX_RUN_BYTES, "run_inspection_unavailable")
    try:
        run = store.load(run_id)
        if run is None or run.get("id") != run_id:
            raise ValueError("Run identity mismatch")
        attempt, count, truncated = _latest_validation_inspection(store, run_id)
        cancellation_owned = bool(manager is not None and (
            run_id == manager.active_run_id or run_id in manager.queued_run_ids
        ))
        return inspectors.inspect_run(run, validation=attempt, validation_count=count,
                                      validation_inventory_truncated=truncated,
                                      cancellation_owned=cancellation_owned)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InspectionReadError("run_inspection_unavailable") from exc


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


def upstream_observation_service() -> upstream_observation.UpstreamObservationService:
    global UPSTREAM_OBSERVATION_SERVICE
    current = UPSTREAM_OBSERVATION_SERVICE
    if current is None or current.store.data_dir.resolve() != DATA.resolve():
        UPSTREAM_OBSERVATION_SERVICE = upstream_observation.UpstreamObservationService(
            upstream_observation.UpstreamObservationStore(DATA),
            resolver=upstream_detection.detect_upstream,
        )
    return UPSTREAM_OBSERVATION_SERVICE


def package_projection_service() -> package_service.PackageService:
    try:
        observation_rows = upstream_observation_service().store.read()["recipes"]
    except upstream_observation.UpstreamObservationError:
        observation_rows = {}

    def observation_projection(recipe_id: str, recipe: dict) -> dict | None:
        row = observation_rows.get(recipe_id)
        if not row or row.get("recipe_sha256") != canonical_recipe_sha256(validate_recipe_metadata(recipe)):
            return None
        return row

    return package_service.PackageService(
        data_dir=DATA,
        workspace_root=ROOT,
        list_workflows=list_workflows,
        workflow_path=workflow_path,
        read_workflow=read_workflow_file,
        repo_settings=repo_settings,
        observation_lookup=observation_projection,
        run_projector=project_run_for_read,
    )


def project_run_for_read(run: dict, store: BuildStore) -> dict:
    """Combine bounded Validation eligibility with durable publication facts."""
    projected = validation_service.project_run(
        run,
        store,
        blocker=APPLICATION_VALIDATION_MANAGER.blocker if APPLICATION_VALIDATION_MANAGER is not None else None,
    )
    apt = repo_settings()
    projected.update(artifact_publication.publication_evidence_projection(
        run,
        repo_root=REPOSITORY_ROOT,
        distribution=apt["distribution"],
        component=apt["component"],
    ))
    # Package identity may require the immutable Recipe snapshot.  Resolve it
    # before the public projection removes the local workspace pointer.
    projected["package"] = build_run_package(run)
    return projected


def recipe_package_name(recipe: dict) -> str:
    return package_service.recipe_package_name(recipe)


def live_published_index() -> list[dict]:
    apt = repo_settings()
    rows = apt_repo.local_packages_index(
        REPOSITORY_ROOT, apt["distribution"], apt["component"], apt["architecture"],
    )
    return rows or []


def build_run_package(run: dict) -> str:
    return package_service.build_run_package(run)


def list_packages(*, include_history: bool = False) -> list[dict]:
    packages = package_projection_service().list_packages(
        include_history=include_history,
        live_rows=live_published_index(),
    )
    recipe_ids = [str(package.get("recipe") or "") for package in packages if package.get("recipe")]
    statuses = automation_projection_service().statuses(recipe_ids) if recipe_ids else {}
    return [{**package, "automation": statuses.get(str(package.get("recipe") or ""))} for package in packages]


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
    recipe = recipe_document_for_storage(workflow)
    if recipe["artifact"]["mode"] != "upstream_archive":
        raise ValueError("archive inspection requires upstream_archive artifact mode")
    return upstream_archive.inspect(recipe, token=github_token(DATA))


def delete_package(name: str) -> None:
    package_projection_service().mark_deleted(name)


def list_recipes() -> list[dict]:
    out = []
    package_by_recipe = {p.get("recipe"): p.get("name") for p in list_packages() if p.get("recipe")}
    try:
        observation_rows = upstream_observation_service().store.read()["recipes"]
    except upstream_observation.UpstreamObservationError:
        observation_rows = {}
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
            candidate = observation_rows.get(rid)
            observation = candidate if candidate and candidate.get("recipe_sha256") == canonical_recipe_sha256(recipe) else None
            out_metadata = {
                "repository": source.get("repository", ""),
                "tracking": source.get("tracking", "latest_release"),
                "active": recipe.get("active", True),
                "observation": observation,
            }
        except Exception:
            out_metadata = {}
        out.append({**wf, **out_metadata, "package": package, "valid": valid})
    return out


def refresh_upstream_observation(recipe_id: str) -> dict:
    """Explicitly refresh display-only upstream state for any valid linked Recipe."""
    require_safe_name(recipe_id, "Recipe ID")
    path = workflow_path(recipe_id)
    if path is None:
        raise FileNotFoundError("recipe not found")
    recipe = validate_recipe_metadata(read_workflow_file(path))
    if recipe.get("name") != recipe_id:
        raise ValueError("Recipe identity is invalid")
    service = upstream_observation_service()
    result = service.observe(
        recipe_id, recipe, path, token=github_token(DATA),
    )
    if not result.get("observation_persisted"):
        raise upstream_observation.UpstreamObservationError(
            str(result.get("observation_persistence_error") or "observation_state_unavailable"),
            "Upstream observation could not be persisted",
        )
    return service.projection(recipe_id, recipe) or {}


def list_executions(limit: int = 50, *, structured_runs: list[dict] | None = None) -> list[dict]:
    store = BuildStore(DATA / "builds")
    runs = structured_runs if structured_runs is not None else store.list(limit=1_000_000)
    projected = [project_run_for_read(run, store) for run in runs]
    return execution_service.list_executions(
        store,
        build_run_package,
        limit=limit,
        runs=projected,
    )


def get_execution(run_id: str) -> dict | None:
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    projected = project_run_for_read(run, store) if run else None
    execution = execution_service.get_execution(store, run_id, run=projected)
    return execution


def get_execution_log(run_id: str, *, verbosity: str = "normal", after: int = 0) -> dict | None:
    store = BuildStore(DATA / "builds")
    run = store.load(run_id)
    projected = project_run_for_read(run, store) if run else None
    return execution_service.get_log(store, run_id, verbosity=verbosity, after=after, run=projected)


def delete_execution_log(run_id: str, *, authorization=None) -> dict:
    result = execution_service.delete_log(
        BuildStore(DATA / "builds"), run_id, authorization=authorization,
    )
    request_maintenance(refresh=True)
    return result


def delete_execution_logs(
    run_ids: list[str] | None = None,
    *,
    all_runs: bool = False,
    dry_run: bool = False,
    authorization=None,
) -> dict:
    result = execution_service.delete_logs(
        BuildStore(DATA / "builds"),
        run_ids,
        all_runs=all_runs,
        dry_run=dry_run,
        authorization=authorization,
    )
    if not dry_run:
        request_maintenance(refresh=True)
    return result


def storage_snapshot(inventory=None) -> dict:
    """Return cached observer state; this path never performs collection."""
    selected = inventory
    if selected is None:
        return storage_inventory.StorageInventory(DATA, REPOSITORY_ROOT).snapshot()
    return selected.snapshot()


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
    return load_settings(DATA, settings_defaults())


def repo_settings() -> dict:
    return app_settings()["apt"]


def effective_security() -> dict:
    return app_settings()["security"]


def system_diagnostics_snapshot(server=None) -> dict:
    """Read-only snapshot shared by HTTP and future support tooling."""
    def bounded_document(path: Path) -> None:
        try:
            if path.lstat().st_size > 256 * 1024:
                raise ValueError("diagnostic document bound exceeded")
        except FileNotFoundError:
            pass

    def bounded_settings():
        bounded_document(settings_path(DATA))
        return app_settings()

    def bounded_secrets():
        bounded_document(secrets_path(DATA))
        return load_secrets(DATA)

    return system_diagnostics.build_system_diagnostics(
        runtime=replace(RUNTIME, data=DATA, repository_root=REPOSITORY_ROOT),
        load_configuration=bounded_settings, load_secret_document=bounded_secrets,
        execution_manager=getattr(server, "execution_manager", None),
        mutation_gate=APPLICATION_MUTATION_GATE,
        validation_manager=APPLICATION_VALIDATION_MANAGER,
        scheduler=APPLICATION_AUTOMATION_SCHEDULER,
        orchestrator=APPLICATION_AUTOMATION_ORCHESTRATOR,
        repository_active=PUBLIC_REPOSITORY_ACTIVE,
    )


def settings_view() -> dict:
    settings = app_settings()
    view = settings_service.public_settings_view(
        data_dir=DATA,
        settings=settings,
    )
    capability = command_containment.cached_containment_capability()
    view["resource_limits_status"] = {
        "valid": True,
        "diagnostic": None,
        "capability": {
            "backend": capability.backend,
            "available": capability.available,
            "reason": capability.reason,
        },
    }
    return view


def update_settings(payload: dict) -> dict:
    def refresh_repository(current, updated):
        if not PUBLIC_REPOSITORY_ACTIVE or current["apt"] == updated["apt"]:
            return
        from .local_repository_bootstrap import bootstrap_repository
        bootstrap_repository(
            repository_root=RUNTIME.repository_root, data_root=DATA,
            suite=updated["apt"]["distribution"], component=updated["apt"]["component"],
            gpg_home=Path(os.environ.get("GNUPGHOME") or DATA / ".gnupg"),
            public_url=updated["apt"]["repository"],
        )

    settings_service.update_settings(DATA, payload, settings_defaults(), before_save=refresh_repository)
    request_maintenance(refresh=True)
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


def _install_shutdown_signal_handlers(wakeup_fd: int, *, requested_state=None):
    """Install handlers that only wake the lifecycle shutdown coordinator."""
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    def request_shutdown(signum, _frame):
        if requested_state is not None:
            requested_state[0] = True
        try:
            os.write(wakeup_fd, bytes((signum,)))
        except BlockingIOError:
            pass

    installed = []
    try:
        for signum in previous:
            signal.signal(signum, request_shutdown)
            installed.append(signum)
    except BaseException:
        for signum in installed:
            signal.signal(signum, previous[signum])
        raise

    def restore():
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


class _LifecycleShutdownRequested(Exception):
    """Internal control flow for a signal observed at a startup boundary."""


def _graceful_shutdown_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("shutdown timeout must be a finite non-negative number")
    value = float(timeout)
    if not math.isfinite(value) or value < 0:
        raise ValueError("shutdown timeout must be a finite non-negative number")
    return value


def _remaining_shutdown_time(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def serve_application(
    handler_class,
    *,
    server_factory=ThreadingHTTPServer,
    repository_handler_class=None,
    repository_server_factory=None,
    manager_factory=create_execution_manager,
    maintenance_factory=create_maintenance_service,
    automation_scheduler_factory=None,
    install_signal_handlers=True,
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
) -> int:
    """Own startup, serving, and shutdown under one diagnostic target."""
    global APPLICATION_AUTOMATION_ORCHESTRATOR, APPLICATION_AUTOMATION_SCHEDULER, APPLICATION_MAINTENANCE_SERVICE, APPLICATION_MUTATION_GATE, APPLICATION_VALIDATION_MANAGER, PUBLIC_REPOSITORY_ACTIVE
    graceful_timeout = _graceful_shutdown_timeout(shutdown_timeout)
    http_server = None
    repository_server = None
    repository_thread = None
    repository_stop = threading.Event()
    repository_failure = []
    manager = None
    validation_manager = None
    maintenance_service = None
    automation_detection_scheduler = None
    cleanup_started = threading.Event()
    shutdown_requested = threading.Event()
    signal_requested_state = [False]
    lifecycle_condition = threading.Condition()
    serving_started = False
    signal_read_fd = None
    signal_write_fd = None
    signal_coordinator = None
    restore_signals = None
    primary_failure = None
    cleanup_failures = []
    shutdown_result = None
    mutation_gate = MutationGate()
    mutation_result = None
    previous_mutation_gate = APPLICATION_MUTATION_GATE
    previous_maintenance_service = APPLICATION_MAINTENANCE_SERVICE
    previous_automation_scheduler = APPLICATION_AUTOMATION_SCHEDULER
    previous_automation_orchestrator = APPLICATION_AUTOMATION_ORCHESTRATOR
    APPLICATION_MUTATION_GATE = mutation_gate

    try:
        if install_signal_handlers:
            signal_read_fd, signal_write_fd = os.pipe()
            os.set_blocking(signal_write_fd, False)
            restore_signals = _install_shutdown_signal_handlers(
                signal_write_fd,
                requested_state=signal_requested_state,
            )

            def coordinate_signal_shutdown():
                nonlocal serving_started
                try:
                    os.read(signal_read_fd, 1)
                except BaseException as exc:
                    cleanup_failures.append(("signal shutdown coordination", exc))
                    return
                shutdown_requested.set()
                selected_scheduler = automation_detection_scheduler
                if selected_scheduler is not None:
                    try:
                        selected_scheduler.stop()
                    except BaseException as exc:
                        cleanup_failures.append(("automation scheduler admission shutdown", exc))
                mutation_gate.begin_shutdown()
                selected_manager = manager
                if selected_manager is not None and selected_manager.worker is not None:
                    try:
                        selected_manager.begin_shutdown()
                    except BaseException as exc:
                        cleanup_failures.append(("execution admission shutdown", exc))
                selected_validation = validation_manager
                if selected_validation is not None:
                    try:
                        selected_validation.begin_shutdown()
                    except BaseException as exc:
                        cleanup_failures.append(("validation admission shutdown", exc))
                selected_maintenance = maintenance_service
                if selected_maintenance is not None:
                    selected_maintenance.stop()
                with lifecycle_condition:
                    while not serving_started and not cleanup_started.is_set():
                        lifecycle_condition.wait()
                    should_stop_server = serving_started and not cleanup_started.is_set()
                    selected_server = http_server
                if should_stop_server and selected_server is not None:
                    repository_stop.set()
                    try:
                        selected_server.shutdown()
                    except BaseException as exc:
                        cleanup_failures.append(("signal shutdown coordination", exc))

            signal_coordinator = threading.Thread(
                target=coordinate_signal_shutdown,
                name="signal-shutdown-coordinator",
                daemon=False,
            )
            signal_coordinator.start()

        def check_shutdown_requested():
            if signal_requested_state[0] or shutdown_requested.is_set():
                raise _LifecycleShutdownRequested()

        prepare_application_directories()
        check_shutdown_requested()
        if repository_handler_class is not None and listeners_overlap(
            RUNTIME.host, RUNTIME.port, RUNTIME.repository_host, RUNTIME.repository_port
        ):
            raise ValueError("Admin and repository listeners cannot bind the same host and port")
        manager = manager_factory()
        check_shutdown_requested()
        http_server = server_factory((RUNTIME.host, RUNTIME.port), handler_class)
        if repository_handler_class is not None:
            factory = repository_server_factory or server_factory
            repository_server = factory((RUNTIME.repository_host, RUNTIME.repository_port), repository_handler_class)
            repository_server.timeout = 0.2
        http_server.mutation_gate = mutation_gate
        http_server.cleanup_authorization = workspace_cleanup.CleanupAuthorization()
        http_server.storage_inventory = create_storage_inventory()
        check_shutdown_requested()
        start_execution_manager(
            http_server,
            manager,
            prepare_directories=False,
            shutdown_check=check_shutdown_requested,
        )
        validation_manager = getattr(http_server, "validation_manager", None)
        check_shutdown_requested()
        scheduler_factory = automation_scheduler_factory or create_automation_scheduler
        automation_detection_scheduler = scheduler_factory(http_server)
        http_server.automation_scheduler = automation_detection_scheduler
        APPLICATION_AUTOMATION_SCHEDULER = automation_detection_scheduler
        automation_detection_scheduler.start()
        check_shutdown_requested()
        maintenance_service = maintenance_factory(http_server)
        http_server.maintenance_service = maintenance_service
        APPLICATION_MAINTENANCE_SERVICE = maintenance_service
        maintenance_service.start()
        check_shutdown_requested()
        maintenance_service.request(cleanup=True)
        print(f"DebBuilder Repo UI listening on http://{RUNTIME.host}:{RUNTIME.port}")
        if repository_server is not None:
            print(f"DebBuilder public APT repository listening on http://{RUNTIME.repository_host}:{RUNTIME.repository_port}")
            def serve_repository():
                try:
                    while not repository_stop.is_set():
                        repository_server.handle_request()
                except BaseException as exc:
                    repository_failure.append(exc)
                    repository_stop.set()
                    with lifecycle_condition:
                        while not serving_started and not cleanup_started.is_set():
                            lifecycle_condition.wait()
                        should_stop_admin = serving_started and not cleanup_started.is_set()
                    if should_stop_admin:
                        http_server.shutdown()

            repository_thread = threading.Thread(target=serve_repository, name="public-repository-listener", daemon=False)
            repository_thread.start()
            PUBLIC_REPOSITORY_ACTIVE = True
        with lifecycle_condition:
            check_shutdown_requested()
            serving_started = True
            lifecycle_condition.notify_all()
        try:
            http_server.serve_forever()
        except BaseException as exc:
            primary_failure = exc
        if repository_failure and primary_failure is None:
            primary_failure = RuntimeError("Public repository listener failed")
            primary_failure.__cause__ = repository_failure[0]
    except _LifecycleShutdownRequested:
        pass
    except BaseException as exc:
        primary_failure = exc
    finally:
        if automation_detection_scheduler is not None:
            try:
                automation_detection_scheduler.stop()
            except BaseException as exc:
                cleanup_failures.append(("automation scheduler admission shutdown", exc))
        mutation_gate.begin_shutdown()
        graceful_deadline = time.monotonic() + graceful_timeout
        repository_stop.set()
        cleanup_started.set()
        with lifecycle_condition:
            lifecycle_condition.notify_all()
        if signal_write_fd is not None:
            try:
                os.write(signal_write_fd, b"\0")
            except (BlockingIOError, OSError):
                pass
        if validation_manager is None and http_server is not None:
            validation_manager = getattr(http_server, "validation_manager", None)
        manager_started = manager is not None and manager.worker is not None
        if manager_started:
            try:
                manager.begin_shutdown()
            except BaseException as exc:
                cleanup_failures.append(("execution admission shutdown", exc))
        if validation_manager is not None:
            try:
                validation_manager.begin_shutdown()
            except BaseException as exc:
                cleanup_failures.append(("validation admission shutdown", exc))
        if automation_detection_scheduler is not None and automation_detection_scheduler.is_alive():
            automation_detection_scheduler.join(_remaining_shutdown_time(graceful_deadline))
            if automation_detection_scheduler.is_alive():
                failure = TimeoutError("automation scheduler did not quiesce within the graceful shutdown target")
                cleanup_failures.append(("automation scheduler shutdown", failure))
                LOGGER.error("Incomplete automation scheduler shutdown: %s", failure)
        try:
            validation_stopped = dependency_preparation.SUPERVISOR.shutdown(
                _remaining_shutdown_time(graceful_deadline)
            )
            if not validation_stopped:
                failure = TimeoutError(
                    "validation preparation/lifecycle did not stop within the graceful shutdown target"
                )
                cleanup_failures.append(("validation shutdown", failure))
                LOGGER.error("Incomplete validation shutdown: %s", failure)
        except BaseException as exc:
            cleanup_failures.append(("validation shutdown", exc))
        if validation_manager is not None:
            try:
                if not validation_manager.shutdown(_remaining_shutdown_time(graceful_deadline)):
                    failure = TimeoutError("validation worker did not stop within the graceful shutdown target")
                    cleanup_failures.append(("validation manager shutdown", failure))
                    LOGGER.error("Incomplete validation manager shutdown: %s", failure)
                    validation_manager.shutdown(None)
            except BaseException as exc:
                cleanup_failures.append(("validation manager shutdown", exc))
                validation_manager.shutdown(None)
        try:
            mutation_result = mutation_gate.wait_for_quiescence(
                _remaining_shutdown_time(graceful_deadline)
            )
            if not mutation_result["complete"]:
                failure = TimeoutError(
                    "durable HTTP mutations did not quiesce within the graceful shutdown target"
                )
                cleanup_failures.append(("HTTP mutation shutdown", failure))
                LOGGER.error("Incomplete HTTP mutation shutdown: %s", failure)
                mutation_result = mutation_gate.wait_for_quiescence(None)
        except BaseException as exc:
            cleanup_failures.append(("HTTP mutation shutdown", exc))
            mutation_result = mutation_gate.wait_for_quiescence(None)
        if manager_started:
            try:
                shutdown_result = manager.shutdown(timeout=_remaining_shutdown_time(graceful_deadline))
                if shutdown_result.get("timed_out") or not shutdown_result["complete"]:
                    failure = RuntimeError(json.dumps(shutdown_result, sort_keys=True))
                    cleanup_failures.append(("execution manager shutdown", failure))
                    LOGGER.error("Incomplete execution manager shutdown: %s", failure)
                if not shutdown_result["complete"]:
                    shutdown_result = manager.shutdown(timeout=None)
                    if not shutdown_result["complete"]:
                        cleanup_failures.append((
                            "execution manager ownership continuation",
                            RuntimeError(json.dumps(shutdown_result, sort_keys=True)),
                        ))
            except BaseException as exc:
                cleanup_failures.append(("execution manager shutdown", exc))
                try:
                    shutdown_result = manager.shutdown(timeout=None)
                except BaseException as continuation_exc:
                    cleanup_failures.append(("execution manager ownership continuation", continuation_exc))
            worker = manager.worker
            if worker is not None and getattr(worker, "is_alive", lambda: False)():
                worker.join()
        if maintenance_service is not None:
            maintenance_service.stop()
            if maintenance_service.is_alive():
                maintenance_service.join(_remaining_shutdown_time(graceful_deadline))
                if maintenance_service.is_alive():
                    failure = TimeoutError(
                        "storage maintenance did not quiesce within the graceful shutdown target"
                    )
                    cleanup_failures.append(("storage maintenance shutdown", failure))
                    LOGGER.error("Incomplete storage maintenance shutdown: %s", failure)
                    maintenance_service.join()
        if http_server is not None:
            try:
                http_server.server_close()
            except BaseException as exc:
                cleanup_failures.append(("HTTP server close", exc))
        if repository_thread is not None:
            repository_thread.join(_remaining_shutdown_time(graceful_deadline))
            if repository_thread.is_alive():
                repository_thread.join()
                cleanup_failures.append(("repository listener shutdown", TimeoutError("Repository listener stopped after deadline")))
        if repository_server is not None:
            try:
                repository_server.server_close()
            except BaseException as exc:
                cleanup_failures.append(("repository HTTP server close", exc))
        if signal_coordinator is not None and signal_coordinator.is_alive():
            signal_coordinator.join(_remaining_shutdown_time(graceful_deadline))
            if signal_coordinator.is_alive():
                failure = TimeoutError(
                    "signal shutdown coordinator did not stop within the graceful shutdown target"
                )
                cleanup_failures.append(("signal shutdown coordination", failure))
                LOGGER.error("Incomplete signal shutdown coordination: %s", failure)
                signal_coordinator.join()
        if restore_signals is not None:
            try:
                restore_signals()
            except BaseException as exc:
                cleanup_failures.append(("signal handler restoration", exc))
        for descriptor in (signal_write_fd, signal_read_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    cleanup_failures.append(("signal wakeup pipe close", exc))
        APPLICATION_MUTATION_GATE = previous_mutation_gate
        PUBLIC_REPOSITORY_ACTIVE = False
        APPLICATION_MAINTENANCE_SERVICE = previous_maintenance_service
        APPLICATION_AUTOMATION_SCHEDULER = previous_automation_scheduler
        APPLICATION_AUTOMATION_ORCHESTRATOR = previous_automation_orchestrator
        if APPLICATION_VALIDATION_MANAGER is validation_manager:
            APPLICATION_VALIDATION_MANAGER = None

    for component, failure in cleanup_failures:
        LOGGER.error("Incomplete %s: %s", component, failure)
    if primary_failure is not None:
        for component, failure in cleanup_failures:
            try:
                primary_failure.add_note(f"Cleanup also failed during {component}: {failure}")
            except (AttributeError, TypeError):
                pass
        raise primary_failure
    return 1 if cleanup_failures else 0


def main() -> int:
    return serve_application(Handler)


if __name__ == "__main__":
    raise SystemExit(main())
