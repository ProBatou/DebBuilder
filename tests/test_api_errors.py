import json
import urllib.error
import urllib.request
from unittest import mock

from debbuilder import app as server
from debbuilder.api_errors import ApiError, STABLE_ERROR_CODES, canonical_error_payload
from tests.admin_api_case import AdminApiCase


class ApiErrorModelTests(AdminApiCase):
    def error_response(self, method, path, body=None, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(method, path, body, headers=headers)
        payload = json.loads(captured.exception.read())
        return captured.exception.code, self.assert_api_error(payload)

    def test_legacy_shapes_normalize_to_one_exact_envelope(self):
        cases = (
            ({"error": "unauthorized"}, 401, "/api/status", "authentication_required"),
            ({"error": "not found"}, 404, "/api/packages/missing", "not_found"),
            ({"error": {"code": "invalid_recipe_json", "message": "raw", "path": "$.name"}}, 422, "/api/recipes/validate", "invalid_recipe_json"),
            ({"code": "execution_active", "error": "raw"}, 409, "/api/executions/run/logs", "execution_active"),
            ({"publication": {"error": {"code": "publication_identity_conflict", "message": "raw"}}}, 409, "/api/executions/run/publish", "publication_identity_conflict"),
            ({"ok": False, "notification": {"debug": "raw"}}, 502, "/api/notifications/test", "notification_failed"),
        )
        for source, status, path, code in cases:
            with self.subTest(code=code):
                payload = canonical_error_payload(source, status, path)
                error = self.assert_api_error(payload, code=code)
                self.assertNotIn("raw", json.dumps(error))

        archive = canonical_error_payload({"error": {
            "code": "ambiguous_archive_source",
            "message": "raw",
            "details": {"sources": [{
                "source": "github_source", "name": "source-v1.tar.gz",
                "archive_format": "tar.gz", "size": 42, "url": "private-url",
            }]},
        }}, 422, "/api/upstream-archive/inspect")
        self.assertEqual(archive["error"]["details"], {"sources": [{
            "source": "github_source", "name": "source-v1.tar.gz",
            "archive_format": "tar.gz", "size": 42,
        }]})
        self.assertNotIn("private-url", json.dumps(archive))

    def test_stable_client_error_code_inventory_is_reviewable(self):
        expected = {
            "application_shutting_down", "authentication_required", "authentication_unavailable",
            "automation_disabled", "build_run_not_found", "execution_active",
            "execution_not_cancellable", "forbidden", "internal_error", "invalid_json",
            "invalid_request", "invalid_validation_identity", "not_found", "notification_failed",
            "publication_identity_conflict", "readonly_recipe", "recipe_exists",
            "repository_mutation_busy", "settings_unavailable", "unsupported_recipe_schema",
        }
        self.assertLessEqual(expected, STABLE_ERROR_CODES)

    def test_api_error_representation_always_includes_object_details(self):
        self.assertEqual(
            ApiError("invalid_request", "The request is invalid").as_dict(),
            {"code": "invalid_request", "message": "The request is invalid", "details": {}},
        )

    def test_representative_domain_failures_use_canonical_contract(self):
        cases = (
            ("POST", "/api/missing", {}, 404, "not_found"),
            ("GET", "/api/packages/missing", None, 404, "not_found"),
            ("POST", "/api/recipes/demo/automation/check", {"unexpected": True}, 400, "invalid_automation_check_request"),
            ("GET", "/api/executions/%2F/validations/invalid", None, 400, "invalid_validation_identity"),
            ("POST", "/api/settings", {"unknown": True}, 422, "unknown_settings_field"),
            ("POST", "/api/run", {"workflow": {"active": False}}, 409, "recipe_disabled"),
        )
        for method, path, body, expected_status, code in cases:
            with self.subTest(path=path):
                status, error = self.error_response(method, path, body)
                self.assertEqual(status, expected_status)
                self.assertEqual(error["code"], code)

        with mock.patch.object(
            server,
            "publish_build_artifact",
            side_effect=server.artifact_publication.PublicationError(
                "publication_identity_conflict", "unsafe raw publication text",
            ),
        ):
            status, error = self.error_response("POST", "/api/executions/run/publish", {})
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "publication_identity_conflict")

    def test_authentication_failure_remains_canonical_and_enforced(self):
        server.AUTH_MODE = "header"
        status, error = self.error_response(
            "GET", "/api/settings", headers={"Authorization": "Bearer sentinel-auth"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(error["code"], "authentication_required")

    def test_unexpected_failure_leaks_no_secret_or_internal_text(self):
        sentinels = (
            "sentinel-password", "Bearer sentinel-authorization", "sentinel-cookie",
            "sentinel-oidc-secret", "sentinel-github-token", "RECIPE_ENV_SECRET",
            "/private/runtime/settings.json", "PRIVATE-KEY-MATERIAL", "unsafe stderr",
        )
        with mock.patch.object(server, "update_settings", side_effect=RuntimeError(" ".join(sentinels))):
            status, error = self.error_response(
                "POST",
                "/api/settings",
                {"github_token": "sentinel-github-token", "environment": {"TOKEN": "RECIPE_ENV_SECRET"}},
                headers={"Cookie": "session=sentinel-cookie", "Authorization": "Bearer sentinel-authorization"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(error, {
            "code": "internal_error",
            "message": "An internal error occurred",
            "details": {},
        })
        serialized = json.dumps(error)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, serialized)

    def test_unexpected_get_failure_is_safe_and_logged(self):
        with (
            mock.patch.object(server, "settings_view", side_effect=RuntimeError("private GET failure")),
            self.assertLogs("debbuilder.http_handler", level="ERROR") as captured,
        ):
            status, error = self.error_response("GET", "/api/settings")
        self.assertEqual(status, 500)
        self.assertEqual(error, {
            "code": "internal_error",
            "message": "An internal error occurred",
            "details": {},
        })
        self.assertIn("private GET failure", "\n".join(captured.output))

    def test_malformed_json_has_bounded_structured_location(self):
        request = urllib.request.Request(
            self.base_url + "/api/settings",
            data=b'{"broken":',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(captured.exception.code, 400)
        error = self.assert_api_error(json.loads(captured.exception.read()), code="invalid_json")
        self.assertEqual(error["details"], {"line": 1, "column": 11, "path": "$"})


if __name__ == "__main__":
    import unittest

    unittest.main()
