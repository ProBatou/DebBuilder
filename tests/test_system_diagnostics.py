"""Contract, confidentiality and HTTP coverage for the read-only snapshot."""
from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from debbuilder import app, system_diagnostics
from debbuilder.api_routes import RouteEffect, match_route
from debbuilder.openapi import openapi_document
from tests.admin_api_case import AdminApiCase


class SystemDiagnosticsTests(TestCase):
    def make_snapshot(self, root, **changes):
        settings = {"security": {"auth_mode": "none"}, "apt": {
            "distribution": "stable", "component": "main", "architecture": "amd64"}}
        args = dict(runtime=SimpleNamespace(repository_root=root),
                    load_configuration=lambda: settings, load_secret_document=lambda: {},
                    execution_manager=SimpleNamespace(accepting=True, admission_blocker=None),
                    mutation_gate=SimpleNamespace(accepting=True),
                    scheduler=SimpleNamespace(status=lambda: {"state": "running", "admission_open": True,
                                                               "checks_enabled": True}),
                    orchestrator=object())
        args.update(changes)
        with mock.patch.object(system_diagnostics, "native_debian_architecture", return_value="amd64"), \
             mock.patch.object(system_diagnostics.command_containment, "cached_containment_capability",
                               return_value=SimpleNamespace(backend="systemd_cgroup", available=True,
                                                            reason="sensitive /private/path")), \
             mock.patch.object(system_diagnostics.shutil, "which", return_value=None):
            return system_diagnostics.build_system_diagnostics(**args)

    def test_contract_and_partial_failure_are_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "conf").mkdir()
            (root / "conf" / "distributions").write_text(
                "Codename: stable\nArchitectures: amd64\nComponents: main\n"
                "SignWith: " + "A" * 40 + "\n")
            (root / "dists" / "stable").mkdir(parents=True)
            (root / "dists" / "stable" / "InRelease").write_text("signed metadata")
            (root / "repository.gpg").write_bytes(b"public key")
            snapshot = self.make_snapshot(root)
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertEqual(snapshot["status"], "warning")  # OCI binary absent.
            checks = snapshot["checks"]
            self.assertEqual([row["id"] for row in checks], list(system_diagnostics.CHECK_IDS))
            self.assertTrue(all(row["status"] in system_diagnostics.STATUSES for row in checks))
            self.assertEqual(len({row["id"] for row in checks}), len(checks))
            self.assertEqual(checks[2]["status"], "ok")
            self.assertEqual(checks[2]["details"]["signing_fingerprint"], "A" * 40)
            broken = self.make_snapshot(root, load_secret_document=lambda: 1 / 0,
                                        execution_manager=SimpleNamespace(
                                            accepting=False,
                                            admission_blocker={"code": "secret", "message": "/private"}))
            self.assertEqual(broken["checks"][1]["status"], "unknown")
            self.assertEqual(broken["checks"][2]["status"], "ok")
            self.assertEqual(broken["checks"][4]["status"], "warning")
            self.assertEqual(broken["checks"][4]["details"], {"open": False, "recovery_blocked": True})

    def test_sentinels_and_unbounded_configuration_do_not_escape(self):
        sentinels = ("ghp_SecretToken123", "oidc-client-secret-123", "cookie-secret-123",
                     "/private/operator/data", "stderr-secret-123", "ENV_SECRET=abc")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "conf").mkdir()
            (root / "conf" / "distributions").write_text(sentinels[4] * 9000)
            snapshot = self.make_snapshot(
                root, load_configuration=lambda: {"security": {"auth_mode": "none"},
                                                  "apt": {"distribution": "stable", "component": "main",
                                                          "architecture": "amd64"},
                                                  "private": sentinels},
                load_secret_document=lambda: {"secrets": sentinels})
            encoded = json.dumps(snapshot)
            for sentinel in sentinels:
                self.assertNotIn(sentinel, encoded)
            self.assertEqual(snapshot["checks"][2]["status"], "warning")

    def test_snapshot_does_not_call_mutating_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(system_diagnostics.local_repository_bootstrap, "bootstrap_local_repository",
                               create=True) as bootstrap, \
             mock.patch("subprocess.run") as command:
            self.make_snapshot(Path(temporary))
            bootstrap.assert_not_called()
            command.assert_not_called()

    def test_global_status_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(system_diagnostics, "_repository",
                                   return_value=("failed", "Repository configuration failed", {})):
                snapshot = self.make_snapshot(Path(temporary))
            self.assertEqual(snapshot["status"], "failed")
            self.assertEqual(snapshot["checks"][2]["status"], "failed")


class SystemDiagnosticsHttpTests(AdminApiCase):
    def test_authenticated_contract_and_read_only_registry(self):
        before = {str(path.relative_to(app.DATA)): (path.stat().st_size, path.stat().st_mtime_ns)
                  for path in app.DATA.rglob("*") if path.is_file()}
        with urllib.request.urlopen(self.base_url + "/api/system/diagnostics", timeout=5) as response:
            status = response.status
            self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
            body = json.loads(response.read())
        self.assertEqual(status, 200)
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(len(body["checks"]), len(system_diagnostics.CHECK_IDS))
        route = match_route("GET", "/api/system/diagnostics").route
        self.assertIs(route.effect, RouteEffect.READ_ONLY)
        operation = openapi_document()["paths"]["/api/system/diagnostics"]["get"]
        self.assertEqual(operation["responses"]["200"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/SystemDiagnosticsResponse"})
        after = {str(path.relative_to(app.DATA)): (path.stat().st_size, path.stat().st_mtime_ns)
                 for path in app.DATA.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_probe_exception_is_sanitized_over_http(self):
        secret = "stderr-secret-123 /private/operator/path"
        with mock.patch.object(system_diagnostics, "native_debian_architecture",
                               side_effect=RuntimeError(secret)):
            status, body = self.request("GET", "/api/system/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(body["checks"][0]["status"], "unknown")
        self.assertNotIn(secret, json.dumps(body))

    def test_header_mode_rejects_unauthorized_request_without_secret_leak(self):
        settings = app.app_settings()
        settings["security"]["auth_mode"] = "header"
        with mock.patch.object(app, "app_settings", return_value=settings):
            with self.assertRaises(urllib.error.HTTPError) as captured:
                self.request("GET", "/api/system/diagnostics")
            body = json.loads(captured.exception.read())
            self.assert_api_error(body, code="authentication_required")
            status, body = self.request("GET", "/api/system/diagnostics",
                                        headers={"X-Forwarded-User": "operator"})
            self.assertEqual(status, 200)
            self.assertEqual(body["checks"][1]["details"], {"auth_mode": "header"})
