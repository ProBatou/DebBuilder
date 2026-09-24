"""Support bundle assembly and HTTP contract tests."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from unittest import TestCase, mock

from debbuilder import __version__, app, inspectors
from debbuilder.build_models import new_run
from debbuilder.openapi import openapi_document
from debbuilder.recipe_schema import recipe_document_for_storage
from debbuilder.support_bundle import (
    MAX_BUNDLE_BYTES, MAX_ENTRY_BYTES, MAX_UNCOMPRESSED_BYTES,
    SupportBundleError, build_support_bundle,
)
from debbuilder.system_diagnostics import CHECK_IDS
from tests.admin_api_case import AdminApiCase


def diagnostics():
    return {"schema_version": 1, "status": "ok", "checks": [
        {"id": check_id, "status": "ok", "message": "Available", "details": {}}
        for check_id in CHECK_IDS
    ]}


def recipe():
    return {"schema_version": 1, "counts_truncated": False,
            "identity": {}, "source": {}, "build": {}, "artifact": {},
            "installation": {}, "service": {}, "automation": {}, "observation": {}}


def run():
    return {"schema_version": 1, "identity": {}, "lifecycle": {}, "artifact": {},
            "validation": {}, "publication": {}, "execution": {}, "error": {}}


class SupportBundleBuilderTests(TestCase):
    def test_sensitive_source_fields_never_enter_any_zip_entry(self):
        sentinels = (
            "ghp_SUPER_SECRET", "oidc-client-secret", "cookie-secret", "notification-token",
            "password-123", "Authorization: Bearer sensitive", "SENSITIVE_ENV=value",
            "command --token=secret", "maintainer-script-secret", "stderr-secret",
            "Traceback private", "/var/lib/debbuilder/private/data",
            "/tmp/workspace-secret/data", "https://user:password@example.invalid",
        )
        authored = recipe_document_for_storage({
            "schema_version": 5, "name": "support-demo",
            "package": {"description": sentinels[0], "maintainer": sentinels[1]},
            "source": {"repository": "owner/project"},
            "build": {"commands": [sentinels[7]],
                      "environment": {"TOKEN": sentinels[6], "AUTH": sentinels[5]}},
            "install": {"maintainer_scripts": {"postinst": sentinels[8]}},
        })
        source_run = new_run("support-run", "support-demo", "build", sentinels[12], "a" * 64)
        source_run["steps"][0]["summary"] = sentinels[9]
        source_run["events"] = [{"message": text} for text in sentinels]
        source_run["error"] = {"code": "build_failed", "message": sentinels[10], "traceback": sentinels[10]}
        source_run["artifact"] = {"path": sentinels[11], "name": sentinels[3], "size": 99,
                                  "inspection": {"package": "support-demo", "version": sentinels[4]}}
        source_run["publications"] = [{"status": "failed", "error": {"message": sentinels[13]}}]
        projection_recipe = inspectors.inspect_recipe(authored, observation={
            "last_attempt": {"classification": "detected", "display_ref": sentinels[2]},
            "last_success": {"display_version": sentinels[13]},
        })
        projection_run = inspectors.inspect_run(source_run, validation={
            "status": "failed", "stderr": sentinels[9], "recovery_blocker": {"message": sentinels[11]},
        }, validation_count=1)
        payload = build_support_bundle(diagnostics=diagnostics(),
                                       recipe_inspection=projection_recipe,
                                       run_inspection=projection_run)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            contents = b"\n".join(archive.read(name) for name in archive.namelist())
            names = "\n".join(archive.namelist()).encode()
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel):
                encoded = sentinel.encode()
                self.assertNotIn(encoded, payload)
                self.assertNotIn(encoded, contents)
                self.assertNotIn(encoded, names)

    def test_all_selection_combinations_are_exact_and_deterministic(self):
        for selected_recipe, selected_run in ((None, None), (recipe(), None), (None, run()), (recipe(), run())):
            with self.subTest(recipe=selected_recipe is not None, run=selected_run is not None):
                kwargs = {"diagnostics": diagnostics(), "recipe_inspection": selected_recipe,
                          "run_inspection": selected_run}
                payload = build_support_bundle(**kwargs)
                self.assertEqual(payload, build_support_bundle(**kwargs))
                self.assertLessEqual(len(payload), MAX_BUNDLE_BYTES)
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    expected = ["manifest.json", "system-diagnostics.json"]
                    if selected_recipe is not None:
                        expected.append("recipe-inspection.json")
                    if selected_run is not None:
                        expected.append("run-inspection.json")
                    self.assertEqual(archive.namelist(), expected)
                    self.assertEqual(len(archive.infolist()), len(expected))
                    self.assertLessEqual(sum(row.file_size for row in archive.infolist()), MAX_UNCOMPRESSED_BYTES)
                    for info in archive.infolist():
                        self.assertFalse(info.filename.startswith("/"))
                        self.assertNotIn("..", info.filename)
                        self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                        self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                        self.assertEqual((info.external_attr >> 16) & 0o170000, 0o100000)
                        self.assertLessEqual(info.file_size, MAX_ENTRY_BYTES)
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest, {"schema_version": 1,
                        "application": {"name": "DebBuilder", "version": __version__},
                        "contents": expected,
                        "selection": {"recipe": selected_recipe is not None,
                                      "run": selected_run is not None}})
                    self.assertEqual(json.loads(archive.read("system-diagnostics.json")), diagnostics())
                    if selected_recipe is not None:
                        self.assertEqual(json.loads(archive.read("recipe-inspection.json")), selected_recipe)
                    if selected_run is not None:
                        self.assertEqual(json.loads(archive.read("run-inspection.json")), selected_run)

    def test_invalid_projections_and_size_fail_without_an_archive(self):
        cases = [
            {"diagnostics": {**diagnostics(), "raw_settings": "secret"}},
            {"diagnostics": {**diagnostics(), "schema_version": 2}},
            {"diagnostics": {**diagnostics(), "checks": []}},
            {"diagnostics": diagnostics(), "recipe_inspection": {**recipe(), "raw_recipe": {}}},
            {"diagnostics": {**diagnostics(), "checks": [
                {**row, "message": "x" * MAX_ENTRY_BYTES} if index == 0 else row
                for index, row in enumerate(diagnostics()["checks"])]}},
            {"diagnostics": {**diagnostics(), "status": object()}},
            {"diagnostics": {**diagnostics(), "status": float("nan")}},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(SupportBundleError):
                    build_support_bundle(**kwargs)

    def test_builder_does_not_read_files_or_call_subprocesses(self):
        with mock.patch("builtins.open", side_effect=AssertionError("filesystem access")), \
             mock.patch("subprocess.run", side_effect=AssertionError("subprocess")):
            self.assertTrue(build_support_bundle(diagnostics=diagnostics()).startswith(b"PK"))

    def test_total_and_final_limits_are_independent(self):
        with mock.patch("debbuilder.support_bundle.MAX_UNCOMPRESSED_BYTES", 10):
            with self.assertRaises(SupportBundleError):
                build_support_bundle(diagnostics=diagnostics())
        with mock.patch("debbuilder.support_bundle.MAX_BUNDLE_BYTES", 10):
            with self.assertRaises(SupportBundleError):
                build_support_bundle(diagnostics=diagnostics())


class SupportBundleHttpTests(AdminApiCase):
    def _request_bundle(self, query=""):
        request = urllib.request.Request(self.base_url + "/api/support-bundle" + query)
        return urllib.request.urlopen(request, timeout=5)

    def test_http_bundle_uses_canonical_projections_and_headers(self):
        path = app.EXAMPLES / "webapp-recipe.json"
        raw_recipe = json.loads(path.read_text())
        raw_recipe["package"]["description"] = "SECRET_DESCRIPTION_SENTINEL"
        raw_recipe["build"] = {"commands": ["echo SECRET_COMMAND_SENTINEL"]}
        path.write_text(json.dumps(raw_recipe))
        expected_recipe = app.get_recipe_inspection("webapp-recipe")
        expected_run = app.get_run_inspection("20260822-031400", manager=self.execution_manager)
        with mock.patch.object(app, "system_diagnostics_snapshot", return_value=diagnostics()):
            with self._request_bundle("?recipe_id=webapp-recipe&run_id=20260822-031400") as response:
                payload = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "application/zip")
                self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="debbuilder-support.zip"')
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(int(response.headers["Content-Length"]), len(payload))
        self.assertNotIn(b"SECRET_DESCRIPTION_SENTINEL", payload)
        self.assertNotIn(b"SECRET_COMMAND_SENTINEL", payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(json.loads(archive.read("recipe-inspection.json")), expected_recipe)
            self.assertEqual(json.loads(archive.read("run-inspection.json")), expected_run)

    def test_global_bundle_and_no_raw_sensitive_fields(self):
        with self._request_bundle() as response:
            payload = response.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["manifest.json", "system-diagnostics.json"])
            self.assertNotIn(b"ghp_", payload)
            self.assertNotIn(b"maintainer", payload)
            self.assertNotIn(b"command", payload)

    def test_single_selection_and_authentication(self):
        for query, expected_name in (("?recipe_id=webapp-recipe", "recipe-inspection.json"),
                                     ("?run_id=20260822-031400", "run-inspection.json")):
            with self.subTest(query=query), self._request_bundle(query) as response:
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertEqual(archive.namelist(), ["manifest.json", "system-diagnostics.json", expected_name])
        app.AUTH_MODE = "header"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._request_bundle()
        self.assertEqual(caught.exception.code, 401)
        self.assert_api_error(json.loads(caught.exception.read()), code="authentication_required")
        request = urllib.request.Request(self.base_url + "/api/support-bundle", headers={app.AUTH_HEADER: "operator"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")

    def test_query_errors_are_canonical(self):
        cases = [
            ("?unknown=value", 400, "invalid_support_bundle_request"),
            ("?recipe_id=a&recipe_id=b", 400, "invalid_support_bundle_request"),
            ("?run_id=a&run_id=b", 400, "invalid_support_bundle_request"),
            ("?recipe_id=", 400, "invalid_recipe_id"),
            ("?run_id=", 400, "invalid_execution_id"),
            ("?recipe_id=bad%2Fid", 400, "invalid_recipe_id"),
            ("?run_id=bad%2Fid", 400, "invalid_execution_id"),
            ("?recipe_id=absent", 404, "recipe_not_found"),
            ("?run_id=absent", 404, "build_run_not_found"),
        ]
        for query, status, code in cases:
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as caught:
                self._request_bundle(query)
            self.assertEqual(caught.exception.code, status)
            self.assertEqual(caught.exception.headers["Content-Type"], "application/json; charset=utf-8")
            self.assert_api_error(json.loads(caught.exception.read()), code=code)

    def test_inspection_and_assembly_failures_are_canonical(self):
        with mock.patch.object(app, "get_recipe_inspection", side_effect=app.InspectionReadError("recipe_inspection_unavailable")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._request_bundle("?recipe_id=webapp-recipe")
            self.assertEqual(caught.exception.code, 409)
            self.assert_api_error(json.loads(caught.exception.read()), code="recipe_inspection_unavailable")
        with mock.patch.object(app, "get_run_inspection", side_effect=app.InspectionReadError("run_inspection_unavailable")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._request_bundle("?run_id=20260822-031400")
            self.assertEqual(caught.exception.code, 409)
            self.assert_api_error(json.loads(caught.exception.read()), code="run_inspection_unavailable")
        with mock.patch("debbuilder.http_handler.build_support_bundle", side_effect=SupportBundleError("secret details")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._request_bundle()
            self.assertEqual(caught.exception.code, 500)
            body = caught.exception.read()
            self.assertNotIn(b"secret details", body)
            self.assert_api_error(json.loads(body), code="support_bundle_unavailable")

    def test_openapi_binary_contract(self):
        operation = openapi_document()["paths"]["/api/support-bundle"]["get"]
        self.assertEqual(operation["x-debbuilder-effect"], "read_only")
        self.assertEqual(set(operation["responses"]["200"]["content"]), {"application/zip"})
        self.assertEqual({row["name"] for row in operation["parameters"]}, {"recipe_id", "run_id"})

    def test_download_has_no_persistent_or_external_side_effects(self):
        def snapshot():
            return {str(path.relative_to(app.DATA)): (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in app.DATA.rglob("*") if path.is_file()}

        before = snapshot()
        with mock.patch.object(app.recipe_store, "save_recipe", side_effect=AssertionError("Recipe write")), \
             mock.patch.object(app.BuildStore, "save", side_effect=AssertionError("Run write")), \
             mock.patch.object(app.upstream_detection, "detect_upstream", side_effect=AssertionError("upstream refresh")), \
             mock.patch.object(app.validation_oci.PodmanRuntime, "run", side_effect=AssertionError("OCI")), \
             mock.patch.object(app.artifact_publication, "publish_artifact", side_effect=AssertionError("publication")), \
             mock.patch.object(app, "enqueue_recipe_run", side_effect=AssertionError("build")):
            with self._request_bundle("?recipe_id=webapp-recipe&run_id=20260822-031400") as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().startswith(b"PK"))
        self.assertEqual(snapshot(), before)
