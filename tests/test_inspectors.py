"""Versioned operator inspections: useful facts without raw private state."""
from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest import TestCase, mock

from debbuilder import app, inspectors
from debbuilder.api_routes import RouteEffect, match_route
from debbuilder.build_models import new_run
from debbuilder.openapi import openapi_document
from debbuilder.recipe_schema import recipe_document_for_storage
from tests.admin_api_case import AdminApiCase


SENTINELS = (
    "ghp_SUPER_SECRET", "oidc-client-secret", "cookie-secret", "password-123",
    "SENSITIVE_ENV=value", "command --token=secret", "maintainer-script-secret",
    "source-change-secret", "stderr-secret", "Traceback private", "/var/lib/debbuilder/private/data",
    "/tmp/workspace-secret/data", "https://user:password@host/private",
)


def recipe(**updates):
    value = {"schema_version": 5, "name": "inspection-demo",
             "source": {"repository": "owner/project"}}
    value.update(updates)
    return recipe_document_for_storage(value)


def run(status="queued"):
    value = new_run("inspection-run", "inspection-demo", "build", "/tmp/workspace-secret/data", "a" * 64)
    value["status"] = status
    return value


class InspectorProjectionTests(TestCase):
    def test_openapi_inspection_objects_are_closed_and_match_service_keys(self):
        schemas = openapi_document()["components"]["schemas"]
        samples = (("RecipeInspection", inspectors.inspect_recipe(recipe())),
                   ("RunInspection", inspectors.inspect_run(run())))

        def check(schema, value):
            if "$ref" in schema:
                return check(schemas[schema["$ref"].rsplit("/", 1)[-1]], value)
            if "oneOf" in schema:
                return check(schema["oneOf"][1] if value is None else schema["oneOf"][0], value)
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(value), set(schema["properties"]))
                self.assertEqual(set(value), set(schema["required"]))
                for key, child in schema["properties"].items():
                    check(child, value[key])
            elif schema.get("type") == "array":
                self.assertLessEqual(len(value), schema["maxItems"])
                for item in value:
                    check(schema["items"], item)
            elif "enum" in schema:
                self.assertIn(value, schema["enum"])
            elif "const" in schema:
                self.assertEqual(value, schema["const"])

        for schema_name, sample in samples:
            check(schemas[schema_name], sample)

    def test_minimal_inactive_and_builtin_recipe(self):
        minimal = inspectors.inspect_recipe(recipe())
        self.assertEqual(minimal["schema_version"], 1)
        self.assertEqual(minimal["identity"]["package"], "inspection-demo")
        self.assertEqual(minimal["build"]["command_count"], 0)
        self.assertEqual(minimal["build"]["ensure_directory_count"], 0)
        self.assertFalse(minimal["counts_truncated"])
        self.assertEqual(minimal["automation"]["eligible"], False)
        inactive = inspectors.inspect_recipe(recipe(active=False, automation={"enabled": True, "policy": "build"}))
        self.assertFalse(inactive["automation"]["eligible"])
        builtin = recipe_document_for_storage({"schema_version": 5, "name": "debbuilder",
                                              "source": {"repository": "owner/project"},
                                              "management": {"owner": "application", "builtin_id": "debbuilder",
                                                             "definition_version": 1, "operator_overrides": {}}})
        managed = inspectors.inspect_recipe(builtin)
        self.assertEqual(managed["identity"]["source"], "builtin")
        self.assertTrue(managed["identity"]["managed"])

    def test_complex_recipe_summary_excludes_all_authored_text(self):
        complex_recipe = recipe(
            automation={"enabled": True, "policy": "build"},
            build={"commands": [SENTINELS[5]], "environment": {"SECRET": SENTINELS[4]},
                   "source_changes": [{"operation": "create_file", "path": "safe.txt",
                                       "content": SENTINELS[7]}],
                   "output": {"mode": "paths", "paths": ["dist/a", "dist/b"]}},
            install={"maintainer_scripts": {"postinst": SENTINELS[6]}},
            service={"enabled": True, "name": "demo.service", "command": "/bin/true",
                     "environment": {"OIDC_SECRET": SENTINELS[1]}},
            artifact={"mode": "upstream_archive", "type": "archive", "archive_source": "github_source",
                      "payload": {"mode": "paths", "include": ["src/"], "exclude": ["src/private/"]}},
        )
        projected = inspectors.inspect_recipe(complex_recipe, observation={
            "last_attempt": {"classification": "detected"},
            "last_success": {"display_version": SENTINELS[0], "display_ref": SENTINELS[2]},
        })
        self.assertEqual(projected["build"]["command_count"], 1)
        self.assertEqual(projected["artifact"]["mode"], "upstream_archive")
        self.assertEqual(projected["artifact"]["include_count"], 1)
        self.assertTrue(projected["installation"]["maintainer_scripts_present"])
        self.assertTrue(projected["service"]["configured"])
        self.assertTrue(projected["automation"]["eligible"])
        self.assertEqual(projected["observation"]["classification"], "detected")
        encoded = json.dumps(projected)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, encoded)

    def test_run_lifecycle_validation_publication_and_secret_exclusion(self):
        for status, terminal in (("queued", False), ("running", False),
                                 ("success", True), ("failed", True), ("cancelled", True)):
            with self.subTest(status=status):
                value = run(status)
                value["steps"][0]["status"] = "success"
                value["steps"][0]["summary"] = SENTINELS[8]
                value["steps"][0]["details"] = {"stderr": SENTINELS[8], "traceback": SENTINELS[9]}
                value["events"] = [{"message": sentinel} for sentinel in SENTINELS]
                value["artifact"] = {"path": SENTINELS[11], "name": SENTINELS[0], "size": 99,
                                     "sha256": "b" * 64,
                                     "inspection": {"package": "demo", "architecture": "amd64",
                                                    "version": SENTINELS[3]}}
                value["publications"] = [{"status": "failed", "error": {"message": SENTINELS[8]},
                                          "repository": {"root": SENTINELS[10],
                                                         "distribution": "stable", "component": "main"}}]
                value["error"] = {"code": "build_failed", "message": SENTINELS[8],
                                  "traceback": SENTINELS[9], "stage": "source"}
                value["recovery"] = {"status": "blocked", "backend": "systemd_cgroup",
                                     "reason": SENTINELS[10]}
                projected = inspectors.inspect_run(value, validation={"id": "attempt-1", "status": "failed",
                                                                       "stderr": SENTINELS[8],
                                                                       "recovery_blocker": {"message": SENTINELS[10]}},
                                                   validation_count=1)
                self.assertEqual(projected["schema_version"], 1)
                self.assertEqual(projected["identity"]["terminal"], terminal)
                self.assertEqual(projected["lifecycle"]["current_stage"], "source")
                self.assertEqual(projected["artifact"]["sha256"], "b" * 64)
                self.assertEqual(projected["validation"]["status"], "failed")
                self.assertEqual(projected["publication"]["status"], "failed")
                self.assertEqual(projected["error"], {"code": "build_failed", "stage": "source"})
                encoded = json.dumps(projected)
                for sentinel in SENTINELS:
                    self.assertNotIn(sentinel, encoded)

    def test_run_absent_artifact_and_no_attempts(self):
        projected = inspectors.inspect_run(run())
        self.assertFalse(projected["artifact"]["available"])
        self.assertEqual(projected["validation"]["status"], "not_run")
        self.assertEqual(projected["publication"]["status"], "not_run")
        self.assertEqual(projected["lifecycle"]["step_count"], 10)
        self.assertFalse(projected["lifecycle"]["steps_truncated"])
        self.assertFalse(projected["execution"]["cancellable"])
        self.assertTrue(inspectors.inspect_run(run(), cancellation_owned=True)["execution"]["cancellable"])

    def test_recipe_and_run_inspectors_expose_only_ensured_directory_counts(self):
        configured = recipe(build={
            "output": {"mode": "path", "path": "apps/server"},
            "ensure_directories": ["apps/server/node_modules"],
        })
        recipe_inspection = inspectors.inspect_recipe(configured)
        self.assertEqual(recipe_inspection["build"]["ensure_directory_count"], 1)
        self.assertNotIn("node_modules", json.dumps(recipe_inspection))

        value = run("success")
        value["steps"][4].update({
            "status": "success",
            "details": {"ensure_directories": {
                "requested": 2, "created": 1, "already_existed": 1,
                "entries": [{"path": SENTINELS[10], "status": "created"}],
            }},
        })
        run_inspection = inspectors.inspect_run(value)
        self.assertEqual(run_inspection["build"]["ensure_directories"], {
            "requested": 2, "created": 1, "already_existed": 1,
        })
        self.assertNotIn(SENTINELS[10], json.dumps(run_inspection))

    def test_suspicious_identity_and_public_metadata_are_suppressed(self):
        suspicious = inspectors.inspect_recipe(recipe(name="password-123"))
        self.assertIsNone(suspicious["identity"]["recipe_id"])
        self.assertIsNone(suspicious["identity"]["package"])
        value = run()
        value["publications"] = [{"status": "failed", "repository": {
            "distribution": "password-123", "component": "main"}}]
        value["error"] = {"code": "ghp_super_secret", "message": SENTINELS[0]}
        projected = inspectors.inspect_run(value)
        self.assertIsNone(projected["publication"]["suite"])
        self.assertEqual(projected["error"]["code"], "other")
        self.assertNotIn("password-123", json.dumps(projected))

    def test_successful_publication_uses_canonical_proof_projection(self):
        value = run("success")
        value["publications"] = [{"status": "success", "repository": {
            "distribution": "stable", "component": "main"}}]
        with mock.patch.object(inspectors.artifact_publication, "publication_attempt_status",
                               return_value="success") as status_probe, \
             mock.patch.object(inspectors.artifact_publication, "successful_publication_proof",
                               return_value={"schema": "proof"}) as proof_probe:
            projected = inspectors.inspect_run(value)
        self.assertTrue(projected["publication"]["published"])
        self.assertEqual(projected["publication"]["status"], "success")
        status_probe.assert_called_once()
        proof_probe.assert_called_once()


