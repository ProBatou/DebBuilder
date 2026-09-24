import http.client
from dataclasses import replace
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server as entrypoint
from debbuilder import app
from debbuilder.local_repository_bootstrap import (
    LocalRepositoryBootstrapError, _render_public_assets, bootstrap_repository,
)
from debbuilder.repository_lock import repository_lease
from debbuilder.runtime import RuntimeConfig, listeners_overlap
from debbuilder.settings_store import SettingsDocumentError, _validate_repo_url


def request(port, path, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


class ListenerConfigurationTests(unittest.TestCase):
    def test_defaults_overrides_and_collisions(self):
        root = Path("/opt/debbuilder")
        default = RuntimeConfig.from_environment(root, {})
        self.assertEqual((default.host, default.port), ("127.0.0.1", 8099))
        self.assertEqual((default.repository_host, default.repository_port), ("127.0.0.1", 8081))
        override = RuntimeConfig.from_environment(root, {
            "DEBBUILDER_HOST": "0.0.0.0", "DEBBUILDER_PORT": "8123",
            "DEBBUILDER_REPOSITORY_HOST": "192.0.2.7", "DEBBUILDER_REPOSITORY_PORT": "8124",
        })
        self.assertEqual((override.host, override.port, override.repository_host, override.repository_port),
                         ("0.0.0.0", 8123, "192.0.2.7", 8124))
        self.assertTrue(listeners_overlap("0.0.0.0", 8123, "127.0.0.1", 8123))
        self.assertTrue(listeners_overlap("localhost", 8123, "127.0.0.1", 8123))
        self.assertFalse(listeners_overlap("127.0.0.1", 8123, "127.0.0.1", 8124))
        for port in ("-1", "65536", "abc"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                RuntimeConfig.from_environment(root, {"DEBBUILDER_REPOSITORY_PORT": port})
        with self.assertRaises(ValueError):
            RuntimeConfig.from_environment(root, {"DEBBUILDER_REPOSITORY_HOST": "host;bad"})

    def test_public_url_validation(self):
        self.assertEqual(_validate_repo_url(""), "")
        self.assertEqual(_validate_repo_url("https://repo.example.test/base/"), "https://repo.example.test/base")
        self.assertEqual(_validate_repo_url("http://repo.example.test"), "http://repo.example.test")
        for invalid in (
            "https://user:pass@repo.example.test", "https://repo.example.test/?x=1",
            "https://repo.example.test/#x", "https://repo.example.test\n", "https://repo.example.test/a b",
            "https://repo.example.test/$(touch /tmp/pwn)", "ftp://repo.example.test",
            "https://repo.example.test/../private", "https://repo.example.test:99999",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SettingsDocumentError):
                _validate_repo_url(invalid)

    def test_product_entrypoint_rejects_ephemeral_listener_ports(self):
        with mock.patch.object(app, "RUNTIME", replace(app.RUNTIME, repository_port=0)):
            with self.assertRaisesRegex(ValueError, "fixed ports"):
                entrypoint.main()


class PublicRepositoryListenerTests(unittest.TestCase):
    def test_route_isolation_and_file_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            (repo / "dists/stable").mkdir(parents=True)
            (repo / "pool/main").mkdir(parents=True)
            (repo / "conf").mkdir()
            (repo / "db").mkdir()
            for name, data in {
                "index.html": b"<h1>Repository</h1>", "install.sh": b"#!/bin/sh\nexit 0\n",
                "repository.gpg": b"public-key", "dists/stable/InRelease": b"signed metadata",
                "pool/main/demo.deb": b"package", "conf/distributions": b"private config",
                "db/version": b"private database",
            }.items():
                (repo / name).write_bytes(data)
            (repo / "pool/main/linked.deb").symlink_to(repo / "conf/distributions")
            admin = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            public = ThreadingHTTPServer(("127.0.0.1", 0), entrypoint.RepositoryHandler)
            threads = [threading.Thread(target=server.serve_forever) for server in (admin, public)]
            with mock.patch.object(entrypoint, "REPO_ROOT", repo), mock.patch.object(app, "DATA", root / "data"):
                for thread in threads:
                    thread.start()
                try:
                    admin_port, public_port = admin.server_address[1], public.server_address[1]
                    self.assertEqual(request(admin_port, "/api/status")[0], 200)
                    for path, expected in {
                        "/": b"<h1>Repository</h1>", "/install.sh": b"#!/bin/sh\nexit 0\n",
                        "/repository.gpg": b"public-key", "/dists/stable/InRelease": b"signed metadata",
                        "/pool/main/demo.deb": b"package",
                    }.items():
                        with self.subTest(path=path):
                            self.assertEqual(request(public_port, path)[::2], (200, expected))
                            self.assertEqual(request(public_port, path, "HEAD")[::2], (200, b""))
                    for path in ("/api/status", "/settings", "/auth/callback", "/conf/distributions",
                                 "/db/version", "/.gnupg/private-keys-v1.d/key", "/../.gnupg/key",
                                 "/pool/../conf/distributions", "/pool/%2e%2e/conf/distributions",
                                 "/pool/main/linked.deb"):
                        with self.subTest(denied=path):
                            self.assertEqual(request(public_port, path)[0], 404)
                finally:
                    for server in (admin, public):
                        server.shutdown()
                    for thread in threads:
                        thread.join(2)
                    for server in (admin, public):
                        server.server_close()
                    self.assertFalse(any(thread.is_alive() for thread in threads))


class GeneratedAssetsTests(unittest.TestCase):
    def test_bootstrap_generation_regeneration_and_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, data = root / "repo", root / "data"
            first = bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main")
            self.assertIn("not configured", (repo / "index.html").read_text())
            self.assertIn("exit 1", (repo / "install.sh").read_text())
            self.assertTrue((repo / "install.sh").read_bytes().startswith(b"#!/bin/sh\n"))
            subprocess.run(["bash", "-n", str(repo / "install.sh")], check=True)
            inactive = subprocess.run([str(repo / "install.sh")], capture_output=True, text=True)
            self.assertNotEqual(inactive.returncode, 0)
            self.assertIn("not configured", inactive.stderr)
            second = bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main",
                                          public_url="https://repo.example.test/base/")
            self.assertEqual(second["fingerprint"], first["fingerprint"])
            self.assertIn("https://repo.example.test/base/install.sh", (repo / "index.html").read_text())
            script = (repo / "install.sh").read_text()
            self.assertIn("URIs: https://repo.example.test/base", script)
            self.assertIn(first["fingerprint"], script)
            subprocess.run(["bash", "-n", str(repo / "install.sh")], check=True)
            with repository_lease(repo, operation="test") as lease:
                _render_public_assets(lease.root_fd, public_url="http://repo.example.test", suite="stable",
                                      component="main", fingerprint=first["fingerprint"])
            self.assertIn("URIs: http://repo.example.test", (repo / "install.sh").read_text())
            prior_index = (repo / "index.html").read_bytes()
            (repo / "install.sh").write_text("operator script")
            with self.assertRaises(LocalRepositoryBootstrapError):
                bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main",
                                     public_url="https://different.example.test")
            self.assertEqual((repo / "index.html").read_bytes(), prior_index)
            (repo / "install.sh").unlink()
            with repository_lease(repo, operation="test") as lease:
                _render_public_assets(lease.root_fd, public_url="http://repo.example.test", suite="stable",
                                      component="main", fingerprint=first["fingerprint"])
            (repo / "index.html").write_text("operator file")
            with self.assertRaises(LocalRepositoryBootstrapError) as raised:
                bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main")
            self.assertEqual(raised.exception.code, "bootstrap_asset_conflict")
            self.assertEqual((repo / "index.html").read_text(), "operator file")
            (repo / "index.html").unlink()
            outside = root / "outside.html"
            outside.write_text("outside")
            (repo / "index.html").symlink_to(outside)
            with self.assertRaises(LocalRepositoryBootstrapError):
                bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main")
            self.assertEqual(outside.read_text(), "outside")


if __name__ == "__main__":
    unittest.main()
