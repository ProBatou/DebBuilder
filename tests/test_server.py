import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import debbuilder.app as server


class RecipePreviewTests(unittest.TestCase):
    def test_github_version_normalization_and_rejection(self):
        self.assertEqual(server.normalize_github_version("v1.2.3"), "1.2.3")
        self.assertEqual(server.normalize_github_version("V2.0~rc1"), "2.0~rc1")
        with self.assertRaisesRegex(ValueError, "not automatically usable"):
            server.normalize_github_version("9f2a3bc1")
        with self.assertRaisesRegex(ValueError, "not automatically usable"):
            server.normalize_github_version("release-latest")

    def test_preview_script_keeps_recipe_metadata_and_global_apt_settings(self):
        workflow = {
            "name": "webapp",
            "package_name": "webapp",
            "github_repository": "example/webapp",
            "version_tracking": "latest_release",
            "version_source": "tag",
            "active": True,
            "steps": [],
        }
        apt = {"repository": "https://apt.example.test", "distribution": "testing", "component": "main", "architecture": "amd64"}
        with mock.patch.object(server, "repo_settings", return_value=apt):
            script = server.generate_script(workflow)
        for expected in [
            "Legacy read-only recipe preview",
            "RECIPE_NAME=webapp",
            "PACKAGE_NAME=webapp",
            "GITHUB_REPOSITORY=example/webapp",
            "APT_REPOSITORY=https://apt.example.test",
            "APT_DISTRIBUTION=testing",
        ]:
            self.assertIn(expected, script)
        self.assertNotIn("repo.example.invalid", script)
        self.assertNotIn("eval", script)

    def test_preview_script_is_safe_to_execute_in_dry_run(self):
        script = server.generate_script({"name": "safe", "package_name": "safe", "github_repository": "example/safe"}, dry_run=True)
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "run.sh"
            script_path.write_text(script)
            proc = subprocess.run(["bash", str(script_path)], text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_stored_step_payload_is_preserved_but_not_executed_by_preview(self):
        workflow = {"name": "stored", "package_name": "stored", "github_repository": "example/stored", "steps": [{"type": "old_step", "value": 1}]}
        normalized = server.normalize_steps(workflow)
        self.assertEqual(normalized[0]["type"], "old_step")
        script = server.generate_script(workflow)
        self.assertIn("Ignored stored step payload", script)

    def test_saved_local_recipes_remain_readable(self):
        for path in (ROOT / "data" / "workflows").glob("*.json"):
            workflow = json.loads(path.read_text())
            server.normalize_steps(workflow)

    def test_recipe_ui_does_not_expose_removed_visual_runtime(self):
        html = (ROOT / "static" / "index.html").read_text()
        self.assertNotIn("block" + "ly", html.lower())
        self.assertIn("GitHub source", html)
        self.assertIn("Debian installation", html)
        self.assertIn("Service", html)


class WorkflowStorageTests(unittest.TestCase):
    def test_data_directory_can_be_separated_from_application_code(self):
        self.assertEqual(server.application_data_dir(Path("/opt/demo"), {}), Path("/opt/demo/data"))
        self.assertEqual(
            server.application_data_dir(Path("/opt/demo"), {"DEBBUILDER_DATA_DIR": "/var/lib/demo"}),
            Path("/var/lib/demo"),
        )
        self.assertEqual(server.REPOSITORY_ROOT, Path(server.os.environ.get("DEBBUILDER_REPO_ROOT", "/var/www/html")))

    def test_workflow_dirs_are_separated(self):
        self.assertTrue(hasattr(server, "EXAMPLES"))
        self.assertTrue(hasattr(server, "USER_WORKFLOWS"))
        self.assertNotEqual(server.EXAMPLES, server.USER_WORKFLOWS)


class AuthTests(unittest.TestCase):
    def test_header_auth_requires_forwarded_user_when_enabled(self):
        self.assertTrue(server.is_request_authorized({}, auth_mode="none"))
        self.assertFalse(server.is_request_authorized({}, auth_mode="header"))
        self.assertTrue(server.is_request_authorized({"X-Forwarded-User": "max"}, auth_mode="header"))

    def test_oidc_authorization_uses_discovery_state_and_nonce(self):
        discovery = {
            "issuer": "https://id.example.test",
            "authorization_endpoint": "https://id.example.test/authorize",
            "token_endpoint": "https://id.example.test/token",
            "userinfo_endpoint": "https://id.example.test/userinfo",
            "jwks_uri": "https://id.example.test/jwks",
        }
        config = {"auth_mode": "oidc", "oidc_issuer": discovery["issuer"], "oidc_client_id": "deb", "oidc_redirect_uri": "https://apt.example.test/auth/callback"}
        with mock.patch.object(server, "effective_security", return_value=config), mock.patch.object(server, "oidc_discovery", return_value=discovery):
            url, state = server.oidc_authorize_url("/settings")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"], [state])
        self.assertTrue(query["nonce"][0])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        session = server.SESSIONS.pop(f"state:{state}")
        expected = server.base64.urlsafe_b64encode(server.hashlib.sha256(session["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(session["return_to"], "/settings")

    def test_oidc_token_exchange_sends_pkce_verifier(self):
        discovery = {"token_endpoint": "https://id.example.test/token", "userinfo_endpoint": "https://id.example.test/userinfo", "jwks_uri": "https://id.example.test/jwks"}
        config = {"oidc_issuer": "https://id.example.test", "oidc_client_id": "deb", "oidc_redirect_uri": "https://apt.example.test/auth/callback"}
        requests = []

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def urlopen(req, timeout=0):
            requests.append(req)
            return Response({"access_token": "access", "id_token": "signed"}) if req.full_url.endswith("/token") else Response({"sub": "user-1"})

        with mock.patch.object(server, "effective_security", return_value=config), mock.patch.object(server, "oidc_discovery", return_value=discovery), mock.patch.object(server, "oidc_client_secret", return_value="secret"), mock.patch.object(server, "_validate_rs256", return_value={"sub": "user-1"}), mock.patch.object(server.urllib.request, "urlopen", side_effect=urlopen):
            server.exchange_oidc_code("code", "nonce", "pkce-verifier")
        token_body = parse_qs(requests[0].data.decode())
        self.assertEqual(token_body["code_verifier"], ["pkce-verifier"])


if __name__ == "__main__":
    unittest.main()