class InspectorHttpTests(AdminApiCase):
    def error(self, path, expected_status, expected_code):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("GET", path)
        self.assertEqual(captured.exception.code, expected_status)
        self.assert_api_error(json.loads(captured.exception.read()), code=expected_code)

    def test_routes_contract_content_type_and_read_only(self):
        for path, name in (("/api/recipes/webapp-recipe/inspect", "RecipeInspectionResponse"),
                           ("/api/executions/20260822-031400/inspect", "RunInspectionResponse")):
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
                    self.assertEqual(json.loads(response.read())["inspection"]["schema_version"], 1)
                matched = match_route("GET", path)
                self.assertIs(matched.route.effect, RouteEffect.READ_ONLY)
                operation = openapi_document()["paths"][matched.route.path_template]["get"]
                self.assertEqual(operation["responses"]["200"]["content"]["application/json"]["schema"],
                                 {"$ref": f"#/components/schemas/{name}"})

    def test_errors_are_canonical_and_auth_is_unchanged(self):
        self.error("/api/recipes/%2F/inspect", 400, "invalid_recipe_id")
        self.error("/api/recipes/missing/inspect", 404, "recipe_not_found")
        self.error("/api/executions/%2F/inspect", 400, "invalid_execution_id")
        self.error("/api/executions/missing/inspect", 404, "build_run_not_found")
        bad_recipe = app.USER_WORKFLOWS / "corrupt.json"
        bad_recipe.write_text("{broken")
        self.error("/api/recipes/corrupt/inspect", 409, "recipe_inspection_unavailable")
        (app.USER_WORKFLOWS / "oversized.json").write_text("x" * (inspectors.MAX_RECIPE_BYTES + 1))
        self.error("/api/recipes/oversized/inspect", 409, "recipe_inspection_unavailable")
        bad_run = app.DATA / "builds" / "corrupt-run"
        bad_run.mkdir()
        (bad_run / "run.json").write_text("{broken")
        self.error("/api/executions/corrupt-run/inspect", 409, "run_inspection_unavailable")
        oversized_run = app.DATA / "builds" / "oversized-run"
        oversized_run.mkdir()
        with (oversized_run / "run.json").open("wb") as handle:
            handle.truncate(inspectors.MAX_RUN_BYTES + 1)
        self.error("/api/executions/oversized-run/inspect", 409, "run_inspection_unavailable")
        settings = app.app_settings()
        settings["security"]["auth_mode"] = "header"
        with mock.patch.object(app, "app_settings", return_value=settings):
            self.error("/api/recipes/webapp-recipe/inspect", 401, "authentication_required")
            status, _body = self.request("GET", "/api/recipes/webapp-recipe/inspect",
                                          headers={"X-Forwarded-User": "operator"})
            self.assertEqual(status, 200)

    def test_inspection_does_not_mutate_or_call_external_subsystems(self):
        with mock.patch.object(app.recipe_store, "save_recipe") as save_recipe, \
             mock.patch.object(app.BuildStore, "save") as save_run, \
             mock.patch.object(app.upstream_detection, "detect_upstream") as refresh, \
             mock.patch.object(app.validation_oci.PodmanRuntime, "run") as oci, \
             mock.patch.object(app.artifact_publication, "publish_artifact") as publish:
            before = {str(path.relative_to(app.DATA)): (path.stat().st_size, path.stat().st_mtime_ns)
                      for path in app.DATA.rglob("*") if path.is_file()}
            self.request("GET", "/api/recipes/webapp-recipe/inspect")
            self.request("GET", "/api/executions/20260822-031400/inspect")
            after = {str(path.relative_to(app.DATA)): (path.stat().st_size, path.stat().st_mtime_ns)
                     for path in app.DATA.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            for operation in (save_recipe, save_run, refresh, oci, publish):
                operation.assert_not_called()

    def test_validation_inventory_bound_reports_unknown_without_loading_history(self):
        root = app.DATA / "builds" / "20260822-031400" / "manifests" / "validation-attempts"
        root.mkdir(parents=True, exist_ok=True)
        for index in range(inspectors.MAX_VALIDATION_ATTEMPTS + 1):
            (root / f"attempt-{index:04d}").mkdir()
        with mock.patch.object(app.validation_service, "load_attempt") as loader:
            status, body = self.request("GET", "/api/executions/20260822-031400/inspect")
        self.assertEqual(status, 200)
        self.assertEqual(body["inspection"]["validation"]["status"], "unknown")
        self.assertTrue(body["inspection"]["validation"]["inventory_truncated"])
        loader.assert_not_called()

    def test_one_validation_attempt_is_loaded_for_summary(self):
        root = app.DATA / "builds" / "20260822-031400" / "manifests" / "validation-attempts"
        selected = "20260924-121212-123456-abcd"
        (root / selected).mkdir(parents=True)
        with mock.patch.object(app.validation_service, "load_attempt",
                               return_value={"id": selected, "status": "running"}) as loader:
            status, body = self.request("GET", "/api/executions/20260822-031400/inspect")
        self.assertEqual(status, 200)
        self.assertEqual(body["inspection"]["validation"]["attempt_count"], 1)
        self.assertEqual(body["inspection"]["validation"]["attempt_id"], selected)
        self.assertEqual(body["inspection"]["validation"]["status"], "running")
        loader.assert_called_once()
