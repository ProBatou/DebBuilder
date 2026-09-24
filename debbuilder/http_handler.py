"""HTTP routing for the DebBuilder application.

The handler is built from an injected application API so routes stay thin and
business operations remain independently testable.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from . import __version__
from .api_errors import ApiError
from .api_routes import ADMIN_API_ROUTES, RouteEffect, match_route, validate_routes
from .execution_projection import public_error
from .lifecycle import MutationGateClosed
from .support_bundle import SupportBundleError, build_support_bundle


LOGGER = logging.getLogger(__name__)
_SUPPORT_SELECTION_ID = re.compile(r"[A-Za-z0-9_.+-]{1,128}\Z")


def create_handler(api):
    """Return a request handler bound to the public application facade."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"debbuilder/{__version__}"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _authorized(self) -> bool:
            try:
                if api.is_request_authorized(self.headers):
                    return True
            except api.SessionSecretError:
                if self.command == "HEAD":
                    self.send_response(503)
                    self.end_headers()
                elif self.path.startswith("/api/"):
                    api.json_response(self, {"error": {
                        "code": "authentication_unavailable",
                        "message": "Authentication is unavailable",
                        "details": {},
                    }}, 503)
                else:
                    api.text_response(self, "Authentication is unavailable", 503)
                return False
            except (api.SettingsDocumentError, api.resource_limits.ResourceLimitError):
                if self.command == "POST" and urlparse(self.path).path == "/api/settings":
                    try:
                        if api.is_settings_repair_authorized(self.headers):
                            return True
                    except (api.SettingsDocumentError, api.resource_limits.ResourceLimitError, api.SessionSecretError):
                        pass
                if self.command == "HEAD":
                    self.send_response(503)
                    self.end_headers()
                elif self.path.startswith("/api/"):
                    api.json_response(self, {"error": {
                        "code": "settings_unavailable",
                        "message": "Application settings are unavailable",
                        "details": {},
                    }}, 503)
                else:
                    api.text_response(self, "Authentication is unavailable", 503)
                return False
            if api.effective_security()["auth_mode"] == "oidc" and self.command == "GET" and not self.path.startswith("/api/"):
                try:
                    url, _state = api.oidc_authorize_url(self.path or "/")
                except Exception as exc:
                    api.text_response(self, str(exc), 500)
                    return False
                self.send_response(302)
                self.send_header("Location", url)
                self.end_headers()
                return False
            api.json_response(self, {"error": "unauthorized"}, 401)
            return False

        def do_HEAD(self):
            if api.is_public_repo_path(urlparse(self.path).path):
                self.send_response(404)
                self.end_headers()
                return
            if self._authorized():
                self.send_response(200)
                self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if api.is_public_repo_path(parsed.path):
                api.text_response(self, "not found", 404)
                return
            if parsed.path in {"/auth/callback", "/auth/pocketid/callback"}:
                self._oidc_callback(parsed)
                return
            if parsed.path in {"/logout", "/auth/logout"}:
                self._logout()
                return
            if not self._authorized():
                return
            if self._get_api(parsed):
                return
            self._serve_static(parsed.path)

        def _oidc_callback(self, parsed):
            query = urllib.parse.parse_qs(parsed.query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            pending = api.SESSIONS.pop(f"state:{state}", None)
            if not code or not pending or pending.get("expires", 0) < time.time():
                api.text_response(self, "Invalid or expired OIDC callback", 400)
                return
            try:
                cookie = api.create_session(api.exchange_oidc_code(code, pending.get("nonce", ""), pending.get("code_verifier", "")))
            except api.SessionSecretError:
                api.text_response(self, "OIDC login failed: authentication is unavailable", 503)
                return
            except Exception as exc:
                api.text_response(self, f"OIDC login failed: {exc}", 500)
                return
            self.send_response(302)
            self.send_header("Set-Cookie", f"debbuilder_session={urllib.parse.quote(cookie)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400")
            self.send_header("Location", pending.get("return_to") or "/")
            self.end_headers()

        def _logout(self):
            cookies = api.parse_cookies(api._header_value(self.headers, "Cookie"))
            try:
                session_id = api.unsign_value(cookies.get("debbuilder_session", ""))
            except api.SessionSecretError:
                api.text_response(self, "Authentication is unavailable", 503)
                return
            if session_id:
                api.SESSIONS.pop(session_id, None)
            self.send_response(302)
            self.send_header("Set-Cookie", "debbuilder_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/")
            self.end_headers()

        def _get_api(self, parsed) -> bool:
            matched = match_route("GET", parsed.path)
            if matched is None:
                return False
            try:
                getattr(self, matched.route.handler)(matched.path_variables, parsed)
            except Exception:
                LOGGER.exception("Unexpected admin API GET failure")
                api.json_response(self, ApiError("internal_error", "An internal error occurred"), 500)
            return True

        def _get_status(self, _variables, _parsed):
            apt = api.repo_settings()
            security = api.effective_security()
            api.json_response(self, {"ok": True, "repo_default": apt["repository"], "suite_default": apt["distribution"], "component_default": apt["component"], "arch_default": apt["architecture"], "notification_type": api.app_settings()["notifications"].get("type", "none"), "auth_mode": security["auth_mode"], "workflow_dirs": {"examples": str(api.EXAMPLES), "user": str(api.USER_WORKFLOWS)}})

        def _get_system_diagnostics(self, _variables, _parsed):
            api.json_response(self, api.system_diagnostics_snapshot(self.server))

        def _get_support_bundle(self, _variables, parsed):
            try:
                pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, errors="strict")
            except UnicodeError:
                pairs = [("invalid", "")]
            if len(pairs) > 2 or len({key for key, _ in pairs}) != len(pairs) or any(
                key not in {"recipe_id", "run_id"} for key, _ in pairs
            ):
                api.json_response(self, ApiError("invalid_support_bundle_request", "The support bundle request is invalid"), 400)
                return
            selected = dict(pairs)
            recipe = run = None
            if "recipe_id" in selected:
                if not _SUPPORT_SELECTION_ID.fullmatch(selected["recipe_id"]):
                    api.json_response(self, ApiError("invalid_recipe_id", "The Recipe identifier is invalid"), 400)
                    return
                try:
                    recipe = api.get_recipe_inspection(selected["recipe_id"])
                except ValueError:
                    api.json_response(self, ApiError("invalid_recipe_id", "The Recipe identifier is invalid"), 400)
                    return
                except api.InspectionReadError:
                    api.json_response(self, ApiError("recipe_inspection_unavailable", "The Recipe cannot be inspected safely"), 409)
                    return
                if recipe is None:
                    api.json_response(self, ApiError("recipe_not_found", "The Recipe was not found"), 404)
                    return
            if "run_id" in selected:
                if not _SUPPORT_SELECTION_ID.fullmatch(selected["run_id"]):
                    api.json_response(self, ApiError("invalid_execution_id", "The execution identifier is invalid"), 400)
                    return
                try:
                    run = api.get_run_inspection(selected["run_id"], manager=getattr(self.server, "execution_manager", None))
                except ValueError:
                    api.json_response(self, ApiError("invalid_execution_id", "The execution identifier is invalid"), 400)
                    return
                except api.InspectionReadError:
                    api.json_response(self, ApiError("run_inspection_unavailable", "The Run cannot be inspected safely"), 409)
                    return
                if run is None:
                    api.json_response(self, ApiError("build_run_not_found", "Build Run was not found"), 404)
                    return
            try:
                payload = build_support_bundle(
                    diagnostics=api.system_diagnostics_snapshot(self.server),
                    recipe_inspection=recipe, run_inspection=run,
                )
            except SupportBundleError:
                LOGGER.exception("Support bundle assembly failed")
                api.json_response(self, ApiError("support_bundle_unavailable", "The support bundle cannot be generated"), 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="debbuilder-support.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _get_openapi(self, _variables, _parsed):
            from .openapi import openapi_document
            api.json_response(self, openapi_document())

        def _get_auth_status(self, _variables, _parsed):
            security = api.effective_security()
            user = self.headers.get(api.AUTH_HEADER, "")
            if not user and security["auth_mode"] == "oidc":
                user = api.oidc_session_user(self.headers)
            api.json_response(self, {"ok": True, "auth_mode": security["auth_mode"], "user": user})

        def _get_dashboard(self, _variables, _parsed):
            api.json_response(self, {"dashboard": api.dashboard_summary()})

        def _get_packages(self, _variables, _parsed):
            api.json_response(self, {"packages": api.list_packages()})

        def _get_package(self, variables, _parsed):
            name = variables["name"]
            try:
                package = api.get_package(name)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"package": package} if package else {"error": "not found"}, 200 if package else 404)

        def _get_recipes(self, _variables, _parsed):
            api.json_response(self, {"recipes": api.list_recipes()})

        def _get_recipe_inspection(self, variables, _parsed):
            try:
                inspection = api.get_recipe_inspection(variables["recipe_id"])
            except ValueError:
                api.json_response(self, ApiError("invalid_recipe_id", "The Recipe identifier is invalid"), 400)
                return
            except api.InspectionReadError:
                api.json_response(self, ApiError("recipe_inspection_unavailable", "The Recipe cannot be inspected safely"), 409)
                return
            api.json_response(self, {"inspection": inspection} if inspection is not None else
                              ApiError("recipe_not_found", "The Recipe was not found"),
                              200 if inspection is not None else 404)

        def _get_automation(self, variables, _parsed):
            try:
                api.json_response(self, {"automation": api.get_automation_status(variables["recipe_id"])})
            except ValueError as exc:
                api.json_response(self, {"error": {
                    "code": "invalid_recipe_id", "message": str(exc), "details": {},
                }}, 400)
            except api.automation_status.AutomationActionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)

        def _get_executions(self, _variables, _parsed):
            api.json_response(self, {"executions": api.list_executions()})

        def _get_execution(self, variables, _parsed):
            run_id = variables["run_id"]
            try:
                execution = api.get_execution(run_id)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"execution": execution} if execution else {"error": "not found"}, 200 if execution else 404)

        def _get_run_inspection(self, variables, _parsed):
            try:
                inspection = api.get_run_inspection(
                    variables["run_id"], manager=getattr(self.server, "execution_manager", None),
                )
            except ValueError:
                api.json_response(self, ApiError("invalid_execution_id", "The execution identifier is invalid"), 400)
                return
            except api.InspectionReadError:
                api.json_response(self, ApiError("run_inspection_unavailable", "The Run cannot be inspected safely"), 409)
                return
            api.json_response(self, {"inspection": inspection} if inspection is not None else
                              ApiError("build_run_not_found", "Build Run was not found"),
                              200 if inspection is not None else 404)

        def _get_execution_log(self, variables, parsed):
            run_id = variables["run_id"]
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int((query.get("after") or ["0"])[0] or 0)
                verbosity = (query.get("verbosity") or ["normal"])[0]
                log = api.get_execution_log(run_id, verbosity=verbosity, after=after)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"log": log} if log else {"error": "not found"}, 200 if log else 404)

        def _get_validation(self, variables, _parsed):
            try:
                validation = api.get_validation_attempt(
                    variables["run_id"], variables["attempt_id"],
                    manager=getattr(self.server, "validation_manager", None),
                )
            except ValueError as exc:
                api.json_response(self, {"error": {
                    "code": "invalid_validation_identity", "message": str(exc),
                }}, 400)
                return
            except api.validation_service.ValidationAdmissionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            api.json_response(self, {"validation": validation})

        def _get_settings(self, _variables, _parsed):
            api.json_response(self, {"settings": api.settings_view()})

        def _get_storage(self, _variables, _parsed):
            api.json_response(self, {"storage": api.storage_snapshot(
                getattr(self.server, "storage_inventory", None),
            )})

        def _get_workflows(self, _variables, _parsed):
            api.json_response(self, api.workflow_listing())

        def _get_workflow(self, variables, _parsed):
            workflow_id = variables["workflow_id"]
            try:
                workflow_file = api.workflow_path(workflow_id)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            if not workflow_file:
                api.json_response(self, {"error": "not found"}, 404)
                return
            api.json_response(self, api.read_workflow_file(workflow_file))

        def _serve_static(self, path: str):
            path = "/index.html" if path == "/" else path
            static_root = api.STATIC.resolve()
            static_file = (static_root / path.lstrip("/")).resolve()
            try:
                static_file.relative_to(static_root)
            except ValueError:
                api.text_response(self, "not found", 404)
                return
            if not static_file.is_file():
                api.text_response(self, "not found", 404)
                return
            content_type = "text/html; charset=utf-8" if static_file.suffix == ".html" else "application/javascript; charset=utf-8" if static_file.suffix == ".js" else "text/css; charset=utf-8"
            api.text_response(self, static_file.read_text(), 200, content_type, "no-cache, must-revalidate")

        def do_POST(self):
            if not self._authorized():
                return
            try:
                parsed = urlparse(self.path)
                matched = match_route("POST", parsed.path)
                gate = getattr(self.server, "mutation_gate", None)
                lease = gate.lease() if (
                    gate is not None
                    and matched is not None
                    and matched.route.effect is RouteEffect.DURABLE_MUTATION
                ) else _NullLease()
                with lease:
                    data = api.read_body(self)
                    self._post(data)
            except MutationGateClosed as exc:
                api.json_response(self, {"error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": {},
                }}, 503)
            except json.JSONDecodeError as exc:
                api.json_response(self, ApiError(
                    "invalid_json",
                    "The request body is not valid JSON",
                    {"line": exc.lineno, "column": exc.colno, "path": "$"},
                ), 400)
            except api.SettingsDocumentError as exc:
                api.json_response(self, {"error": exc.as_dict()}, 422)
            except api.resource_limits.ResourceLimitError as exc:
                api.json_response(self, {"error": exc.as_dict()}, 422)
            except ValueError:
                api.json_response(self, ApiError("invalid_request", "The request is invalid"), 400)
            except Exception:
                LOGGER.exception("Unexpected admin API POST failure")
                api.json_response(self, ApiError("internal_error", "An internal error occurred"), 400)

        def _post(self, data: dict):
            parsed = urlparse(self.path)
            matched = match_route("POST", parsed.path)
            if matched is None:
                api.json_response(self, {"error": "not found"}, 404)
                return
            getattr(self, matched.route.handler)(data, matched.path_variables, parsed)

        def _post_observation_refresh(self, data, variables, _parsed):
            observation_recipe_id = variables["recipe_id"]
            if not isinstance(data, dict) or data:
                api.json_response(self, {"error": {
                    "code": "invalid_observation_refresh_request",
                    "message": "Upstream refresh does not accept request fields",
                    "details": {},
                }}, 400)
                return
            try:
                observation = api.refresh_upstream_observation(observation_recipe_id)
            except FileNotFoundError:
                api.json_response(self, {"error": {
                    "code": "recipe_not_found", "message": "Recipe was not found", "details": {},
                }}, 404)
                return
            except api.upstream_detection.UpstreamDetectionError as exc:
                api.json_response(self, {"error": {
                    "code": exc.code, "message": str(exc),
                    "details": {"classification": exc.classification},
                }}, 502)
                return
            except api.upstream_observation.UpstreamObservationError as exc:
                status = 409 if exc.code == "recipe_changed_during_detection" else 503
                api.json_response(self, {"error": {
                    "code": exc.code, "message": str(exc), "details": {},
                }}, status)
                return
            api.json_response(self, {"ok": True, "observation": observation}, 200)

        def _post_automation_check(self, data, variables, _parsed):
            self._post_automation_action(data, variables["recipe_id"], "check")

        def _post_automation_retry(self, data, variables, _parsed):
            self._post_automation_action(data, variables["recipe_id"], "retry")

        def _post_automation_action(self, data, recipe_id, action):
            try:
                if action == "check":
                    if not isinstance(data, dict) or data:
                        api.json_response(self, {"error": {
                            "code": "invalid_automation_check_request",
                            "message": "Check now does not accept request fields",
                            "details": {},
                        }}, 400)
                        return
                    result = api.check_automation_now(recipe_id)
                else:
                    result = api.retry_automation(recipe_id, data)
            except ValueError as exc:
                api.json_response(self, {"error": {
                    "code": "invalid_recipe_id", "message": str(exc), "details": {},
                }}, 400)
                return
            except api.automation_status.AutomationActionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            api.json_response(self, {"ok": True, "automation": result}, 202)

        def _post_validation_cancel(self, data, variables, _parsed):
            run_id = variables["run_id"]
            attempt_id = variables["attempt_id"]
            if not isinstance(data, dict) or data:
                api.json_response(self, {"error": {
                    "code": "invalid_validation_cancellation_request",
                    "message": "Validation cancellation does not accept request fields",
                }}, 400)
                return
            try:
                result = api.cancel_validation_attempt(
                    getattr(self.server, "validation_manager", None), run_id, attempt_id,
                )
            except ValueError as exc:
                api.json_response(self, {"error": {
                    "code": "invalid_validation_identity", "message": str(exc), "details": {},
                }}, 400)
                return
            except api.validation_service.ValidationAdmissionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            except api.validation_service.ValidationCancellationError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            status = 202 if result["validation"]["status"] == "cancelling" else 200
            api.json_response(self, {"ok": True, **result}, status)

        def _post_execution_cancel(self, data, variables, _parsed):
            run_id = variables["run_id"]
            if not isinstance(data, dict) or data:
                api.json_response(self, {"error": {
                    "code": "invalid_cancellation_request",
                    "message": "Execution cancellation does not accept request fields",
                }}, 400)
                return
            try:
                service = getattr(self.server, "maintenance_service", None)
                result = api.cancel_execution(
                    getattr(self.server, "execution_manager", None),
                    run_id,
                    maintenance_request=service.request if service is not None else None,
                )
            except ValueError as exc:
                api.json_response(self, {"error": {
                    "code": "invalid_execution_id", "message": str(exc), "details": {},
                }}, 400)
                return
            except api.ExecutionCancellationError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            api.json_response(self, {"ok": True, "cancellation": result}, 200 if result["status"] == "cancelled" else 202)

        def _post_recipe_validate(self, data, _variables, _parsed):
            recipe = data.get("recipe") if isinstance(data, dict) and "recipe" in data else data
            try:
                api.json_response(self, api.recipe_json_validation(recipe))
            except api.RecipeDocumentError as exc:
                api.json_response(self, {"ok": False, "error": {"code": exc.code, "message": str(exc), "path": exc.path}}, 422)

        def _post_recipe_import(self, data, _variables, _parsed):
            recipe = data.get("recipe") if isinstance(data, dict) else data
            replace = data.get("replace", False) if isinstance(data, dict) else False
            if not isinstance(replace, bool):
                api.json_response(self, {"ok": False, "error": {"code": "invalid_replace", "message": "replace must be a boolean", "path": "$.replace"}}, 422)
                return
            try:
                api.json_response(self, api.import_recipe_json(recipe, replace=replace))
            except api.builtin_recipe.BuiltinRecipeError as exc:
                api.json_response(self, {"ok": False, "error": exc.as_dict()}, 409)
            except api.RecipeDocumentError as exc:
                api.json_response(self, {"ok": False, "error": {"code": exc.code, "message": str(exc), "path": exc.path}}, 422)
            except FileExistsError as exc:
                api.json_response(self, {"ok": False, "error": {"code": "recipe_exists", "message": str(exc), "path": "$.name"}}, 409)
            except PermissionError as exc:
                api.json_response(self, {"ok": False, "error": {"code": "readonly_recipe", "message": str(exc), "path": "$.name"}}, 403)

        def _post_run(self, data, _variables, _parsed):
            workflow = data.get("workflow", data)
            if workflow.get("active") is False:
                api.json_response(self, {"error": "recipe is disabled"}, 409)
                return
            dry_run = bool(data.get("dry_run", True))
            try:
                result = api.enqueue_recipe_run(getattr(self.server, "execution_manager", None), workflow, dry_run=dry_run)
            except api.RunAdmissionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            api.json_response(self, result, 202)

        def _post_archive_inspect(self, data, _variables, _parsed):
            workflow = data.get("workflow", data)
            try:
                api.json_response(self, {"inspection": api.inspect_upstream_archive(workflow)})
            except api.RecipeDocumentError as exc:
                api.json_response(self, {"error": {
                    "code": exc.code, "message": str(exc), "path": exc.path,
                }}, 422)
            except api.upstream_archive.UpstreamArchiveError as exc:
                api.json_response(self, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, 422)

        def _post_validation_start(self, data, variables, _parsed):
            try:
                result = api.admit_validation_attempt(
                    getattr(self.server, "validation_manager", None), variables["run_id"], data,
                )
            except api.validation_service.ValidationAdmissionError as exc:
                api.json_response(self, {"error": public_error(exc.as_dict())}, exc.status)
                return
            api.json_response(self, {"validation": result}, 202)

        def _post_publication_publish(self, data, variables, _parsed):
            try:
                result = api.publish_build_artifact(variables["run_id"], data)
            except api.artifact_publication.PublicationError as exc:
                api.json_response(self, {"error": public_error({
                    "code": exc.code, "stage": "publication", "message": str(exc),
                })}, 400)
                return
            self._publication_response(result)

        def _post_publication_reconcile(self, data, variables, _parsed):
            self._publication_response(api.reconcile_build_publication(variables["run_id"], data))

        def _publication_response(self, result):
            failure_code = str((result.get("error") or {}).get("code") or "")
            failure_status = 409 if failure_code in {"repository_mutation_busy", "publication_identity_conflict"} else 422
            api.json_response(self, {"publication": result}, 200 if result["status"] == "success" else failure_status)

        def _post_notification_test(self, _data, _variables, _parsed):
            result = api.test_notification()
            api.json_response(self, {"ok": bool(result.get("ok")), "notification": result}, 200 if result.get("ok") else 502)

        def _post_settings(self, data, _variables, _parsed):
            api.json_response(self, {"ok": True, "settings": api.update_settings(data)})

        def _post_execution_logs_delete(self, data, _variables, _parsed):
            api.json_response(self, api.delete_execution_logs(
                data.get("ids") or [],
                all_runs=bool(data.get("all")),
                dry_run=bool(data.get("dry_run")),
                authorization=getattr(self.server, "cleanup_authorization", None),
            ))

        def _post_package_create(self, data, _variables, _parsed):
            api.json_response(self, {"ok": True, "package": api.create_or_update_package(data)})

        def _post_package_update(self, data, variables, _parsed):
            try:
                package = api.create_or_update_package(data, name=variables["name"])
            except KeyError:
                api.json_response(self, {"error": "not found"}, 404)
                return
            api.json_response(self, {"ok": True, "package": package})

        def _post_workflow_save(self, data, variables, _parsed):
            self._save_workflow(data, variables["workflow_id"])

        def _save_workflow(self, data: dict, workflow_id: str):
            workflow_id = api.sanitize_id(workflow_id)
            workflow = data.get("workflow", data)
            workflow["name"] = workflow.get("name") or workflow_id
            previous_id = str(data.get("previous_id") or "")
            try:
                result = api.save_workflow_recipe(workflow_id, workflow, previous_id=previous_id)
            except api.builtin_recipe.BuiltinRecipeError as exc:
                api.json_response(self, {"error": exc.as_dict()}, 409)
                return
            except api.RecipeDocumentError as exc:
                api.json_response(self, {"error": {
                    "code": exc.code, "message": str(exc), "path": exc.path,
                }}, 422)
                return
            except PermissionError as exc:
                api.json_response(self, {"error": str(exc)}, 403)
                return
            api.json_response(self, result)

        def do_DELETE(self):
            if not self._authorized():
                return
            parsed = urlparse(self.path)
            try:
                matched = match_route("DELETE", parsed.path)
                gate = getattr(self.server, "mutation_gate", None)
                lease = gate.lease() if (
                    gate is not None
                    and matched is not None
                    and matched.route.effect is RouteEffect.DURABLE_MUTATION
                ) else _NullLease()
                with lease:
                    if matched is not None:
                        getattr(self, matched.route.handler)(matched.path_variables, parsed)
                        return
                    api.json_response(self, {"error": "not found"}, 404)
            except MutationGateClosed as exc:
                api.json_response(self, {"error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": {},
                }}, 503)
            except ValueError:
                api.json_response(self, ApiError("invalid_request", "The request is invalid"), 400)
            except Exception:
                LOGGER.exception("Unexpected admin API DELETE failure")
                api.json_response(self, ApiError("internal_error", "An internal error occurred"), 400)

        def _delete_workflow(self, variables, _parsed):
            workflow_id = variables["workflow_id"]
            try:
                api.delete_workflow(workflow_id)
            except api.builtin_recipe.BuiltinRecipeError as exc:
                api.json_response(self, {"error": exc.as_dict()}, 403)
                return
            except FileNotFoundError:
                api.json_response(self, {"error": "recipe not found"}, 404)
                return
            except PermissionError as exc:
                api.json_response(self, {"error": str(exc)}, 403)
                return
            api.json_response(self, {"ok": True, "id": workflow_id, "deleted_from_repository": False})

        def _delete_package(self, variables, parsed):
            name = variables["name"]
            if parsed.query:
                api.json_response(self, {"error": "package deletion does not accept repository operations"}, 400)
                return
            api.delete_package(name)
            api.json_response(self, {"ok": True, "id": name, "deleted_from_repo": False})

        def _delete_execution_log(self, variables, _parsed):
            run_id = variables["run_id"]
            try:
                api.json_response(self, {"ok": True, "deletion": api.delete_execution_log(
                    run_id,
                    authorization=getattr(self.server, "cleanup_authorization", None),
                )})
            except FileNotFoundError:
                api.json_response(self, {"error": "execution not found"}, 404)
            except api.workspace_cleanup.WorkspaceBusyError as exc:
                api.json_response(self, {"error": str(exc), "code": "execution_active"}, 409)

    validate_routes(ADMIN_API_ROUTES, Handler)
    return Handler


class _NullLease:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
