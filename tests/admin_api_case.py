"""Shared isolated HTTP fixture for DebBuilder API integration tests."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import debbuilder.app as server
from debbuilder.build_store import BuildStore


class AdminApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        names = ("DATA", "USER_WORKFLOWS", "EXAMPLES", "STATIC", "AUTH_MODE", "GITHUB_RELEASE_CACHE_SERVICE")
        self.old = {name: getattr(server, name) for name in names}
        server.DATA = base / "data"
        server.USER_WORKFLOWS = server.DATA / "workflows"
        server.EXAMPLES = base / "examples" / "recipes"
        server.STATIC = base / "static"
        for directory in (server.DATA, server.USER_WORKFLOWS, server.EXAMPLES, server.STATIC):
            directory.mkdir(parents=True, exist_ok=True)
        (server.STATIC / "index.html").write_text("DebBuilder")
        (server.DATA / "repo-current-packages-inventory.json").write_text(json.dumps([
            {"Package": "webapp", "Version": "3.4.1", "Architecture": "all", "Homepage": None, "Filename": "pool/main/o/webapp/webapp_3.4.1_all.deb", "Depends": "npm, sqlite3, jq", "Description": "Description"},
            {"Package": "monitoring-app", "Version": "117", "Architecture": "all", "Homepage": None, "Filename": "pool/main/u/monitoring-app/monitoring-app_117_all.deb", "Depends": "npm, nodejs", "Description": "Description"},
        ]))
        (server.EXAMPLES / "webapp-recipe.json").write_text(json.dumps({
            "schema_version": 1,
            "name": "webapp-recipe",
            "active": True,
            "package": {"name": "webapp", "architecture": "all"},
            "source": {"provider": "github", "repository": "example/webapp", "tracking": "latest_release", "version": {"source": "tag"}},
        }))
        seed_recipe = {
            "schema_version": 1,
            "name": "webapp-recipe",
            "active": True,
            "package": {"name": "webapp", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Description"},
            "source": {"provider": "github", "repository": "example/webapp", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        store = BuildStore(server.DATA / "builds")
        run = store.create(seed_recipe, recipe_id="webapp-recipe", mode="build", run_id="20260822-031400")
        run.update({"status": "success", "version": {"upstream": "3.4.1", "debian": "3.4.1"}})
        store.save(run)
        store.append_event(run, "ok")
        server.AUTH_MODE = "none"
        server.GITHUB_RELEASE_CACHE_SERVICE = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        for name, value in self.old.items():
            setattr(server, name, value)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())

    def successful_build_run(self, run_id="auto-run", package="auto-package", version="1.0-1"):
        recipe = {
            "schema_version": 1,
            "name": f"{package}-recipe",
            "active": True,
            "package": {"name": package, "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo"},
            "source": {"provider": "github", "repository": f"owner/{package}", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        (server.USER_WORKFLOWS / f"{package}-recipe.json").write_text(json.dumps(recipe))
        store = BuildStore(server.DATA / "builds")
        run = store.create(recipe, recipe_id=f"{package}-recipe", mode="build", run_id=run_id)
        artifact = Path(run["workspace"]) / f"artifacts/{package}_{version}_all.deb"
        artifact.write_bytes(b"deb")
        run.update({
            "status": "success",
            "version": {"upstream": version.split("-")[0], "debian": version},
            "artifact": {"path": str(artifact), "size": 3, "sha256": run_id, "inspection": {"package": package, "version": version, "architecture": "all"}},
        })
        store.save(run)
        return store, run, artifact
