"""HTTP routing for the DebBuilder application.

The handler is built from an injected application API so routes stay thin and
business operations remain independently testable.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from . import __version__
from .lifecycle import MutationGateClosed, is_durable_mutation_route


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
            path = parsed.path
            if path == "/api/status":
                apt = api.repo_settings()
                security = api.effective_security()
                api.json_response(self, {"ok": True, "repo_default": apt["repository"], "suite_default": apt["distribution"], "component_default": apt["component"], "arch_default": apt["architecture"], "notification_type": api.app_settings()["notifications"].get("type", "none"), "auth_mode": security["auth_mode"], "workflow_dirs": {"examples": str(api.EXAMPLES), "user": str(api.USER_WORKFLOWS)}})
            elif path == "/api/auth/status":
                security = api.effective_security()
                user = self.headers.get(api.AUTH_HEADER, "")
                if not user and security["auth_mode"] == "oidc":
                    user = api.oidc_session_user(self.headers)
                api.json_response(self, {"ok": True, "auth_mode": security["auth_mode"], "user": user})
            elif path == "/api/dashboard":
                api.json_response(self, {"dashboard": api.dashboard_summary()})
            elif path == "/api/packages":
                api.json_response(self, {"packages": api.list_packages()})
            elif path.startswith("/api/packages/"):
                self._get_package(path)
            elif path == "/api/recipes":
                api.json_response(self, {"recipes": api.list_recipes()})
            elif path == "/api/executions":
                api.json_response(self, {"executions": api.list_executions()})
            elif path.startswith("/api/executions/"):
                if path.endswith("/logs"):
                    self._get_execution_log(parsed)
                    return True
                self._get_execution(path)
            elif path == "/api/settings":
                api.json_response(self, {"settings": api.settings_view()})
            elif path == "/api/storage":
                api.json_response(self, {"storage": api.storage_snapshot(
                    getattr(self.server, "storage_inventory", None),
                )})
            elif path == "/api/workflows":
                api.json_response(self, api.workflow_listing())
            elif path.startswith("/api/workflows/"):
                self._get_workflow(path)
            else:
                return False
            return True

        def _get_package(self, path: str):
            name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            try:
                package = api.get_package(name)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"package": package} if package else {"error": "not found"}, 200 if package else 404)

        def _get_execution(self, path: str):
            run_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            try:
                execution = api.get_execution(run_id)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"execution": execution} if execution else {"error": "not found"}, 200 if execution else 404)

        def _get_execution_log(self, parsed):
            run_id = urllib.parse.unquote(parsed.path[len("/api/executions/"):-len("/logs")].strip("/"))
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int((query.get("after") or ["0"])[0] or 0)
                verbosity = (query.get("verbosity") or ["normal"])[0]
                log = api.get_execution_log(run_id, verbosity=verbosity, after=after)
            except ValueError as exc:
                api.json_response(self, {"error": str(exc)}, 400)
                return
            api.json_response(self, {"log": log} if log else {"error": "not found"}, 200 if log else 404)

        def _get_workflow(self, path: str):
            workflow_id = path.rsplit("/", 1)[-1]
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
                gate = getattr(self.server, "mutation_gate", None)
                lease = gate.lease() if gate is not None and is_durable_mutation_route(
                    "POST", urlparse(self.path).path,
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
                if self.path in {"/api/recipes/validate", "/api/recipes/import"}:
                    api.json_response(self, {"ok": False, "error": {"code": "invalid_json", "message": f"JSON syntax error at line {exc.lineno}, column {exc.colno}", "path": "$"}}, 400)
                else:
                    api.json_response(self, {"error": str(exc)}, 400)
            except Exception as exc:
                if self.path in {"/api/recipes/validate", "/api/recipes/import"}:
                    api.json_response(self, {"ok": False, "error": {"code": "invalid_request", "message": str(exc), "path": "$"}}, 400)
                else:
                    api.json_response(self, {"error": str(exc)}, 400)

        def _post(self, data: dict):
            parsed_path = urlparse(self.path).path
            if parsed_path.startswith("/api/executions/") and parsed_path.endswith("/cancel"):
                run_id = urllib.parse.unquote(parsed_path[len("/api/executions/"):-len("/cancel")].strip("/"))
                if not isinstance(data, dict) or data:
                    api.json_response(self, {"error": {
                        "code": "invalid_cancellation_request",
                        "message": "Execution cancellation does not accept request fields",
                        "details": {"run_id": run_id},
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
                    api.json_response(self, {"error": exc.as_dict()}, exc.status)
                    return
                api.json_response(self, {"ok": True, "cancellation": result}, 200 if result["status"] == "cancelled" else 202)
                return
            if self.path == "/api/recipes/validate":
                recipe = data.get("recipe") if isinstance(data, dict) and "recipe" in data else data
                try:
                    api.json_response(self, api.recipe_json_validation(recipe))
                except api.RecipeDocumentError as exc:
                    api.json_response(self, {"ok": False, "error": {"code": exc.code, "message": str(exc), "path": exc.path}}, 422)
                return
            if self.path == "/api/recipes/import":
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
                return
            if self.path == "/api/run":
                workflow = data.get("workflow", data)
                if workflow.get("active") is False:
                    api.json_response(self, {"error": "recipe is disabled"}, 409)
                    return
                dry_run = bool(data.get("dry_run", True))
                try:
                    result = api.enqueue_recipe_run(getattr(self.server, "execution_manager", None), workflow, dry_run=dry_run)
                except api.RunAdmissionError as exc:
                    api.json_response(self, {"error": exc.as_dict()}, exc.status)
                    return
                api.json_response(self, result, 202)
                return
            if self.path == "/api/upstream-archive/inspect":
                workflow = data.get("workflow", data)
                try:
                    api.json_response(self, {"inspection": api.inspect_upstream_archive(workflow)})
                except api.upstream_archive.UpstreamArchiveError as exc:
                    api.json_response(self, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, 422)
                return
            if self.path.startswith("/api/executions/") and self.path.endswith("/validate"):
                run_id = urllib.parse.unquote(self.path[len("/api/executions/"):-len("/validate")].strip("/"))
                try:
                    result = api.validate_build_artifact(run_id, data)
                except api.artifact_validation.ValidationError as exc:
                    api.json_response(self, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, 400)
                    return
                api.json_response(self, {"validation": result}, 200 if result["status"] == "success" else 422)
                return
            if self.path.startswith("/api/executions/") and self.path.endswith("/publish"):
                run_id = urllib.parse.unquote(self.path[len("/api/executions/"):-len("/publish")].strip("/"))
                try:
                    result = api.publish_build_artifact(run_id, data)
                except api.artifact_publication.PublicationError as exc:
                    api.json_response(self, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, 400)
                    return
                failure_code = str((result.get("error") or {}).get("code") or "")
                failure_status = 409 if failure_code in {"repository_mutation_busy", "publication_identity_conflict"} else 422
                api.json_response(self, {"publication": result}, 200 if result["status"] == "success" else failure_status)
                return
            if self.path.startswith("/api/executions/") and self.path.endswith("/reconcile-publication"):
                run_id = urllib.parse.unquote(self.path[len("/api/executions/"):-len("/reconcile-publication")].strip("/"))
                result = api.reconcile_build_publication(run_id, data)
                failure_code = str((result.get("error") or {}).get("code") or "")
                failure_status = 409 if failure_code in {"repository_mutation_busy", "publication_identity_conflict"} else 422
                api.json_response(self, {"publication": result}, 200 if result["status"] == "success" else failure_status)
                return
            if self.path == "/api/notifications/test":
                result = api.test_notification()
                api.json_response(self, {"ok": bool(result.get("ok")), "notification": result}, 200 if result.get("ok") else 502)
                return
            if self.path == "/api/settings":
                api.json_response(self, {"ok": True, "settings": api.update_settings(data)})
                return
            if self.path == "/api/executions/delete-logs":
                api.json_response(self, api.delete_execution_logs(
                    data.get("ids") or [],
                    all_runs=bool(data.get("all")),
                    dry_run=bool(data.get("dry_run")),
                    authorization=getattr(self.server, "cleanup_authorization", None),
                ))
                return
            if self.path == "/api/packages":
                api.json_response(self, {"ok": True, "package": api.create_or_update_package(data)})
                return
            if self.path.startswith("/api/packages/"):
                package_path = self.path[len("/api/packages/"):].strip("/")
                if not package_path or "/" in package_path:
                    api.json_response(self, {"error": "not found"}, 404)
                    return
                name = urllib.parse.unquote(package_path)
                try:
                    package = api.create_or_update_package(data, name=name)
                except KeyError:
                    api.json_response(self, {"error": "not found"}, 404)
                    return
                api.json_response(self, {"ok": True, "package": package})
                return
            if self.path.startswith("/api/workflows/"):
                self._save_workflow(data)
                return
            api.json_response(self, {"error": "not found"}, 404)

        def _save_workflow(self, data: dict):
            workflow_id = api.sanitize_id(self.path.rsplit("/", 1)[-1])
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
                gate = getattr(self.server, "mutation_gate", None)
                lease = gate.lease() if gate is not None and is_durable_mutation_route(
                    "DELETE", parsed.path,
                ) else _NullLease()
                with lease:
                    if parsed.path.startswith("/api/workflows/"):
                        self._delete_workflow(parsed.path)
                        return
                    if parsed.path.startswith("/api/executions/") and parsed.path.endswith("/logs"):
                        self._delete_execution_log(parsed.path)
                        return
                    if parsed.path.startswith("/api/packages/"):
                        self._delete_package(parsed)
                        return
                    api.json_response(self, {"error": "not found"}, 404)
            except MutationGateClosed as exc:
                api.json_response(self, {"error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": {},
                }}, 503)
            except Exception as exc:
                api.json_response(self, {"error": str(exc)}, 400)

        def _delete_workflow(self, path: str):
            workflow_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
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

        def _delete_package(self, parsed):
            name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            if parsed.query:
                api.json_response(self, {"error": "package deletion does not accept repository operations"}, 400)
                return
            api.delete_package(name)
            api.json_response(self, {"ok": True, "id": name, "deleted_from_repo": False})

        def _delete_execution_log(self, path: str):
            run_id = urllib.parse.unquote(path[len("/api/executions/"):-len("/logs")].strip("/"))
            try:
                api.json_response(self, {"ok": True, "deletion": api.delete_execution_log(
                    run_id,
                    authorization=getattr(self.server, "cleanup_authorization", None),
                )})
            except FileNotFoundError:
                api.json_response(self, {"error": "execution not found"}, 404)
            except api.workspace_cleanup.WorkspaceBusyError as exc:
                api.json_response(self, {"error": str(exc), "code": "execution_active"}, 409)

    return Handler


class _NullLease:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
