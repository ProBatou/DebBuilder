import json
import threading
import urllib.error
from unittest import mock

from debbuilder import app as server
from debbuilder.lifecycle import MutationGate, is_durable_mutation_route
from debbuilder.release_cache import GitHubReleaseCache
from tests.admin_api_case import AdminApiCase


class HttpMutationLifecycleTests(AdminApiCase):
    def error_response(self, method, path, body=None):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(method, path, body)
        return captured.exception.code, json.loads(captured.exception.read())

    def assert_route_lease(self, method, path, body, target, result):
        gate = MutationGate()
        self.httpd.mutation_gate = gate
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        response = {}
        errors = []

        def delayed(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("mutation test was not released")
            completed.set()
            return result

        def request():
            try:
                response["value"] = self.request(method, path, body)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(server, target, side_effect=delayed):
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(entered.wait(2), path)
            gate.begin_shutdown()
            incomplete = gate.wait_for_quiescence(0)
            self.assertFalse(incomplete["complete"], path)
            self.assertEqual(incomplete["active_mutations"], 1)
            self.assertFalse(completed.is_set())
            release.set()
            thread.join(3)

        self.assertEqual(errors, [], path)
        self.assertFalse(thread.is_alive(), path)
        self.assertTrue(completed.is_set())
        self.assertTrue(gate.wait_for_quiescence(1)["complete"])
        self.assertIn(response["value"][0], {200, 202})

    def test_run_repository_workspace_mutations_hold_one_lifecycle_lease(self):
        cases = (
            (
                "POST", "/api/executions/20260822-031400/validate", {},
                "validate_build_artifact", {"status": "success"},
            ),
            (
                "POST", "/api/executions/20260822-031400/publish", {},
                "publish_build_artifact", {"status": "success"},
            ),
            (
                "POST", "/api/executions/20260822-031400/reconcile-publication", {},
                "reconcile_build_publication", {"status": "success"},
            ),
            (
                "DELETE", "/api/executions/20260822-031400/logs", None,
                "delete_execution_log", {"run_id": "20260822-031400"},
            ),
        )
        for method, path, body, target, result in cases:
            with self.subTest(path=path):
                self.assert_route_lease(method, path, body, target, result)

    def test_repository_busy_and_identity_conflict_map_to_http_conflict(self):
        for code in ("repository_mutation_busy", "publication_identity_conflict"):
            result = {"status": "failed", "error": {"code": code, "message": "busy", "details": {}}}
            with self.subTest(code=code), mock.patch.object(server, "publish_build_artifact", return_value=result):
                status, payload = self.error_response(
                    "POST", "/api/executions/20260822-031400/publish", {},
                )
            self.assertEqual(status, 409)
            self.assertEqual(payload["publication"]["error"]["code"], code)

    def test_queued_cancellation_releases_http_lease_after_bounded_request(self):
        gate = MutationGate()
        self.httpd.mutation_gate = gate
        requested = threading.Event()
        cancellation = {
            "outcome": "queued_cancelled",
            "cancellation": {"code": "execution_cancelled", "reason": "user_requested"},
        }
        maintenance_service = mock.Mock()
        maintenance_service.request.side_effect = lambda **_kwargs: requested.set()
        self.httpd.maintenance_service = maintenance_service

        with mock.patch.object(self.execution_manager, "cancel", return_value=cancellation):
            status, _payload = self.request(
                "POST", "/api/executions/20260822-031400/cancel", {},
            )

        self.assertEqual(status, 200)
        self.assertTrue(requested.is_set())
        maintenance_service.request.assert_called_once_with(cleanup=True)
        self.assertTrue(gate.wait_for_quiescence(0)["complete"])

    def test_new_mutator_is_rejected_but_read_only_request_is_excluded(self):
        gate = MutationGate()
        self.httpd.mutation_gate = gate
        gate.begin_shutdown()

        status, payload = self.error_response("POST", "/api/settings", {})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "application_shutting_down")
        self.assertEqual(self.request("GET", "/api/executions")[0], 200)
        self.assertTrue(gate.wait_for_quiescence(0)["complete"])

    def test_route_audit_classifies_all_durable_mutators_without_read_gets(self):
        durable = (
            ("POST", "/api/recipes/import"),
            ("POST", "/api/run"),
            ("POST", "/api/notifications/test"),
            ("POST", "/api/settings"),
            ("POST", "/api/executions/delete-logs"),
            ("POST", "/api/packages"),
            ("POST", "/api/packages/demo"),
            ("POST", "/api/workflows/demo"),
            ("POST", "/api/executions/run/cancel"),
            ("POST", "/api/executions/run/validate"),
            ("POST", "/api/executions/run/publish"),
            ("POST", "/api/executions/run/reconcile-publication"),
            ("DELETE", "/api/workflows/demo"),
            ("DELETE", "/api/executions/run/logs"),
            ("DELETE", "/api/packages/demo"),
        )
        for method, path in durable:
            self.assertTrue(is_durable_mutation_route(method, path), (method, path))
        for method, path in (
            ("GET", "/api/executions"),
            ("GET", "/api/executions/run/logs"),
            ("POST", "/api/recipes/validate"),
            ("POST", "/api/upstream-archive/inspect"),
        ):
            self.assertFalse(is_durable_mutation_route(method, path), (method, path))

    def test_read_triggered_cache_refresh_owns_its_background_durable_write(self):
        gate = MutationGate()
        cache = GitHubReleaseCache(server.DATA, lambda: "", workers=1, mutation_gate=gate)
        entered = threading.Event()
        release = threading.Event()

        def latest(_repository, *, token):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"tag": "v1"}

        try:
            with mock.patch("debbuilder.release_cache.github_client.latest_release", side_effect=latest):
                self.assertIsNone(cache.get("owner/lifecycle"))
                self.assertTrue(entered.wait(2))
                gate.begin_shutdown()
                self.assertFalse(gate.wait_for_quiescence(0)["complete"])
                release.set()
                self.assertTrue(gate.wait_for_quiescence(2)["complete"])
            cache.close()
        finally:
            release.set()
            cache.close()

        self.assertTrue(cache.path.is_file())


if __name__ == "__main__":
    import unittest

    unittest.main()
