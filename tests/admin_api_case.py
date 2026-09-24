"""Shared isolated HTTP fixture for DebBuilder API integration tests."""
from __future__ import annotations
from tests.lifecycle_helpers import stop_partial_manager

import json
import hashlib
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import debbuilder.app as server
from debbuilder.build_store import BuildStore


class AdminApiCase(unittest.TestCase):
    def assert_api_error(self, payload, *, code=None):
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertIs(payload["ok"], False)
        self.assertEqual(set(payload["error"]), {"code", "message", "details"})
        self.assertIsInstance(payload["error"]["code"], str)
        self.assertTrue(payload["error"]["code"])
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])
        self.assertIsInstance(payload["error"]["details"], dict)
        if code is not None:
            self.assertEqual(payload["error"]["code"], code)
        return payload["error"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        names = ("DATA", "USER_WORKFLOWS", "EXAMPLES", "STATIC", "REPOSITORY_ROOT", "AUTH_MODE", "UPSTREAM_OBSERVATION_SERVICE")
        self.old = {name: getattr(server, name) for name in names}
        server.DATA = base / "data"
        server.USER_WORKFLOWS = server.DATA / "workflows"
        server.EXAMPLES = base / "examples" / "recipes"
        server.STATIC = base / "static"
        server.REPOSITORY_ROOT = base / "repository"
        for directory in (server.DATA, server.USER_WORKFLOWS, server.EXAMPLES, server.STATIC, server.REPOSITORY_ROOT):
            directory.mkdir(parents=True, exist_ok=True)
        (server.STATIC / "index.html").write_text("DebBuilder")
        packages_dir = server.REPOSITORY_ROOT / "dists" / "stable" / "main" / "binary-amd64"
        packages_dir.mkdir(parents=True)
        rows = [
            {"Package": "webapp", "Version": "3.4.1", "Architecture": "all", "Homepage": None, "Filename": "pool/main/o/webapp/webapp_3.4.1_all.deb", "Depends": "npm, sqlite3, jq", "Description": "Description"},
            {"Package": "monitoring-app", "Version": "117", "Architecture": "all", "Homepage": None, "Filename": "pool/main/u/monitoring-app/monitoring-app_117_all.deb", "Depends": "npm, nodejs", "Description": "Description"},
        ]
        (packages_dir / "Packages").write_text("\n\n".join("\n".join(f"{key}: {value}" for key, value in row.items() if value is not None) for row in rows) + "\n\n")
        (server.EXAMPLES / "webapp-recipe.json").write_text(json.dumps({
            "schema_version": 5,
            "name": "webapp-recipe",
            "active": True,
            "package": {"name": "webapp", "architecture": "all"},
            "source": {"provider": "github", "repository": "example/webapp", "tracking": "latest_release", "version": {"source": "tag"}},
        }))
        seed_recipe = {
            "schema_version": 5,
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
        server.UPSTREAM_OBSERVATION_SERVICE = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.execution_manager = server.start_execution_manager(self.httpd)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        stop_partial_manager(self.httpd, timeout=5)
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

    def wait_for_run(self, run_id: str, *, statuses=("prepared", "success", "failed"), timeout=5):
        deadline = time.monotonic() + timeout
        store = BuildStore(server.DATA / "builds")
        while time.monotonic() < deadline:
            run = store.load(run_id)
            if run and run.get("status") in statuses:
                return run
            time.sleep(0.01)
        self.fail(f"Run {run_id} did not reach {statuses}")

    def terminal_executor(self, status: str):
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = status
                store.save(run)
            finished.set()
            return {"run_id": run_id, "status": status}

        return execute, finished

    def successful_build_run(self, run_id="auto-run", package="auto-package", version="1.0-1"):
        recipe = {
            "schema_version": 5,
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
            "artifact": {
                "path": str(artifact), "size": 3,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "inspection": {"package": package, "version": version, "architecture": "all"},
            },
        })
        store.save(run)
        return store, run, artifact
