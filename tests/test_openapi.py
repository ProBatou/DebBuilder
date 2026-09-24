"""Parity checks for the offline and served OpenAPI contract."""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.error
import urllib.request
from unittest import TestCase, mock

from debbuilder import __version__, app
from debbuilder.api_errors import STABLE_ERROR_CODES, canonical_error_payload
from debbuilder.api_routes import ADMIN_API_ROUTES, RouteEffect
from debbuilder.build_models import RUN_STATUSES
from debbuilder.openapi import OPERATION_DOCS, SCHEMAS, openapi_document, openapi_json
from debbuilder.recipe_schema import AUTOMATION_POLICIES, SCHEMA_VERSION, recipe_document_for_storage
from tests.admin_api_case import AdminApiCase


ARTIFACT = Path(__file__).resolve().parents[1] / "openapi.json"


def operations(document):
    return {
        (method.upper(), path): operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }


class OpenApiContractTests(TestCase):
    def test_registry_is_the_exact_source_of_paths_methods_and_ids(self):
        document = openapi_document()
        actual = operations(document)
        expected = {(route.method, route.path_template): route for route in ADMIN_API_ROUTES}
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(actual), 40)
        self.assertEqual(set(OPERATION_DOCS), {route.operation_id for route in ADMIN_API_ROUTES})
        self.assertEqual(
            {operation["operationId"] for operation in actual.values()},
            {route.operation_id for route in ADMIN_API_ROUTES},
        )
        for key, route in expected.items():
            with self.subTest(operation=route.operation_id):
                operation = actual[key]
                self.assertEqual(operation["operationId"], route.operation_id)
                self.assertEqual(operation["summary"], route.summary)
                self.assertEqual(operation["tags"], list(route.tags))
                self.assertEqual(operation["x-debbuilder-effect"], route.effect.value)
                self.assertEqual(
                    {p["name"] for p in operation.get("parameters", []) if p["in"] == "path"},
                    set(re.findall(r"\{([^}]+)\}", route.path_template)),
                )
                self.assertTrue(operation["responses"])
                self.assertEqual(operation["security"], [{}, {"ProxyIdentity": []}, {"OidcSession": []}])
                if route.method == "GET":
                    self.assertIs(route.effect, RouteEffect.READ_ONLY)
                    self.assertNotIn("requestBody", operation)

    def test_error_schema_matches_runtime_and_all_documented_codes_are_canonical(self):
        document = openapi_document()
        schema = document["components"]["schemas"]["ApiError"]
        self.assertEqual(schema["required"], ["ok", "error"])
        self.assertEqual(set(schema["properties"]), {"ok", "error"})
        self.assertIs(schema["properties"]["ok"]["const"], False)
        inner = schema["properties"]["error"]
        self.assertEqual(inner["required"], ["code", "message", "details"])
        self.assertEqual(set(inner["properties"]), {"code", "message", "details"})
        self.assertEqual(inner["properties"]["details"]["type"], "object")
        self.assertEqual(inner["properties"]["details"]["additionalProperties"], False)
        self.assertEqual(canonical_error_payload({"error": "unauthorized"}, 401, "/api/settings"), {
            "ok": False,
            "error": {"code": "authentication_required", "message": "Authentication is required", "details": {}},
        })
        documented = set()
        for operation in operations(document).values():
            for status, response in operation["responses"].items():
                if int(status) < 400:
                    self.assertNotIn("x-debbuilder-error-codes", response)
                    continue
                self.assertEqual(response["content"]["application/json"]["schema"], {"$ref": "#/components/schemas/ApiError"})
                codes = response["x-debbuilder-error-codes"]
                self.assertTrue(codes)
                documented.update(codes)
        self.assertTrue(documented <= STABLE_ERROR_CODES)
        self.assertGreaterEqual(len(documented), 25)
        self.assertEqual(
            operations(document)[("POST", "/api/executions/{run_id}/cancel")]["responses"]["409"]["x-debbuilder-error-codes"],
            ["execution_not_cancellable"],
        )
        self.assertIn("ambiguous_release_asset", operations(document)[
            ("POST", "/api/upstream-archive/inspect")]["responses"]["422"]["x-debbuilder-error-codes"])
        self.assertIn("unsupported_version_source", operations(document)[
            ("POST", "/api/recipes/validate")]["responses"]["422"]["x-debbuilder-error-codes"])
        self.assertIn("github_unavailable", operations(document)[
            ("POST", "/api/recipes/{recipe_id}/observation/refresh")]["responses"]["502"]["x-debbuilder-error-codes"])

    def test_schema_references_and_key_request_contracts_are_valid(self):
        document = openapi_document()
        schemas = document["components"]["schemas"]

        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertIn(value["$ref"].removeprefix("#/components/schemas/"), schemas)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(document)
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertEqual(document["info"]["version"], __version__)
        self.assertEqual(schemas["Recipe"]["properties"]["schema_version"], {"const": SCHEMA_VERSION})
        self.assertEqual(
            set(schemas["AutomationPolicy"]["properties"]["policy"]["enum"]), AUTOMATION_POLICIES,
        )
        self.assertEqual(schemas["AutomationRetryInput"]["required"], ["generation", "revision"])
        self.assertEqual(set(schemas["ValidationStartInput"]["properties"]), {"profile", "previous_artifact"})
        self.assertEqual(schemas["RecipeImportInput"]["required"], ["recipe"])
        self.assertEqual(schemas["PublicationInput"]["required"], ["confirm"])
        self.assertEqual(schemas["PackageCreateInput"]["allOf"][1]["required"], ["name"])
        self.assertEqual(schemas["RunAdmissionResponse"]["required"], ["run_id", "status"])
        self.assertEqual(set(schemas["Execution"]["properties"]["status"]["enum"]), RUN_STATUSES)
        recipe_document_for_storage({
            "schema_version": SCHEMA_VERSION, "name": "api-docs-example",
            "package": {"name": "api-docs-example"},
            "source": {"repository": "example/api-docs-example", "tracking": "latest_release"},
        })

    def test_generation_is_byte_stable_and_contains_no_runtime_secrets(self):
        first = openapi_json().encode("utf-8")
        self.assertEqual(first, openapi_json().encode("utf-8"))
        self.assertEqual(first, ARTIFACT.read_bytes())
        self.assertEqual(json.loads(first), openapi_document())
        for sentinel in (
            "sentinel-password", "sentinel-authorization", "sentinel-cookie",
            "sentinel-oidc-secret", "sentinel-github-token", "RECIPE_ENV_SECRET",
            "/private/runtime/settings.json", "PRIVATE-KEY-MATERIAL", "unsafe stderr",
            "/opt/debbuilder-worktrees", "/root/.codex",
        ):
            self.assertNotIn(sentinel.encode(), first)


