import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import http.client
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import debbuilder.app as server
from debbuilder.build_store import BuildStore


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.old = {name: getattr(server, name) for name in ["DATA", "USER_WORKFLOWS", "EXAMPLES", "RUNS", "AUTH_MODE"]}
        server.DATA = base / "data"
        server.USER_WORKFLOWS = server.DATA / "workflows"
        server.EXAMPLES = base / "examples" / "recipes"
        server.RUNS = server.DATA / "runs"
        for d in (server.DATA, server.USER_WORKFLOWS, server.EXAMPLES, server.RUNS):
            d.mkdir(parents=True, exist_ok=True)
        (server.DATA / "repo-current-packages-inventory.json").write_text(json.dumps([
            {"Package": "webapp", "Version": "3.4.1", "Architecture": "all", "Homepage": None, "Filename": "pool/main/o/webapp/webapp_3.4.1_all.deb", "Depends": "npm, sqlite3, jq", "Description": "Description"},
            {"Package": "monitoring-app", "Version": "117", "Architecture": "all", "Homepage": None, "Filename": "pool/main/u/monitoring-app/monitoring-app_117_all.deb", "Depends": "npm, nodejs", "Description": "Description"},
        ]))
        (server.EXAMPLES / "webapp-recipe.json").write_text(json.dumps({
            "name": "webapp-recipe",
            "steps": [{"type": "init_deb_package", "package": "webapp", "version": "3.4.1", "architecture": "all"}]
        }))
        (server.RUNS / "20260822-031400.sh").write_text("#!/bin/bash\n")
        (server.RUNS / "20260822-031400.out").write_text("ok\n")
        (server.DATA / "executions.json").write_text(json.dumps([{"id":"20260822-031400","package":"webapp","action":"build","status":"success","updated":1}]))
        server.AUTH_MODE = "none"
        server.GITHUB_RELEASE_CACHE.clear()
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
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def successful_build_run(self, run_id="auto-run", package="auto-package", version="1.0-1"):
        recipe = {
            "schema_version": 1, "name": f"{package}-recipe", "active": True,
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

    def test_get_package_list_seeded_from_inventory_and_recipe_association(self):
        status, data = self.request("GET", "/api/packages")
        self.assertEqual(status, 200)
        names = [p["name"] for p in data["packages"]]
        self.assertEqual(names, ["monitoring-app", "webapp"])
        webapp = next(p for p in data["packages"] if p["name"] == "webapp")
        self.assertEqual(webapp["apt_version"], "3.4.1")
        self.assertEqual(webapp["recipe"], "webapp-recipe")
        self.assertEqual(webapp["status"], "ready")
        self.assertEqual(webapp["version"]["published"], "3.4.1")
        self.assertEqual(webapp["lifecycle_state"], "unknown")
        self.assertIn("build", webapp)
        self.assertIn("repository", webapp)
        self.assertFalse(webapp["recipe_complete"] if "recipe_complete" in webapp else False)

    def test_dashboard_counts_lifecycle_states_from_same_package_rows(self):
        server.save_json_file(server.packages_file(), [
            {"name":"github-demo","apt_version":"1.0","upstream_version":"2.0","recipe":"webapp-recipe","source":{"type":"github","repository":"o/r"}},
            {"name":"local-demo","apt_version":"1.0-1","upstream_version":"1.0","recipe":"webapp-recipe","source":{"type":"local"}},
        ])
        summary = server.dashboard_summary()
        self.assertEqual(summary["packages"], 4)
        self.assertEqual(summary["updates"], 1)
        self.assertEqual(summary["state_counts"]["update_available"], 1)
        self.assertEqual(summary["packages_by_state"]["update_available"][0]["name"], "github-demo")
        self.assertIn("local-demo", [row["name"] for row in summary["packages_by_state"]["up_to_date"]])

    def test_dashboard_reuses_package_lifecycle_state_without_recomparing_versions(self):
        packages = [
            {"name": "same", "lifecycle_state": "up_to_date", "version": {"source": "3.4.1", "published": "3.4.1-2"}},
            {"name": "new", "lifecycle_state": "update_available", "version": {"source": "3.4.2", "published": "3.4.1-2"}},
            {"name": "pending", "lifecycle_state": "publication_available", "lifecycle_display_status": "validation_needed", "version": {"source": "3.4.3", "published": "3.4.1-2"}},
        ]
        with mock.patch("debbuilder.app.list_packages", return_value=packages), mock.patch("debbuilder.app.list_executions", return_value=[]):
            summary = server.dashboard_summary()
        self.assertEqual(summary["state_counts"], {"up_to_date": 1, "update_available": 1, "validation_needed": 1})
        self.assertEqual(summary["packages_by_state"]["up_to_date"][0]["name"], "same")
        self.assertEqual(summary["packages_by_state"]["validation_needed"][0]["name"], "pending")

    def test_package_list_prefers_live_apt_repository_versions_when_available(self):
        (server.DATA / "settings.json").write_text(json.dumps({"apt": {"repository": "https://repo.example.test", "distribution": "testing", "component": "main", "architecture": "amd64"}}))
        with mock.patch("debbuilder.app.apt_repo.fetch_packages_index", return_value=[
            {"Package": "webapp", "Version": "3.4.2", "Architecture": "all", "Filename": "pool/main/o/webapp/webapp_3.4.2_all.deb"},
            {"Package": "monitoring-app", "Version": "117", "Architecture": "all", "Filename": "pool/main/u/monitoring-app/monitoring-app_117_all.deb"},
        ]):
            status, data = self.request("GET", "/api/packages")
        self.assertEqual(status, 200)
        webapp = next(p for p in data["packages"] if p["name"] == "webapp")
        self.assertEqual(webapp["apt_version"], "3.4.2")
        self.assertEqual(webapp["version"]["published"], "3.4.2")

    def test_package_aggregate_rebuilds_from_recipe_run_and_apt_after_restart(self):
        recipe = {
            "schema_version": 1, "name": "demo-recipe", "active": True,
            "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Real demo", "runtime_dependencies": ["curl"]},
            "source": {"provider": "github", "repository": "owner/demo", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        (server.USER_WORKFLOWS / "demo-recipe.json").write_text(json.dumps(recipe))
        store = BuildStore(server.DATA / "builds")
        run = store.create(recipe, recipe_id="demo-recipe", mode="build", run_id="structured-run")
        artifact = Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "version": {"upstream": "2.0", "debian": "2.0-1"}, "artifact": {"path": str(artifact), "size": 3, "sha256": "abc"}})
        source = next(step for step in run["steps"] if step["name"] == "source")
        source["details"] = {"repository": "owner/demo", "ref": "v2.0", "tag": "v2.0", "release_url": "https://github.test/v2.0"}
        run["validations"] = [{"id": "validation", "artifact": str(artifact), "status": "success", "finished_at": "2026-01-01T00:00:00+00:00"}]
        run["publications"] = [{"id": "publication", "status": "success", "published_version": "2.0-1", "finished_at": "2026-01-01T00:01:00+00:00"}]
        store.save(run)
        published = [{"Package": "demo", "Version": "2.0-1", "Architecture": "all", "Filename": "pool/main/d/demo.deb"}]
        with mock.patch("debbuilder.app.live_published_index", return_value=published):
            first = server.get_package("demo")
            server.GITHUB_RELEASE_CACHE.clear()
            second = server.get_package("demo")
        for package in (first, second):
            self.assertEqual(package["recipe"], "demo-recipe")
            self.assertEqual(package["version"], {"source": "2.0", "debian": "2.0-1", "published": "2.0-1", "candidate": "2.0-1", "strategy": "github_tag"})
            self.assertEqual(package["build"]["last_build_id"], "structured-run")
            self.assertEqual(package["validation"]["status"], "success")
            self.assertEqual(package["publication"]["status"], "success")
            self.assertTrue(package["history"])
            self.assertEqual(package["lifecycle_state"], "up_to_date")

    def test_package_lifecycle_tracks_latest_real_run_without_hiding_repository_version(self):
        recipe = {
            "schema_version": 1, "name": "debbuilder-recipe", "active": True,
            "package": {"name": "debbuilder", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo"},
            "source": {"provider": "github", "repository": "owner/debbuilder", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        (server.USER_WORKFLOWS / "debbuilder-recipe.json").write_text(json.dumps(recipe))
        store = BuildStore(server.DATA / "builds")

        def build_run(run_id, version, created_at):
            run = store.create(recipe, recipe_id="debbuilder-recipe", mode="build", run_id=run_id)
            artifact = Path(run["workspace"]) / f"artifacts/debbuilder_{version}_all.deb"
            artifact.write_bytes(b"deb")
            run.update({
                "status": "success", "created_at": created_at, "created_at_epoch": 1,
                "version": {"upstream": version.split("-")[0], "debian": version},
                "artifact": {"path": str(artifact), "size": 3, "sha256": run_id, "inspection": {"package": "debbuilder", "version": version, "architecture": "all"}},
            })
            store.save(run)
            return run, artifact

        old, old_artifact = build_run("old-published", "0.1.3-2", "2026-01-01T00:00:00+00:00")
        old["validations"] = [{"id": "old-validation", "artifact": str(old_artifact), "status": "success"}]
        old["publications"] = [{"id": "old-publication", "status": "success", "published_version": "0.1.3-2"}]
        store.save(old)
        current, current_artifact = build_run("current-build", "0.1.4-2", "2026-01-02T00:00:00+00:00")

        published_old = [{"Package": "debbuilder", "Version": "0.1.3-2", "Architecture": "all", "Filename": "pool/debbuilder_0.1.3-2_all.deb"}]
        with mock.patch("debbuilder.app.live_published_index", return_value=published_old):
            package = server.get_package("debbuilder")
        self.assertEqual(package["lifecycle_display_status"], "validation_needed")
        self.assertEqual(package["version"]["published"], "0.1.3-2")
        self.assertEqual(package["version"]["candidate"], "0.1.4-2")
        self.assertEqual(package["build"]["latest_run_id"], "current-build")
        self.assertIsNone(package["validation"])
        self.assertIsNone(package["publication"])

        current["validations"] = [{"id": "current-validation", "artifact": str(current_artifact), "status": "success"}]
        store.save(current)
        with mock.patch("debbuilder.app.live_published_index", return_value=published_old):
            package = server.get_package("debbuilder")
        self.assertEqual(package["lifecycle_display_status"], "ready_to_publish")
        self.assertTrue(package["build"]["ready_to_publish"])

        current["publications"] = [{"id": "current-publication", "status": "success", "published_version": "0.1.4-2"}]
        store.save(current)
        published_current = [{**published_old[0], "Version": "0.1.4-2"}]
        with mock.patch("debbuilder.app.live_published_index", return_value=published_current):
            package = server.get_package("debbuilder")
        self.assertEqual(package["lifecycle_display_status"], "published")
        self.assertEqual(package["version"]["published"], "0.1.4-2")

        failed = store.create(recipe, recipe_id="debbuilder-recipe", mode="build", run_id="latest-failure")
        failed.update({"status": "failed", "created_at": "2026-01-03T00:00:00+00:00", "created_at_epoch": 2, "version": {"upstream": "0.1.5", "debian": "0.1.5-1"}})
        store.save(failed)
        with mock.patch("debbuilder.app.live_published_index", return_value=published_current):
            package = server.get_package("debbuilder")
        self.assertEqual(package["lifecycle_display_status"], "build_failed")
        self.assertEqual(package["version"]["published"], "0.1.4-2")
        self.assertEqual(package["build"]["latest_run_id"], "latest-failure")
        self.assertEqual(package["build"]["latest_status"], "failed")
        self.assertIsNone(package["validation"])
        self.assertIsNone(package["publication"])

    def test_failed_dry_run_keeps_package_without_pending_real_run_up_to_date(self):
        recipe = {
            "schema_version": 1, "name": "stable-recipe", "active": True,
            "package": {"name": "stable", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Stable"},
            "source": {"provider": "github", "repository": "owner/stable", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        (server.USER_WORKFLOWS / "stable-recipe.json").write_text(json.dumps(recipe))
        store = BuildStore(server.DATA / "builds")
        dry = store.create(recipe, recipe_id="stable-recipe", mode="dry_run", run_id="failed-dry-run")
        dry.update({"status": "failed", "version": {"upstream": "1.0.0", "debian": "1.0.0-1"}})
        store.save(dry)
        published = [{"Package": "stable", "Version": "1.0.0-1", "Architecture": "all", "Filename": "pool/stable_1.0.0-1_all.deb"}]
        with mock.patch("debbuilder.app.live_published_index", return_value=published):
            package = server.get_package("stable")
        self.assertEqual(package["lifecycle_display_status"], "up_to_date")
        self.assertEqual(package["version"]["published"], "1.0.0-1")
        self.assertIsNone(package["build"]["latest_run"])

    def test_get_package_detail_and_missing_package(self):
        status, data = self.request("GET", "/api/packages/webapp")
        self.assertEqual(status, 200)
        self.assertEqual(data["package"]["name"], "webapp")
        self.assertEqual(data["package"]["history"][0]["id"], "20260822-031400")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("GET", "/api/packages/missing")
        self.assertEqual(ctx.exception.code, 404)

    def test_create_update_delete_package_refuses_repo_delete_without_confirmation(self):
        status, created = self.request("POST", "/api/packages", {"name": "download-ui", "architecture": "amd64", "source": {"type": "github", "repository": "example/download-ui"}})
        self.assertEqual(status, 200)
        self.assertEqual(created["package"]["status"], "recipe_missing")
        status, updated = self.request("POST", "/api/packages/download-ui", {"description": "Download UI package", "status": "unknown"})
        self.assertEqual(updated["package"]["description"], "Download UI package")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("DELETE", "/api/packages/download-ui?delete_repo=1")
        self.assertEqual(ctx.exception.code, 403)
        status, deleted = self.request("DELETE", "/api/packages/download-ui")
        self.assertTrue(deleted["ok"])

    def test_recipe_list_logs_execution_detail_and_settings_are_available(self):
        status, recipes = self.request("GET", "/api/recipes")
        self.assertEqual(recipes["recipes"][0]["id"], "webapp-recipe")
        status, executions = self.request("GET", "/api/executions")
        self.assertEqual(executions["executions"][0]["id"], "20260822-031400")
        status, detail = self.request("GET", "/api/executions/20260822-031400")
        self.assertIn("ok", detail["execution"]["log"])
        status, settings = self.request("GET", "/api/settings")
        self.assertNotIn("build", settings["settings"])
        self.assertEqual(settings["settings"]["github"]["token"], "masked")

    def test_execution_list_does_not_include_staging_inventory_or_manifest_contents(self):
        store = BuildStore(server.DATA / "builds")
        run = store.create({
            "name": "large", "package_name": "large", "github_repository": "example/large", "active": True,
            "package": {"name": "large", "maintainer": "Test <test@example.test>", "description": "Large"},
        }, mode="build", run_id="large-run")
        files = [f"node_modules/{index}.js" for index in range(60_000)]
        details = store.staging_details_for_storage(run, {"content_available": True, "content_files": files})
        run["steps"][5].update({"status": "success", "summary": "Staging prepared with 60,000 application files", "details": details})
        store.save(run)
        _, response = self.request("GET", "/api/executions")
        encoded = json.dumps(response)
        self.assertNotIn("content_files", encoded)
        self.assertNotIn("staging-files.json", encoded)
        self.assertLess(len(encoded), 10_000)

    def test_execution_logs_are_separate_incremental_and_verbose_selectable(self):
        store = BuildStore(server.DATA / "builds")
        run = store.create({
            "name": "logs", "package_name": "logs", "github_repository": "example/logs", "active": True,
            "package": {"name": "logs", "maintainer": "Test <test@example.test>", "description": "Logs"},
        }, mode="build", run_id="live-run")
        run["status"] = "running"
        run["steps"][0].update({"status": "success", "summary": "source ok"})
        run["steps"][4].update({"status": "running", "summary": "command running", "details": {"commands": [{"index": 1, "status": "running", "working_directory": "/tmp/source", "duration": 1, "stdout": "Compiling one", "stderr": ""}]}})
        store.save(run)
        store.append_log_line("live-run", "Build command 1 stdout: Compiling one")
        status, compact = self.request("GET", "/api/executions/live-run/logs?verbosity=compact")
        self.assertEqual(status, 200)
        self.assertIn("source: success", compact["log"]["text"])
        self.assertIn("build: running", compact["log"]["text"])
        self.assertNotIn("Compiling one", compact["log"]["text"])
        status, verbose = self.request("GET", "/api/executions/live-run/logs?verbosity=verbose")
        self.assertIn("Compiling one", verbose["log"]["text"])
        status, raw = self.request("GET", "/api/executions/live-run/logs?verbosity=raw&after=0")
        offset = raw["log"]["offset"]
        store.append_log_line("live-run", "Build command 1 stdout: Compiling two")
        status, tail = self.request("GET", f"/api/executions/live-run/logs?verbosity=raw&after={offset}")
        self.assertEqual(status, 200)
        self.assertIn("Compiling two", tail["log"]["text"])
        self.assertNotIn("Compiling one", tail["log"]["text"])

    def test_delete_execution_log_preserves_package_lifecycle_and_artifact(self):
        store = BuildStore(server.DATA / "builds")
        run = store.create({
            "name": "cleanup", "package_name": "cleanup", "github_repository": "example/cleanup", "active": True,
            "package": {"name": "cleanup", "maintainer": "Test <test@example.test>", "description": "Cleanup"},
        }, mode="build", run_id="cleanup-run")
        artifact = Path(run["workspace"]) / "artifacts/cleanup.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc", "inspection": {"package": "cleanup", "version": "1.0-1", "architecture": "all"}}, "validations": [{"status": "success", "artifact": str(artifact)}]})
        run["steps"][4]["details"] = {"commands": [{"index": 1, "stdout": "long output", "stderr": ""}]}
        store.save(run)
        store.append_log_line("cleanup-run", "long output")
        status, deletion = self.request("DELETE", "/api/executions/cleanup-run/logs")
        self.assertEqual(status, 200)
        self.assertEqual(deletion["deletion"]["deleted"], "log_history")
        cleaned = store.load("cleanup-run")
        self.assertTrue(artifact.exists())
        self.assertEqual(cleaned["status"], "success")
        self.assertEqual(cleaned["artifact"]["path"], str(artifact))
        self.assertEqual(cleaned["steps"][4]["details"]["commands"][0]["stdout"], "")
        package = server.get_package("cleanup")
        self.assertEqual(package["build"]["latest_run_id"], "cleanup-run")
        self.assertEqual(package["lifecycle_display_status"], "ready_to_publish")

    def test_clear_all_execution_logs_preserves_lifecycle_and_artifacts(self):
        store = BuildStore(server.DATA / "builds")
        for run_id in ("batch-one", "batch-two"):
            run = store.create({"name": run_id, "package_name": run_id, "github_repository": f"example/{run_id}", "active": True}, mode="dry_run", run_id=run_id)
            run["status"] = "prepared"
            store.save(run)
            store.append_log_line(run_id, "temporary detail")
        run = store.create({
            "name": "global-cleanup", "package_name": "global-cleanup", "github_repository": "example/global-cleanup", "active": True,
            "package": {"name": "global-cleanup", "maintainer": "Test <test@example.test>", "description": "Cleanup"},
        }, mode="build", run_id="global-cleanup-run")
        artifact = Path(run["workspace"]) / "artifacts/global-cleanup.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc", "inspection": {"package": "global-cleanup", "version": "1.0-1", "architecture": "all"}}, "validations": [{"status": "success", "artifact": str(artifact)}]})
        run["steps"][4]["details"] = {"commands": [{"index": 1, "stdout": "long output", "stderr": ""}]}
        store.save(run)
        store.append_log_line("global-cleanup-run", "temporary detail")
        status, preview = self.request("POST", "/api/executions/delete-logs", {"all": True, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(preview["count"], 3)
        self.assertIn("global-cleanup-run", preview["ids"])
        status, result = self.request("POST", "/api/executions/delete-logs", {"all": True})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["deleted"]), 3)
        self.assertEqual(result["errors"], [])
        self.assertTrue(store.load("batch-one")["log_deleted"])
        cleaned = store.load("global-cleanup-run")
        self.assertTrue(cleaned["log_deleted"])
        self.assertTrue(artifact.exists())
        self.assertEqual(cleaned["artifact"]["path"], str(artifact))
        self.assertEqual(cleaned["validations"][0]["status"], "success")
        self.assertEqual(server.get_package("global-cleanup")["lifecycle_display_status"], "ready_to_publish")

    def test_repo_settings_can_be_updated_and_are_persisted(self):
        body = {
            "apt": {
                "repository": "https://repo.example.test",
                "distribution": "testing",
                "component": "contrib",
                "architecture": "arm64",
            }
        }
        status, updated = self.request("POST", "/api/settings", body)
        self.assertEqual(status, 200)
        self.assertEqual(updated["settings"]["apt"]["repository"], "https://repo.example.test")
        self.assertEqual(updated["settings"]["apt"]["distribution"], "testing")
        self.assertEqual(updated["settings"]["apt"]["component"], "contrib")
        self.assertEqual(updated["settings"]["apt"]["architecture"], "arm64")

        status, loaded = self.request("GET", "/api/settings")
        self.assertEqual(loaded["settings"]["apt"], updated["settings"]["apt"])
        settings_path = server.DATA / "settings.json"
        self.assertTrue(settings_path.exists())
        saved = json.loads(settings_path.read_text())
        self.assertEqual(saved["apt"]["repository"], "https://repo.example.test")

    def test_all_safe_settings_sections_can_be_updated(self):
        body = {
            "general": {"app_name": "Package Console", "url": "https://console.example.test"},
            "apt": {"repository": "https://repo.example.test", "distribution": "testing", "component": "main", "architecture": "amd64"},
            "github": {"api_url": "https://api.github.com"},
            "notifications": {"type": "ntfy", "server_url": "https://ntfy.example.test", "topic": "debbuilder"},
            "automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True},
        }
        status, updated = self.request("POST", "/api/settings", body)
        self.assertEqual(status, 200)
        settings = updated["settings"]
        self.assertEqual(settings["general"]["app_name"], "Package Console")
        self.assertEqual(settings["general"]["url"], "https://console.example.test")
        self.assertEqual(settings["github"]["api_url"], "https://api.github.com")
        self.assertEqual(settings["github"]["token"], "masked")
        self.assertFalse(settings["github"]["token_configured"])
        self.assertTrue(settings["notifications"]["configured"])
        self.assertEqual(settings["notifications"]["type"], "ntfy")
        self.assertTrue(settings["automation"]["auto_validate_after_successful_build"])
        self.assertTrue(settings["automation"]["auto_publish_after_successful_validation"])

    def test_auto_publish_setting_enables_auto_validate_backend_constraint(self):
        status, updated = self.request("POST", "/api/settings", {
            "automation": {"auto_validate_after_successful_build": False, "auto_publish_after_successful_validation": True}
        })
        self.assertEqual(status, 200)
        self.assertTrue(updated["settings"]["automation"]["auto_validate_after_successful_build"])
        self.assertTrue(updated["settings"]["automation"]["auto_publish_after_successful_validation"])
        saved = json.loads((server.DATA / "settings.json").read_text())
        self.assertTrue(saved["automation"]["auto_validate_after_successful_build"])
        self.assertTrue(saved["automation"]["auto_publish_after_successful_validation"])

    def test_disabling_auto_validate_also_disables_auto_publish_backend_constraint(self):
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True}})
        status, updated = self.request("POST", "/api/settings", {
            "automation": {"auto_validate_after_successful_build": False}
        })
        self.assertEqual(status, 200)
        self.assertFalse(updated["settings"]["automation"]["auto_validate_after_successful_build"])
        self.assertFalse(updated["settings"]["automation"]["auto_publish_after_successful_validation"])

    def test_github_token_update_stays_server_side(self):
        body = {"github": {"api_url": "https://api.github.com", "token": "ghlocalvalue12345678901234567890"}}
        status, updated = self.request("POST", "/api/settings", body)
        self.assertEqual(status, 200)
        github = updated["settings"]["github"]
        self.assertEqual(github["token"], "masked")
        self.assertTrue(github["token_configured"])
        self.assertNotIn("ghlocalvalue", json.dumps(updated))
        secrets_path = Path(self.tmp.name) / "data" / "secrets.json"
        self.assertTrue(secrets_path.exists())
        self.assertIn("ghlocalvalue12345678901234567890", secrets_path.read_text())

    def test_notification_settings_reject_unknown_types(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/settings", {"notifications": {"type": "webhook"}})
        self.assertEqual(ctx.exception.code, 400)

    def test_disabled_ntfy_accepts_an_empty_configuration(self):
        status, updated = self.request("POST", "/api/settings", {
            "notifications": {"type": "none", "server_url": "", "topic": ""},
        })
        self.assertEqual(status, 200)
        notifications = updated["settings"]["notifications"]
        self.assertFalse(notifications["configured"])
        self.assertEqual(notifications["server_url"], "")
        self.assertEqual(notifications["topic"], "")

    def test_repo_settings_reject_secret_like_repository_values(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/settings", {"apt": {"repository": "https://example.invalid/?token=abc12345"}})
        self.assertEqual(ctx.exception.code, 400)

    def test_recipe_metadata_can_be_created_loaded_and_renamed(self):
        workflow = {"name":"flood","package_name":"flood","github_repository":"jesec/flood","version_tracking":"latest_release","active":True,"steps":[{"type":"github_release_available"}]}
        status, _ = self.request("POST", "/api/workflows/flood", {"workflow": workflow})
        self.assertEqual(status, 200)
        status, loaded = self.request("GET", "/api/workflows/flood")
        self.assertEqual(loaded["package_name"], "flood")
        self.assertEqual(loaded["github_repository"], "jesec/flood")
        loaded["name"] = "flood-release"
        status, _ = self.request("POST", "/api/workflows/flood-release", {"workflow": loaded, "previous_id":"flood"})
        self.assertEqual(status, 200)
        self.assertFalse((server.USER_WORKFLOWS / "flood.json").exists())
        self.assertTrue((server.USER_WORKFLOWS / "flood-release.json").exists())

    def test_user_recipe_can_be_deleted_without_repository_or_system_deletion(self):
        workflow = {"name":"temporary","package_name":"temporary","github_repository":"example/temporary","version_tracking":"latest_release","active":True,"steps":[]}
        status, _ = self.request("POST", "/api/workflows/temporary", {"workflow": workflow})
        self.assertEqual(status, 200)
        status, listed = self.request("GET", "/api/workflows")
        self.assertEqual(status, 200)
        self.assertIn("temporary", [row["id"] for row in listed["workflows"]])
        status, loaded = self.request("GET", "/api/workflows/temporary")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["name"], "temporary")
        self.assertTrue((server.USER_WORKFLOWS / "temporary.json").exists())
        status, deleted = self.request("DELETE", "/api/workflows/temporary")
        self.assertEqual(status, 200)
        self.assertEqual(deleted, {"ok": True, "id": "temporary", "deleted_from_repository": False})
        self.assertFalse((server.USER_WORKFLOWS / "temporary.json").exists())
        status, listed = self.request("GET", "/api/workflows")
        self.assertEqual(status, 200)
        self.assertNotIn("temporary", [row["id"] for row in listed["workflows"]])
        packages = server.load_package_overrides()
        self.assertNotEqual(packages["temporary"].get("recipe"), "temporary")

    def test_recipe_package_can_select_inventory_item_or_create_new_item(self):
        existing = {"name":"webapp-build","package_name":"webapp","github_repository":"example/webapp","version_tracking":"latest_release","active":True,"steps":[]}
        status, _ = self.request("POST", "/api/workflows/webapp-build", {"workflow": existing})
        self.assertEqual(status, 200)
        with mock.patch("debbuilder.app.github_client.latest_release", return_value={"tag":"v3.5.0","name":"3.5.0","url":"https://github.example.test/release","archive_url":"https://github.example.test/archive","assets":[]}):
            package = server.get_package("webapp")
            self.assertEqual(package["recipe"], "")
            self.assertEqual(package["recipe_error"]["code"], "ambiguous_recipe")
            self.assertEqual(set(package["recipe_error"]["candidates"]), {"webapp-recipe", "webapp-build"})
        new = {"name":"new-app","package_name":"new-app","github_repository":"example/new-app","version_tracking":"latest_release","active":True,"steps":[]}
        status, _ = self.request("POST", "/api/workflows/new-app", {"workflow": new})
        self.assertEqual(status, 200)
        created = server.get_package("new-app")
        self.assertEqual(created["recipe"], "new-app")
        self.assertEqual(created["source"]["repository"], "example/new-app")

    def test_recipe_created_from_package_stays_linked_after_canonical_reload(self):
        status, created = self.request("POST", "/api/packages", {"name": "nested-app", "architecture": "amd64", "source": {"type": "github", "repository": "example/nested-app"}})
        self.assertEqual(status, 200)
        workflow = {
            "schema_version": 1, "name": "nested-app", "active": True,
            "package": {"name": "nested-app", "architecture": "amd64"},
            "source": {"provider": "github", "repository": "example/nested-app", "tracking": "latest_release", "version": {"source": "tag"}},
            "build": {"commands": [], "output": {"mode": "source"}},
        }
        status, _ = self.request("POST", "/api/workflows/nested-app", {"workflow": workflow})
        self.assertEqual(status, 200)
        stored = json.loads((server.USER_WORKFLOWS / "nested-app.json").read_text())
        self.assertNotIn("package_name", stored)
        self.assertNotIn("github_repository", stored)
        package = server.get_package("nested-app")
        self.assertEqual(package["recipe"], "nested-app")
        self.assertEqual(package["source"]["repository"], "example/nested-app")
        status, data = self.request("GET", "/api/packages")
        self.assertEqual(status, 200)
        listed = next(row for row in data["packages"] if row["name"] == "nested-app")
        self.assertEqual(listed["recipe"], "nested-app")
        self.assertNotIn("recipe_error", listed)

    def test_published_inventory_package_is_enriched_from_matching_recipe_metadata(self):
        inventory = json.loads(server.inventory_file().read_text())
        inventory.append({
            "Package": "flood", "Version": "4.8.2-0", "Architecture": "amd64",
            "Homepage": None, "Filename": "pool/main/f/flood/flood_4.8.2-0_amd64.deb",
            "Description": "Flood",
        })
        server.inventory_file().write_text(json.dumps(inventory))
        server.save_json_file(server.packages_file(), [{
            "name": "flood", "apt_version": "4.8.2-0", "upstream_version": "4.8.2-0",
            "source": {"type": "apt-repository", "repository": ""}, "recipe": "flood",
        }])
        (server.USER_WORKFLOWS / "flood.json").write_text(json.dumps({
            "name": "flood", "package_name": "Flood", "github_repository": "jesec/flood",
            "version_tracking": "latest_release", "version_source": "tag", "active": True, "steps": [],
        }))
        release = {"tag":"v4.9.0","name":"Flood 4.9.0","url":"https://github.example.test/flood/4.9.0","archive_url":"https://github.example.test/flood/archive","assets":[]}
        with mock.patch.dict(server.GITHUB_RELEASE_CACHE, {"jesec/flood": (time.time() + 300, release)}, clear=False):
            status, data = self.request("GET", "/api/packages")
        self.assertEqual(status, 200)
        flood = next(package for package in data["packages"] if package["name"] == "flood")
        self.assertEqual(flood["source"]["type"], "github")
        self.assertEqual(flood["source"]["repository"], "jesec/flood")
        self.assertEqual(flood["source"]["latest_release"], "v4.9.0")
        self.assertEqual(flood["version"]["published"], "4.8.2-0")
        self.assertEqual(flood["version"]["source"], "4.9.0")
        self.assertEqual(flood["recipe"], "flood")
        self.assertEqual(flood["lifecycle_state"], "update_available")

    def test_empty_recipe_create_load_duplicate_and_save_metadata(self):
        workflow = {"name":"empty","package_name":"empty","github_repository":"example/empty","version_tracking":"latest_release","version_source":"tag","active":True,"steps":[]}
        self.request("POST", "/api/workflows/empty", {"workflow": workflow})
        _, loaded = self.request("GET", "/api/workflows/empty")
        self.assertEqual(loaded["steps"], [])
        duplicate = {**loaded, "name":"empty-copy", "package_name":"empty-copy", "github_repository":"example/empty-copy"}
        self.request("POST", "/api/workflows/empty-copy", {"workflow": duplicate})
        _, copied = self.request("GET", "/api/workflows/empty-copy")
        self.assertEqual(copied["steps"], [])
        self.assertEqual(copied["version_source"], "tag")

    def test_recipe_v1_is_stored_canonically_and_loaded_with_full_sections(self):
        recipe = {
            "schema_version": 1, "name": "v1-demo", "active": True,
            "package": {"name": "v1-demo", "architecture": "all", "runtime_dependencies": ["python3"]},
            "source": {"provider": "github", "repository": "example/v1-demo", "tracking": "latest_release", "version": {"source": "tag"}},
            "build": {"extra_dependencies": ["python3-dev"], "commands": ["python3 -m build"], "working_directory": ".", "output": {"mode": "path", "path": "dist"}},
            "install": {"destination": "/opt/v1-demo", "owner": {"user": "root", "group": "root"}, "config_policy": "replace", "config_files": [{"source": "demo.conf", "destination": "/etc/v1-demo.conf"}]},
            "service": {"configured": True, "name": "v1-demo.service", "user": "v1-demo", "group": "v1-demo", "command": "/usr/bin/python3 /opt/v1-demo/server.py"},
        }
        status, _ = self.request("POST", "/api/workflows/v1-demo", {"workflow": recipe})
        self.assertEqual(status, 200)
        stored = json.loads((server.USER_WORKFLOWS / "v1-demo.json").read_text())
        self.assertEqual(stored["schema_version"], 1)
        self.assertNotIn("package_name", stored)
        self.assertNotIn("github_repository", stored)
        self.assertNotIn("config_policy", stored["install"])
        self.assertEqual(stored["install"]["config_files"][0]["policy"], "replace")
        self.assertNotIn("configured", stored["service"])
        _, loaded = self.request("GET", "/api/workflows/v1-demo")
        self.assertEqual(loaded["build"]["extra_dependencies"], ["python3-dev"])
        self.assertEqual(loaded["install"]["owner"]["user"], "root")
        self.assertEqual(loaded["service"]["user"], "v1-demo")
        self.assertTrue(loaded["service"]["configured"])

    def test_readonly_recipe_cannot_be_deleted(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("DELETE", "/api/workflows/webapp-recipe")
        self.assertEqual(ctx.exception.code, 403)
        self.assertTrue((server.EXAMPLES / "webapp-recipe.json").exists())

    def test_disabled_recipe_cannot_run(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/run", {"workflow":{"name":"disabled","active":False,"steps":[]}, "dry_run":True})
        self.assertEqual(ctx.exception.code, 409)

    def test_real_build_uses_structured_pipeline_without_legacy_settings_gate(self):
        workflow = {"name": "enabled", "active": True, "steps": []}
        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": "real-run", "status": "success"}) as run:
            status, response = self.request("POST", "/api/run", {"workflow": workflow, "dry_run": False})
        self.assertEqual(status, 200)
        self.assertEqual(response["run_id"], "real-run")
        run.assert_called_once_with(workflow, dry_run=False)

    def test_auto_validation_does_not_run_after_dry_run(self):
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True}})
        workflow = {"name": "dry-auto", "active": True, "steps": []}
        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": "dry-run", "status": "success"}) as run, mock.patch("debbuilder.app.validate_build_artifact") as validate:
            status, response = self.request("POST", "/api/run", {"workflow": workflow, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(response["run_id"], "dry-run")
        run.assert_called_once_with(workflow, dry_run=True)
        validate.assert_not_called()

    def test_successful_real_build_auto_validates_latest_artifact_when_enabled(self):
        store, run, artifact = self.successful_build_run(run_id="latest-auto-run", package="latest-auto", version="2.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": False}})

        def validate(run_id, payload):
            self.assertEqual(payload, {})
            current = store.load(run_id)
            validation = {"id": "auto-validation", "build_run_id": run_id, "artifact": current["artifact"]["path"], "status": "success"}
            current.setdefault("validations", []).append(validation)
            store.save(current)
            return validation

        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": run["id"], "status": "success"}), mock.patch("debbuilder.app.validate_build_artifact", side_effect=validate) as validate_mock, mock.patch("debbuilder.app.publish_build_artifact") as publish:
            status, response = self.request("POST", "/api/run", {"workflow": {"name": "latest-auto-recipe", "active": True}, "dry_run": False})
        self.assertEqual(status, 200)
        validate_mock.assert_called_once_with("latest-auto-run", {})
        publish.assert_not_called()
        self.assertEqual(response["validation"]["status"], "success")
        self.assertEqual(store.load("latest-auto-run")["validations"][0]["artifact"], str(artifact))
        self.assertEqual(server.get_package("latest-auto")["lifecycle_display_status"], "ready_to_publish")

    def test_auto_validation_uses_returned_build_run_not_previous_artifact(self):
        store, old, old_artifact = self.successful_build_run(run_id="old-auto-run", package="same-auto", version="1.0-1")
        old["created_at"] = "2026-01-01T00:00:00+00:00"
        old["validations"] = [{"id": "old-validation", "artifact": str(old_artifact), "status": "success"}]
        store.save(old)
        _, current, current_artifact = self.successful_build_run(run_id="current-auto-run", package="same-auto", version="2.0-1")
        current["created_at"] = "2026-01-02T00:00:00+00:00"
        store.save(current)
        server.update_settings({"automation": {"auto_validate_after_successful_build": True}})

        def validate(run_id, _payload):
            self.assertEqual(run_id, "current-auto-run")
            run = store.load(run_id)
            validation = {"id": "current-validation", "build_run_id": run_id, "artifact": str(current_artifact), "status": "success"}
            run.setdefault("validations", []).append(validation)
            store.save(run)
            return validation

        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": "current-auto-run", "status": "success"}), mock.patch("debbuilder.app.validate_build_artifact", side_effect=validate) as validate_mock:
            status, _response = self.request("POST", "/api/run", {"workflow": {"name": "same-auto-recipe", "active": True}, "dry_run": False})
        self.assertEqual(status, 200)
        validate_mock.assert_called_once()
        self.assertEqual(len(store.load("old-auto-run")["validations"]), 1)
        self.assertEqual(store.load("current-auto-run")["validations"][0]["artifact"], str(current_artifact))

    def test_auto_validation_failure_records_validation_failed_lifecycle(self):
        store, run, artifact = self.successful_build_run(run_id="failed-auto-run", package="failed-auto", version="3.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True}})

        def validate(run_id, _payload):
            current = store.load(run_id)
            validation = {"id": "failed-validation", "build_run_id": run_id, "artifact": str(artifact), "status": "failed", "error": {"message": "install failed"}}
            current.setdefault("validations", []).append(validation)
            store.save(current)
            return validation

        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": run["id"], "status": "success"}), mock.patch("debbuilder.app.validate_build_artifact", side_effect=validate):
            status, response = self.request("POST", "/api/run", {"workflow": {"name": "failed-auto-recipe", "active": True}, "dry_run": False})
        self.assertEqual(status, 200)
        self.assertEqual(response["validation"]["status"], "failed")
        self.assertEqual(server.get_package("failed-auto")["lifecycle_display_status"], "validation_failed")

    def test_auto_publish_runs_after_successful_auto_validation_when_enabled(self):
        store, run, artifact = self.successful_build_run(run_id="publish-auto-run", package="publish-auto", version="4.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True}})

        def validate(run_id, _payload):
            current = store.load(run_id)
            validation = {"id": "publish-validation", "build_run_id": run_id, "artifact": str(artifact), "status": "success"}
            current.setdefault("validations", []).append(validation)
            store.save(current)
            return validation

        def publish(run_id, payload):
            self.assertEqual(payload["confirm"], "publish:publish-auto:4.0-1")
            current = store.load(run_id)
            publication = {"id": "auto-publication", "build_run_id": run_id, "artifact": str(artifact), "status": "success", "published_version": "4.0-1"}
            current.setdefault("publications", []).append(publication)
            store.save(current)
            return publication

        with mock.patch("debbuilder.app.run_recipe_pipeline", return_value={"run_id": run["id"], "status": "success"}), mock.patch("debbuilder.app.validate_build_artifact", side_effect=validate), mock.patch("debbuilder.app.publish_build_artifact", side_effect=publish) as publish_mock:
            status, response = self.request("POST", "/api/run", {"workflow": {"name": "publish-auto-recipe", "active": True}, "dry_run": False})
        self.assertEqual(status, 200)
        publish_mock.assert_called_once()
        self.assertEqual(response["publication"]["status"], "success")
        self.assertEqual(server.get_package("publish-auto")["lifecycle_display_status"], "published")

    def test_dry_run_creates_structured_workspace_and_is_visible_in_logs(self):
        workflow = {
            "name": "structured", "package_name": "structured", "github_repository": "example/structured", "active": True,
            "package": {"name": "structured", "maintainer": "Test <test@example.test>", "description": "Structured test package"},
        }
        def acquire(_recipe, workspace, token=""):
            source = Path(workspace) / "source"
            (source / "requirements.txt").write_text("requests\n")
            return {"repository":"example/structured","ref":"v1.0.0","tag":"v1.0.0","upstream_version":"1.0.0","debian_version":"1.0.0-1","source_directory":str(source)}
        dependency_state = {"detected":["python3","python3-pip"],"manually_added":[],"required":["python3","python3-pip"],"available":["python3","python3-pip"],"missing":[],"checks":[],"installation_attempted":False}
        with mock.patch("debbuilder.build_pipeline.source_acquisition.acquire_source", side_effect=acquire), mock.patch("debbuilder.build_pipeline.dependency_checker.check_dependencies", return_value=dependency_state):
            status, result = self.request("POST", "/api/run", {"workflow": workflow, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "prepared")
        workspace = Path(result["workspace"])
        self.assertEqual(workspace.parent, server.DATA / "builds")
        self.assertTrue((workspace / "recipe.json").exists())
        self.assertEqual([step["status"] for step in result["steps"][:4]], ["success"] * 4)
        self.assertEqual(result["steps"][4]["status"], "skipped")
        self.assertEqual([step["status"] for step in result["steps"][5:]], ["success", "success", "skipped", "skipped", "skipped"])
        _, executions = self.request("GET", "/api/executions")
        row = next(item for item in executions["executions"] if item["id"] == result["run_id"])
        self.assertEqual(row["status"], "prepared")
        _, detail = self.request("GET", f"/api/executions/{result['run_id']}")
        self.assertEqual(detail["execution"]["recipe_sha256"], json.loads((workspace / "run.json").read_text())["recipe_sha256"])
        self.assertIn("snapshot", detail["execution"]["log"])

    def test_successful_build_artifact_can_be_validated_through_separate_endpoint(self):
        validation = {"id": "validation-one", "build_run_id": "run-one", "status": "success", "checks": [], "commands": []}
        with mock.patch("debbuilder.app.validate_build_artifact", return_value=validation) as validate:
            status, result = self.request("POST", "/api/executions/run-one/validate", {"previous_artifact": ""})
        self.assertEqual(status, 200)
        self.assertEqual(result["validation"]["status"], "success")
        validate.assert_called_once_with("run-one", {"previous_artifact": ""})

    def test_oidc_settings_round_trip_without_exposing_secret(self):
        payload = {
            "security": {"auth_mode": "oidc", "oidc_issuer": "https://id.example.test", "oidc_client_id": "debbuilder", "oidc_redirect_uri": "https://apt.example.test/auth/callback", "oidc_client_secret": "very-private-client-secret"},
        }
        view = server.update_settings(payload)
        self.assertEqual(view["security"]["auth_mode"], "oidc")
        self.assertTrue(view["security"]["oidc_client_secret_configured"])
        self.assertNotIn("very-private", json.dumps(view))
        self.assertNotIn("build", view)
        server.update_settings({"security": {**view["security"], "oidc_client_secret": ""}})
        self.assertEqual(server.oidc_client_secret(server.DATA), "very-private-client-secret")
        disabled = server.update_settings({"security": {"auth_mode": "none", "oidc_issuer": "", "oidc_client_id": "", "oidc_redirect_uri": ""}})
        self.assertEqual(disabled["security"]["auth_mode"], "none")

    def test_cookie_secret_is_generated_once_and_persisted(self):
        with mock.patch.dict(os.environ, {"DEBBUILDER_COOKIE_SECRET": ""}):
            first = server.cookie_secret(server.DATA)
            second = server.cookie_secret(server.DATA)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 40)
        self.assertEqual((server.DATA / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_legacy_build_settings_are_ignored(self):
        view = server.update_settings({"build": {"allow_real_run": False, "allow_unsafe_build_command": True, "temp_dir": "/tmp/apt-blockly-workflow"}})
        self.assertNotIn("build", view)
        self.assertNotIn("build", json.loads((server.DATA / "settings.json").read_text()))

    def test_oidc_protects_admin_but_public_repository_paths_are_exempt(self):
        server.update_settings({"security": {"auth_mode": "oidc", "oidc_issuer": "https://id.example.test", "oidc_client_id": "deb", "oidc_redirect_uri": "https://apt.example.test/auth/callback", "oidc_client_secret": "test-only-client-secret"}})
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/settings")
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()
        for path in ("/dists/stable/Release", "/pool/main/p/pkg.deb", "/repository.gpg", "/install.sh"):
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
            conn.request("GET", path)
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()

    def test_package_lifecycle_endpoints_refresh_update_verify_and_protected_publish(self):
        status, created = self.request("POST", "/api/packages", {
            "name": "download-ui",
            "architecture": "amd64",
            "apt_version": "1.0.0",
            "source": {"type": "github", "repository": "example/download-ui"},
        })
        self.assertEqual(status, 200)
        with mock.patch("debbuilder.app.github_client.repo_info", return_value={"repository": "example/download-ui", "default_branch": "main", "description": "Download UI", "language": "Python", "archived": False}), \
             mock.patch("debbuilder.app.github_client.list_branches", return_value=["main", "develop"]), \
             mock.patch("debbuilder.app.github_client.list_tags", return_value=["v1.1.0", "v1.0.0"]), \
             mock.patch("debbuilder.app.github_client.list_releases", return_value=[{"tag": "v1.1.0", "assets": ["download-ui_1.1.0_amd64.deb"]}]):
            status, source = self.request("POST", "/api/packages/download-ui/refresh-source")
        self.assertEqual(status, 200)
        self.assertEqual(source["source"]["default_branch"], "main")
        self.assertEqual(source["source"]["branches"], ["main", "develop"])
        self.assertEqual(source["source"]["latest_release"], "v1.1.0")

        status, update = self.request("POST", "/api/packages/download-ui/check-updates", {"source_version": "1.1.0"})
        self.assertEqual(status, 200)
        self.assertEqual(update["state"], "update_available")
        self.assertEqual(update["published_version"], "1.0.0")

        deb = Path(self.tmp.name) / "download-ui_1.1.0_amd64.deb"
        deb.write_bytes(b"fake")
        with mock.patch("debbuilder.app.deb_inspector.inspect_deb", return_value={"ok": True, "package": "download-ui", "version": "1.1.0", "architecture": "amd64", "size": 4, "files": [], "maintainer_scripts": []}):
            status, verified = self.request("POST", "/api/packages/download-ui/verify-deb", {"deb": str(deb)})
        self.assertEqual(status, 200)
        self.assertTrue(verified["verification"]["ok"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/packages/download-ui/publish", {"deb": str(deb), "dry_run": False})
        self.assertEqual(ctx.exception.code, 403)
        with mock.patch("debbuilder.app.operations.publish_deb_operation", return_value={"status": "dry_run", "package": "download-ui"}):
            status, published = self.request("POST", "/api/packages/download-ui/publish", {"deb": str(deb), "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(published["publication"]["status"], "dry_run")

    def test_admin_api_authentication_and_invalid_recipe_association(self):
        server.AUTH_MODE = "header"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("GET", "/api/packages")
        self.assertEqual(ctx.exception.code, 401)
        status, _ = self.request("GET", "/api/packages", headers={"X-Forwarded-User": "max"})
        self.assertEqual(status, 200)
        server.AUTH_MODE = "none"
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            self.request("POST", "/api/packages/webapp", {"recipe": "does-not-exist"})
        self.assertEqual(ctx2.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
