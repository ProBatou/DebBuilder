"""Production admin asset routing and cache contract."""
import http.client

import debbuilder.app as server
from tests.admin_api_case import AdminApiCase


class FrontendStaticTests(AdminApiCase):
    def get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_entry_assets_and_api_have_distinct_handlers(self):
        (server.STATIC / "index.html").write_text('<script src="./assets/index-ABCdef12.js"></script>')
        assets = server.STATIC / "assets"
        assets.mkdir()
        (assets / "index-ABCdef12.js").write_bytes(b"export {};\n")
        (assets / "index-ABCdef12.css").write_bytes(b"body{}\n")
        (assets / "plain.js").write_bytes(b"export {};\n")
        (assets / "icon-ABCdef12.png").write_bytes(b"\x89PNG\r\n")
        (assets / "icon-ABCdef12.webp").write_bytes(b"RIFF")
        (server.STATIC / "favicon.ico").write_bytes(b"\0\1")
        (server.STATIC / "site.webmanifest").write_text("{}")
        (server.STATIC / "icon.svg").write_text("<svg/>")
        (server.STATIC / "data.json").write_text("{}")

        for path, mime, cache in (
            ("/", "text/html", "no-cache"),
            ("/index.html", "text/html", "no-cache"),
            ("/assets/index-ABCdef12.js", "text/javascript", "immutable"),
            ("/assets/index-ABCdef12.css", "text/css", "immutable"),
            ("/assets/plain.js", "text/javascript", "no-cache"),
            ("/assets/icon-ABCdef12.png", "image/png", "immutable"),
            ("/assets/icon-ABCdef12.webp", "image/webp", "immutable"),
            ("/favicon.ico", "image/vnd.microsoft.icon", "no-cache"),
            ("/site.webmanifest", "application/manifest+json", "no-cache"),
            ("/icon.svg", "image/svg+xml", "no-cache"),
            ("/data.json", "application/json", "no-cache"),
        ):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertIn(cache, headers["Cache-Control"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertTrue(body)

        for path in ("/assets/missing.js", "/unknown", "/api/missing", "/auth/unknown"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"<script", body)

        for path in ("/api/status", "/api/auth/status"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertNotIn(b"<script", body)
        status, _, body = self.get("/auth/callback")
        self.assertEqual(status, 400)
        self.assertNotIn(b"<script", body)

    def test_encoded_traversal_and_symlink_cannot_escape_asset_root(self):
        secret = server.STATIC.parent / "secret.txt"
        secret.write_text("outside asset root")
        (server.STATIC / "escape.txt").symlink_to(secret)
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/escape.txt", "/assets/../escape.txt"):
            with self.subTest(path=path):
                status, _, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(secret.read_bytes(), body)

    def test_admin_auth_and_public_repository_isolation(self):
        server.AUTH_MODE = "header"
        self.assertEqual(self.get("/")[0], 401)
        self.assertEqual(self.get("/", {"X-Forwarded-User": "operator"})[0], 200)
        for path in ("/install.sh", "/dists/stable/Release", "/pool/main/p/pkg.deb"):
            self.assertEqual(self.get(path, {"X-Forwarded-User": "operator"})[0], 404)