class OpenApiHttpTests(AdminApiCase):
    def test_document_is_authenticated_json_and_does_not_mutate_state(self):
        with mock.patch.object(app, "request_maintenance", side_effect=AssertionError("mutation")):
            request = urllib.request.Request(self.base_url + "/api/openapi.json")
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(json.loads(response.read()), openapi_document())

        app.AUTH_MODE = "header"
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(self.base_url + "/api/openapi.json", timeout=5)
        self.assertEqual(captured.exception.code, 401)
        self.assert_api_error(json.loads(captured.exception.read()), code="authentication_required")

        request = urllib.request.Request(self.base_url + "/api/openapi.json", headers={app.AUTH_HEADER: "operator"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["info"]["version"], __version__)

    def test_selected_success_wrappers_match_live_http_handlers(self):
        for path, status, key, schema_name in (
            ("/api/status", 200, "ok", "StatusResponse"),
            ("/api/packages", 200, "packages", "PackagesResponse"),
            ("/api/settings", 200, "settings", "SettingsResponse"),
            ("/api/storage", 200, "storage", "StorageResponse"),
            ("/api/workflows", 200, "workflows", "WorkflowListResponse"),
            ("/api/executions/20260822-031400", 200, "execution", "ExecutionResponse"),
        ):
            with self.subTest(path=path):
                actual_status, payload = self.request("GET", path)
                self.assertEqual(actual_status, status)
                self.assertIn(key, payload)
                schema = SCHEMAS[schema_name]
                self.assertTrue(set(schema["required"]) <= set(payload))

        with mock.patch.object(app, "enqueue_recipe_run", return_value={"run_id": "queued-run", "status": "queued"}):
            status, payload = self.request("POST", "/api/run", {"workflow": {"name": "demo"}})
        self.assertEqual(status, 202)
        self.assertEqual(set(payload), set(SCHEMAS["RunAdmissionResponse"]["required"]))

        with mock.patch.object(app, "admit_validation_attempt", return_value={"attempt_id": "attempt", "status": "queued"}):
            status, payload = self.request("POST", "/api/executions/20260822-031400/validate", {"profile": "bookworm"})
        self.assertEqual(status, 202)
        self.assertEqual(set(payload), {"validation"})

        with mock.patch.object(app, "check_automation_now", return_value={"accepted": True, "status": {
            "recipe_id": "demo", "automation": {"enabled": True, "policy": "detect"}, "state": "checking",
        }}):
            status, payload = self.request("POST", "/api/recipes/demo/automation/check", {})
        self.assertEqual(status, 202)
        self.assertEqual(set(payload), {"ok", "automation"})
        self.assertEqual(payload["automation"]["status"]["recipe_id"], "demo")

    def test_documented_error_codes_match_representative_runtime_failures(self):
        document = openapi_document()
        for method, path, template, body in (
            ("GET", "/api/packages/missing", "/api/packages/{name}", None),
            ("GET", "/api/executions/20260822-031400/logs?after=bad", "/api/executions/{run_id}/logs", None),
            ("POST", "/api/recipes/demo/automation/check", "/api/recipes/{recipe_id}/automation/check", {"unexpected": True}),
            ("POST", "/api/executions/20260822-031400/cancel", "/api/executions/{run_id}/cancel", {"unexpected": True}),
            ("POST", "/api/executions/20260822-031400/validate", "/api/executions/{run_id}/validate", {"unexpected": True}),
            ("POST", "/api/settings", "/api/settings", {"unknown": True}),
            ("POST", "/api/run", "/api/run", {"workflow": {"active": False}}),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(urllib.error.HTTPError) as captured:
                self.request(method, path, body)
            status = str(captured.exception.code)
            error = self.assert_api_error(json.loads(captured.exception.read()))
            codes = document["paths"][template][method.lower()]["responses"][status]["x-debbuilder-error-codes"]
            self.assertIn(error["code"], codes)


if __name__ == "__main__":
    import unittest
    unittest.main()
