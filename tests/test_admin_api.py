import json
import hashlib
import os
import time
import urllib.error
import http.client
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from pathlib import Path

import debbuilder.app as server
from debbuilder import storage
from debbuilder import dependency_preparation
from debbuilder.build_store import BuildStore
from debbuilder.automation_identity import normalize_upstream_identity
from debbuilder.automation_ledger import AutomationLedger
from debbuilder.automation_scheduler import AutomationRetryStore
from debbuilder.build_store import canonical_recipe_sha256
from debbuilder.lifecycle import MutationGate
from tests.admin_api_case import AdminApiCase
from tests.validation_helpers import record_canonical_validation


def publication_attempt(run, attempt_id):
    artifact = run["artifact"]
    inspection = artifact["inspection"]
    apt = server.repo_settings()
    repository_root = str(server.REPOSITORY_ROOT.absolute())
    sha256 = artifact["sha256"]
    size = artifact["size"]
    package = inspection["package"]
    version = inspection["version"]
    architecture = inspection["architecture"]
    pool_path = f"pool/main/{package[0]}/{package}/{Path(artifact['path']).name}"
    return {
        "id": attempt_id, "build_run_id": run["id"], "status": "success", "artifact": artifact["path"],
        "package": package, "version": version, "architecture": architecture,
        "repository": {
            "root": repository_root, "distribution": apt["distribution"], "component": apt["component"],
        },
        "proof": {
            "schema": "debbuilder.repository-publication-proof.v1", "proof_version": 1,
            "verified_at": "2026-01-01T00:01:00+00:00",
            "repository": {"root": repository_root, "device": 1, "inode": 2},
            "distribution": {
                "requested": apt["distribution"], "codename": apt["distribution"], "suite": "stable",
            },
            "component": apt["component"], "package": package, "version": version,
            "architecture": architecture,
            "source": {"path": artifact["path"], "size": size, "sha256": sha256, "device": 3, "inode": 4},
            "targets": [{
                "database_architecture": apt["architecture"],
                "index": {
                    "path": f"dists/{apt['distribution']}/{apt['component']}/binary-{apt['architecture']}/Packages.gz",
                    "device": 5, "inode": 6, "filename": pool_path, "size": size, "sha256": sha256,
                },
                "pool": {"path": pool_path, "size": size, "sha256": sha256, "device": 7, "inode": 8},
            }],
        },
    }


class AdminApiTests(AdminApiCase):

    class AutomationSchedulerStub:
        def __init__(self):
            self.requests = []
            self.wakes = 0
            self.ledger = AutomationLedger(server.DATA)
            self.retry_store = AutomationRetryStore(server.DATA)

        def status(self):
            return {
                "state": "running", "admission_open": True, "pass_active": False,
                "last_pass_started": None, "last_pass_finished": None,
                "next_scheduled_check": "2026-09-15T12:00:00+00:00",
                "active_recipe_checks": 0, "global_blocker": None,
            }

        def recipe_activity(self, recipe_id):
            return {"queued": recipe_id in self.requests, "checking": False}

        def request_recipe(self, recipe_id):
            created = recipe_id not in self.requests
            if created:
                self.requests.append(recipe_id)
            return {"accepted": True, "created": created, "recipe_id": recipe_id}

        def request_observation(self, recipe_id):
            return self.request_recipe(recipe_id)

        def request(self):
            self.wakes += 1
            return True

        def request_orchestration(self):
            return self.request()

    @staticmethod
    def automation_identity():
        return normalize_upstream_identity({
            "provider": "github", "repository": "example/automatic", "tracking": "latest_release",
            "source_type": "release_asset", "payload_kind": "deb", "release_id": "10",
            "asset_id": "20", "asset_name": "automatic_1.2.3_all.deb", "expected_size": 7,
            "resolved_ref": "v1.2.3", "resolved_version": "1.2.3",
            "expected_package": "automatic", "expected_architecture": "all",
        })

    def automatic_recipe(self, *, enabled=True, policy="build", active=True):
        return {
            "schema_version": 5, "name": "automatic", "active": active,
            "automation": {"enabled": enabled, "policy": policy},
            "package": {"name": "automatic", "architecture": "all", "description": "Automatic"},
            "source": {"provider": "github", "repository": "example/automatic", "tracking": "latest_release", "version": {"source": "tag"}},
            "artifact": {"mode": "upstream_deb", "architecture": "all", "name_pattern": "automatic_*_all.deb"},
        }

    @staticmethod
    def observation_result(version="9.8.7"):
        return {
            "display_version": version,
            "display_ref": f"v{version}",
            "identity": {"private_exact_identity": "must-not-be-persisted"},
        }

    def test_explicit_observation_refresh_supports_manual_disabled_and_inactive_without_lifecycle(self):
        recipes = (
            self.automatic_recipe(enabled=False, policy="manual"),
            self.automatic_recipe(enabled=False, policy="manual", active=False),
        )
        service = server.upstream_observation_service()
        service.resolver = lambda _recipe, token="": self.observation_result()
        before_runs = list((server.DATA / "builds").rglob("run.json"))
        before_ledger = (server.DATA / "automation-ledger.json").read_bytes() if (server.DATA / "automation-ledger.json").exists() else None
        for index, configured in enumerate(recipes):
            configured["name"] = f"observe-{index}"
            configured["package"]["name"] = f"observe-{index}"
            configured["source"]["repository"] = f"owner/observe-{index}"
            status, _ = self.request("POST", f"/api/workflows/observe-{index}", {"workflow": configured})
            self.assertEqual(status, 200)
            status, refreshed = self.request("POST", f"/api/recipes/observe-{index}/observation/refresh", {})
            self.assertEqual(status, 200)
            self.assertEqual(refreshed["observation"]["last_success"]["display_version"], "9.8.7")
        self.assertEqual(list((server.DATA / "builds").rglob("run.json")), before_runs)
        after_ledger = (server.DATA / "automation-ledger.json").read_bytes() if (server.DATA / "automation-ledger.json").exists() else None
        self.assertEqual(after_ledger, before_ledger)
        persisted = (server.DATA / "upstream-observations.json").read_text()
        self.assertNotIn("private_exact_identity", persisted)

    def test_explicit_observation_refresh_supports_application_managed_recipe(self):
        service = server.upstream_observation_service()
        service.resolver = lambda _recipe, token="": self.observation_result("1.0.0")
        status, refreshed = self.request("POST", "/api/recipes/debbuilder/observation/refresh", {})
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["observation"]["last_success"]["display_ref"], "v1.0.0")

    def test_failed_explicit_refresh_records_bounded_attempt_without_lifecycle(self):
        configured = self.automatic_recipe(enabled=False, policy="manual")
        self.request("POST", "/api/workflows/automatic", {"workflow": configured})
        service = server.upstream_observation_service()
        service.resolver = lambda _recipe, token="": (_ for _ in ()).throw(
            server.upstream_detection.UpstreamDetectionError("github_unavailable", "bounded"),
        )
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("POST", "/api/recipes/automatic/observation/refresh", {})
        self.assertEqual(captured.exception.code, 502)
        payload = json.loads(captured.exception.read())
        self.assertEqual(payload["error"]["code"], "github_unavailable")
        recipe = server.read_workflow_file(server.USER_WORKFLOWS / "automatic.json")
        observation = service.projection("automatic", recipe)
        self.assertEqual(observation["last_attempt"]["diagnostic_code"], "github_unavailable")
        self.assertFalse((server.DATA / "automation-ledger.json").exists())

    def test_package_and_recipe_get_routes_never_own_upstream_or_background_work(self):
        service = server.upstream_observation_service()
        observation_before = service.store.path.read_bytes() if service.store.path.exists() else None
        with mock.patch.object(service, "observe", side_effect=AssertionError("GET attempted observation")) as observe, \
                mock.patch("debbuilder.github_client.latest_release", side_effect=AssertionError("GET called GitHub")) as github, \
                mock.patch("debbuilder.app.apt_repo.local_packages_index", return_value=[]), \
                mock.patch.object(ThreadPoolExecutor, "submit", side_effect=AssertionError("GET submitted background work")) as submit:
            for path in ("/api/dashboard", "/api/packages", "/api/packages/webapp", "/api/recipes"):
                status, _payload = self.request("GET", path)
                self.assertEqual(status, 200, path)
        observe.assert_not_called()
        github.assert_not_called()
        submit.assert_not_called()
        observation_after = service.store.path.read_bytes() if service.store.path.exists() else None
        self.assertEqual(observation_after, observation_before)

    def complete_manual_run(self, run_id: str) -> dict:
        store = BuildStore(server.DATA / "builds")
        with mock.patch.object(
            server.build_pipeline,
            "execute_pipeline_run",
            return_value={"run_id": run_id, "status": "success"},
        ):
            return server.execute_queued_recipe_run(
                run_id, store=store, expected_initial_status="queued",
            )

    def test_recipe_automation_save_roundtrip_wakes_without_detection(self):
        scheduler = self.AutomationSchedulerStub()
        previous = server.APPLICATION_AUTOMATION_SCHEDULER
        server.APPLICATION_AUTOMATION_SCHEDULER = scheduler
        try:
            status, saved = self.request("POST", "/api/workflows/automatic", {"workflow": self.automatic_recipe()})
            self.assertEqual(status, 200)
            self.assertTrue(saved["ok"])
            self.assertEqual(scheduler.requests, ["automatic"])
            status, loaded = self.request("GET", "/api/workflows/automatic")
            self.assertEqual(status, 200)
            self.assertEqual(loaded["automation"], {"enabled": True, "policy": "build"})

            disabled = {**loaded, "automation": {"enabled": False, "policy": "build"}}
            self.request("POST", "/api/workflows/automatic", {"workflow": disabled, "previous_id": "automatic"})
            _, reloaded = self.request("GET", "/api/workflows/automatic")
            self.assertEqual(reloaded["automation"], {"enabled": False, "policy": "build"})
            self.assertEqual(scheduler.requests, ["automatic"])

            invalid = {**loaded, "automation": {"enabled": True, "policy": "everything"}}
            with self.assertRaises(urllib.error.HTTPError) as invalid_response:
                self.request("POST", "/api/workflows/automatic", {"workflow": invalid, "previous_id": "automatic"})
            self.assertEqual(invalid_response.exception.code, 422)
            error = json.loads(invalid_response.exception.read())["error"]
            self.assertEqual(error["code"], "invalid_automation_policy")
            self.assertEqual(error["details"]["path"], "$.automation.policy")
        finally:
            server.APPLICATION_AUTOMATION_SCHEDULER = previous

    def test_recipe_automation_policy_matrix_round_trips_exact_values(self):
        scheduler = self.AutomationSchedulerStub()
        previous = server.APPLICATION_AUTOMATION_SCHEDULER
        server.APPLICATION_AUTOMATION_SCHEDULER = scheduler
        try:
            for index, policy in enumerate(("manual", "detect", "test", "build", "build_validate", "full")):
                configured = self.automatic_recipe(enabled=True, policy=policy)
                body = {"workflow": configured}
                if index:
                    body["previous_id"] = "automatic"
                status, _saved = self.request("POST", "/api/workflows/automatic", body)
                self.assertEqual(status, 200)
                _, loaded = self.request("GET", "/api/workflows/automatic")
                self.assertEqual(loaded["automation"], {"enabled": True, "policy": policy})
                if policy == "manual":
                    self.assertEqual(scheduler.requests, ["automatic"])
            self.assertEqual(scheduler.requests, ["automatic"])
        finally:
            server.APPLICATION_AUTOMATION_SCHEDULER = previous

    def test_automation_status_and_check_now_routes_are_bounded_and_coalesced(self):
        scheduler = self.AutomationSchedulerStub()
        previous = server.APPLICATION_AUTOMATION_SCHEDULER
        server.APPLICATION_AUTOMATION_SCHEDULER = scheduler
        try:
            self.request("POST", "/api/workflows/automatic", {"workflow": self.automatic_recipe()})
            scheduler.requests.clear()
            status, viewed = self.request("GET", "/api/recipes/automatic/automation")
            self.assertEqual(status, 200)
            projection = viewed["automation"]
            self.assertEqual(projection["state"], "watching")
            self.assertTrue(projection["can_check_now"])
            encoded = json.dumps(projection)
            for forbidden in ("attempt_key", "recipe_sha256", "upstream_identity", "workspace", "asset_url"):
                self.assertNotIn(forbidden, encoded)

            first_status, first = self.request("POST", "/api/recipes/automatic/automation/check", {})
            second_status, second = self.request("POST", "/api/recipes/automatic/automation/check", {})
            self.assertEqual((first_status, second_status), (202, 202))
            self.assertTrue(first["automation"]["created"])
            self.assertFalse(second["automation"]["created"])
            self.assertEqual(scheduler.requests, ["automatic"])
        finally:
            server.APPLICATION_AUTOMATION_SCHEDULER = previous

    def test_automation_check_rejects_disabled_and_retry_is_exact(self):
        scheduler = self.AutomationSchedulerStub()
        previous = server.APPLICATION_AUTOMATION_SCHEDULER
        server.APPLICATION_AUTOMATION_SCHEDULER = scheduler
        try:
            self.request("POST", "/api/workflows/automatic", {"workflow": self.automatic_recipe(enabled=False)})
            with self.assertRaises(urllib.error.HTTPError) as disabled:
                self.request("POST", "/api/recipes/automatic/automation/check", {})
            self.assertEqual(disabled.exception.code, 409)
            self.assertEqual(json.loads(disabled.exception.read())["error"]["code"], "automation_disabled")

            configured = self.automatic_recipe()
            self.request("POST", "/api/workflows/automatic", {"workflow": configured, "previous_id": "automatic"})
            scheduler.requests.clear()
            canonical = server.read_workflow_file(server.USER_WORKFLOWS / "automatic.json")
            claim = scheduler.ledger.claim_current_recipe(
                server.USER_WORKFLOWS / "automatic.json", "automatic", self.automation_identity(),
                canonical_recipe_sha256(canonical), "build",
            )
            scheduler.ledger.finish(claim.attempt_key, 0, "terminal_failure", diagnostic="run_failed")
            _, viewed = self.request("GET", "/api/recipes/automatic/automation")
            projection = viewed["automation"]
            self.assertTrue(projection["can_retry"])
            status, retried = self.request("POST", "/api/recipes/automatic/automation/retry", {
                "generation": projection["generation"], "revision": projection["revision"],
            })
            self.assertEqual(status, 202)
            self.assertEqual(retried["automation"]["generation"], 1)
            with self.assertRaises(urllib.error.HTTPError) as stale:
                self.request("POST", "/api/recipes/automatic/automation/retry", {
                    "generation": projection["generation"], "revision": projection["revision"],
                })
            self.assertEqual(stale.exception.code, 409)
            self.assertEqual(len(scheduler.ledger.read()["attempts"][claim.attempt_key]["generations"]), 2)
        finally:
            server.APPLICATION_AUTOMATION_SCHEDULER = previous

    def test_resource_settings_api_round_trip_and_structured_validation(self):
        status, initial = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(all(value is None for value in initial["settings"]["resource_limits"].values()))
        policy = {
            "memory_max_bytes": 536870912,
            "tasks_max": 128,
            "cpu_quota_percent": 250,
            "io_read_bandwidth_max_bytes_per_sec": None,
            "io_write_bandwidth_max_bytes_per_sec": 1048576,
        }
        status, updated = self.request("POST", "/api/settings", {"resource_limits": policy})
        self.assertEqual(status, 200)
        self.assertEqual(updated["settings"]["resource_limits"], policy)
        self.assertEqual(json.loads((server.DATA / "settings.json").read_text())["resource_limits"], policy)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/settings", {"resource_limits": {"tasks_max": False}})
        self.assertEqual(raised.exception.code, 422)
        error = json.loads(raised.exception.read())["error"]
        self.assertEqual(error["code"], "invalid_resource_limit")
        self.assertEqual(error["details"]["path"], "$.resource_limits.tasks_max")

    def test_get_package_list_seeded_from_inventory_and_recipe_association(self):
        status, data = self.request("GET", "/api/packages")
        self.assertEqual(status, 200)
        names = [p["name"] for p in data["packages"]]
        self.assertEqual(names, ["debbuilder", "monitoring-app", "webapp"])
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
        storage.save_json(server.DATA / "packages.json", [
            {"name":"github-demo","apt_version":"1.0","upstream_version":"2.0","recipe":"webapp-recipe","source":{"type":"github","repository":"o/r"}},
            {"name":"local-demo","apt_version":"1.0-1","upstream_version":"1.0","recipe":"webapp-recipe","source":{"type":"local"}},
        ])
        summary = server.dashboard_summary()
        self.assertEqual(summary["packages"], 5)
        self.assertEqual(summary["updates"], 0)
        self.assertEqual(summary["state_counts"].get("update_available", 0), 0)
        self.assertIn("github-demo", [row["name"] for row in summary["package_rows"]])
        self.assertIn("local-demo", [row["name"] for row in summary["package_rows"]])
        self.assertTrue(all("history" not in row for row in summary["package_rows"]))

    def test_dashboard_reuses_package_lifecycle_state_without_recomparing_versions(self):
        packages = [
            {"name": "same", "lifecycle_state": "up_to_date", "version": {"source": "3.4.1", "published": "3.4.1-2"}},
            {"name": "new", "lifecycle_state": "update_available", "version": {"source": "3.4.2", "published": "3.4.1-2"}},
            {"name": "pending", "lifecycle_state": "publication_available", "lifecycle_display_status": "validation_needed", "version": {"source": "3.4.3", "published": "3.4.1-2"}},
        ]
        with mock.patch("debbuilder.app.list_packages", return_value=packages), mock.patch("debbuilder.app.list_executions", return_value=[]):
            summary = server.dashboard_summary()
        self.assertEqual(summary["state_counts"], {"up_to_date": 1, "update_available": 1, "validation_needed": 1})
        states = {row["name"]: row["lifecycle_display_status"] for row in summary["package_rows"]}
        self.assertEqual(states["same"], "up_to_date")
        self.assertEqual(states["pending"], "validation_needed")

    def test_package_list_prefers_live_apt_repository_versions_when_available(self):
        settings = server.settings_defaults()
        settings["apt"] = {"repository": "https://repo.example.test", "distribution": "testing", "component": "main", "architecture": "amd64"}
        server.settings_service.save_settings(server.DATA, settings)
        with mock.patch("debbuilder.app.apt_repo.local_packages_index", return_value=[
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
            "schema_version": 5, "name": "demo-recipe", "active": True,
            "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Real demo", "runtime_dependencies": ["curl"]},
            "source": {"provider": "github", "repository": "owner/demo", "tracking": "latest_release", "version": {"source": "tag"}},
        }
        (server.USER_WORKFLOWS / "demo-recipe.json").write_text(json.dumps(recipe))
        store = BuildStore(server.DATA / "builds")
        run = store.create(recipe, recipe_id="demo-recipe", mode="build", run_id="structured-run")
        artifact = Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "version": {"upstream": "2.0", "debian": "2.0-1"}, "artifact": {
            "path": str(artifact), "size": 3, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "inspection": {"package": "demo", "version": "2.0-1", "architecture": "all"},
        }})
        source = next(step for step in run["steps"] if step["name"] == "source")
        source["details"] = {"repository": "owner/demo", "ref": "v2.0", "tag": "v2.0", "release_url": "https://github.test/v2.0"}
        run["publications"] = [{
            **publication_attempt(run, "publication"),
            "finished_at": "2026-01-01T00:01:00+00:00",
        }]
        store.save(run)
        record_canonical_validation(store, run, attempt_id="validation")
        published = [{"Package": "demo", "Version": "2.0-1", "Architecture": "all", "Filename": "pool/main/d/demo.deb"}]
        server.upstream_observation_service().store.record_success(
            "demo-recipe", canonical_recipe_sha256(recipe),
            {"display_version": "2.0", "display_ref": "v2.0"},
        )
        with mock.patch("debbuilder.app.live_published_index", return_value=published):
            first = server.get_package("demo")
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
            "schema_version": 5, "name": "debbuilder-recipe", "active": True,
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
                "artifact": {"path": str(artifact), "size": 3, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "inspection": {"package": "debbuilder", "version": version, "architecture": "all"}},
            })
            store.save(run)
            return run, artifact

        old, old_artifact = build_run("old-published", "0.1.3-2", "2026-01-01T00:00:00+00:00")
        old["publications"] = [publication_attempt(old, "old-publication")]
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

        with mock.patch("debbuilder.app.live_published_index", return_value=published_old):
            package = server.get_package("debbuilder")
        self.assertEqual(package["lifecycle_display_status"], "validation_needed")
        self.assertFalse(package["build"]["ready_to_publish"])
        serialized_package = json.dumps(package)
        self.assertNotIn("BEGIN PGP PRIVATE KEY", serialized_package)
        self.assertNotIn("/tmp/private-validation", serialized_package)
        self.assertIsNone(package["validation"])

        record_canonical_validation(store, current, attempt_id="current-validation")
        current["publications"] = [publication_attempt(current, "current-publication")]
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
            "schema_version": 5, "name": "stable-recipe", "active": True,
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
        self.assertEqual(package["lifecycle_display_status"], "unknown")
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

    def test_create_update_delete_package_rejects_removed_repository_delete_options(self):
        status, created = self.request("POST", "/api/packages", {"name": "download-ui", "architecture": "amd64", "source": {"type": "github", "repository": "example/download-ui"}})
        self.assertEqual(status, 200)
        self.assertEqual(created["package"]["status"], "recipe_missing")
        status, updated = self.request("POST", "/api/packages/download-ui", {"description": "Download UI package", "status": "unknown"})
        self.assertEqual(updated["package"]["description"], "Download UI package")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("DELETE", "/api/packages/download-ui?delete_repo=1")
        self.assertEqual(ctx.exception.code, 400)
        status, deleted = self.request("DELETE", "/api/packages/download-ui")
        self.assertTrue(deleted["ok"])

    def test_recipe_list_logs_execution_detail_and_settings_are_available(self):
        status, recipes = self.request("GET", "/api/recipes")
        self.assertIn("webapp-recipe", [row["id"] for row in recipes["recipes"]])
        status, executions = self.request("GET", "/api/executions")
        self.assertEqual(executions["executions"][0]["id"], "20260822-031400")
        status, detail = self.request("GET", "/api/executions/20260822-031400")
        self.assertNotIn("log", detail["execution"])
        _, log = self.request("GET", "/api/executions/20260822-031400/logs?verbosity=raw")
        self.assertIn("ok", log["log"]["text"])
        status, settings = self.request("GET", "/api/settings")
        self.assertNotIn("build", settings["settings"])
        self.assertNotIn("token", settings["settings"]["github"])

    def test_execution_detail_hides_source_and_archive_identity(self):
        store = BuildStore(server.DATA / "builds")
        for mode in ("source_build", "archive_payload", "upstream_deb"):
            with self.subTest(mode=mode):
                run = store.load("20260822-031400")
                source = next(step for step in run["steps"] if step["name"] == "source")
                source["details"] = {"artifact_mode": mode, "upstream_identity": self.automation_identity()}
                if mode == "upstream_deb":
                    run["artifact"] = {"name": "demo.deb", "upstream_identity": self.automation_identity()}
                    artifact_step = next(step for step in run["steps"] if step["name"] == "artifact")
                    artifact_step["details"] = dict(run["artifact"])
                else:
                    run["artifact"] = None
                store.save(run)
                before = (store.run_dir(run["id"]) / "run.json").read_bytes()
                _, response = self.request("GET", f"/api/executions/{run['id']}")
                projected = next(step for step in response["execution"]["steps"] if step["name"] == "source")
                self.assertEqual(projected["details"], {"mode": mode})
                self.assertNotIn("upstream_identity", json.dumps(response))
                if mode == "upstream_deb":
                    self.assertEqual(response["execution"]["artifact"]["name"], "demo.deb")
                    self.assertNotIn("path", response["execution"]["artifact"])
                self.assertEqual((store.run_dir(run["id"]) / "run.json").read_bytes(), before)

    def test_execution_endpoints_project_every_canonical_lifecycle_transition(self):
        store, run, artifact = self.successful_build_run(run_id="lifecycle-run", package="lifecycle", version="2.0-1")
        run["artifact"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        store.save(run)

        def assert_state(lifecycle, *, active, validate, publish):
            _, listed = self.request("GET", "/api/executions")
            summary = next(row for row in listed["executions"] if row["id"] == run["id"])
            _, response = self.request("GET", f"/api/executions/{run['id']}")
            detail = response["execution"]
            for row in (summary, detail):
                self.assertEqual(
                    row["lifecycle_status"], lifecycle,
                    {"projected": row, "stored": store.load(run["id"])},
                )
                self.assertEqual(row["lifecycle_active"], active)
                self.assertEqual(row["allowed_actions"], {"validate": validate, "publish": publish})
            self.assertEqual(detail["package"], "lifecycle")
            return detail

        run["status"] = "running"
        run["steps"][4]["status"] = "running"
        store.save(run)
        detail = assert_state("building", active=True, validate=False, publish=False)
        self.assertEqual(detail["steps"][4]["status"], "running")

        run["status"] = "success"
        run["steps"][4]["status"] = "success"
        store.save(run)
        detail = assert_state("validation_needed", active=False, validate=True, publish=False)
        self.assertEqual(detail["artifact"]["name"], artifact.name)
        self.assertNotIn("path", detail["artifact"])

        record_canonical_validation(store, run, attempt_id="validation-one", status="running")
        detail = assert_state("validating", active=True, validate=False, publish=False)
        self.assertNotIn("BEGIN PGP PRIVATE KEY", json.dumps(detail))
        self.assertNotIn("/tmp/private-validation", json.dumps(detail))
        self.assertNotIn("commands", detail["validations"][-1])
        self.assertNotIn("backend", detail["validations"][-1])
        run = store.load(run["id"])
        record_canonical_validation(store, run, attempt_id="validation-one", status="failed")
        assert_state("validation_failed", active=False, validate=True, publish=False)
        run = store.load(run["id"])
        record_canonical_validation(
            store, run, attempt_id="validation-two", status="running",
            created_at="2026-09-14T09:00:00+00:00",
        )
        assert_state("validating", active=True, validate=False, publish=False)
        run = store.load(run["id"])
        record_canonical_validation(
            store, run, attempt_id="validation-two", status="success",
            created_at="2026-09-14T09:00:00+00:00",
        )
        assert_state("ready_to_publish", active=False, validate=True, publish=True)

        run["publications"] = [{"id": "publication-one", "status": "running"}]
        store.save(run)
        assert_state("publishing", active=True, validate=False, publish=False)
        run["publications"][-1]["status"] = "failed"
        store.save(run)
        assert_state("publication_failed", active=False, validate=True, publish=True)
        run["publications"].append({"id": "publication-two", "status": "running"})
        store.save(run)
        assert_state("publishing", active=True, validate=False, publish=False)
        run["publications"][-1] = publication_attempt(run, "publication-two")
        store.save(run)
        assert_state("published", active=False, validate=True, publish=False)

    def test_execution_list_does_not_include_staging_inventory_or_manifest_contents(self):
        store = BuildStore(server.DATA / "builds")
        run = store.create({
            "schema_version": 5,
            "name": "large", "active": True, "source": {"repository": "example/large"},
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
            "schema_version": 5,
            "name": "logs", "active": True, "source": {"repository": "example/logs"},
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
            "schema_version": 5,
            "name": "cleanup", "active": True, "source": {"repository": "example/cleanup"},
            "package": {"name": "cleanup", "maintainer": "Test <test@example.test>", "description": "Cleanup"},
        }, mode="build", run_id="cleanup-run")
        artifact = Path(run["workspace"]) / "artifacts/cleanup.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc", "inspection": {"package": "cleanup", "version": "1.0-1", "architecture": "all"}}})
        run["steps"][4]["details"] = {"commands": [{"index": 1, "stdout": "long output", "stderr": ""}]}
        store.save(run)
        store.append_log_line("cleanup-run", "long output")
        stale_run = store.load("cleanup-run")
        status, execution_list = self.request("GET", "/api/executions")
        self.assertEqual(status, 200)
        self.assertIn("cleanup-run", [row["id"] for row in execution_list["executions"]])
        status, deletion = self.request("DELETE", "/api/executions/cleanup-run/logs")
        self.assertEqual(status, 200)
        self.assertEqual(deletion["deletion"]["deleted"], "log_history")
        self.assertTrue(deletion["deletion"]["history_deleted"])
        self.assertFalse(deletion["deletion"]["visible"])
        self.assertFalse(deletion["deletion"]["already_deleted"])
        self.assertTrue(store.execution_history_deletion_path("cleanup-run").is_file())
        cleaned = store.load("cleanup-run")
        self.assertTrue(artifact.exists())
        self.assertEqual(cleaned["status"], "success")
        self.assertEqual(cleaned["artifact"]["path"], str(artifact))
        self.assertEqual(cleaned["steps"][4]["details"]["commands"][0]["stdout"], "")

        # A lifecycle worker may still hold a pre-deletion Run snapshot. Its
        # later save must not resurrect the execution in canonical history.
        store.save(stale_run)
        restarted_store = BuildStore(server.DATA / "builds")
        self.assertTrue(restarted_store.execution_history_deleted("cleanup-run", restarted_store.load("cleanup-run")))
        package = server.get_package("cleanup")
        self.assertEqual(package["build"]["latest_run_id"], "cleanup-run")
        self.assertEqual(package["lifecycle_display_status"], "validation_needed")
        self.assertNotIn("cleanup-run", [row["id"] for row in package.get("history", [])])
        status, execution_list = self.request("GET", "/api/executions")
        self.assertEqual(status, 200)
        self.assertNotIn("cleanup-run", [row["id"] for row in execution_list["executions"]])
        with self.assertRaises(urllib.error.HTTPError) as detail_error:
            self.request("GET", "/api/executions/cleanup-run")
        self.assertEqual(detail_error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as log_error:
            self.request("GET", "/api/executions/cleanup-run/logs")
        self.assertEqual(log_error.exception.code, 404)
        status, repeated = self.request("DELETE", "/api/executions/cleanup-run/logs")
        self.assertEqual(status, 200)
        self.assertTrue(repeated["deletion"]["already_deleted"])
        self.assertFalse(repeated["deletion"]["visible"])

    def test_clear_all_execution_logs_preserves_lifecycle_and_artifacts(self):
        store = BuildStore(server.DATA / "builds")
        for run_id in ("batch-one", "batch-two"):
            run = store.create({"schema_version": 5, "name": run_id, "package": {"name": run_id}, "source": {"repository": f"example/{run_id}"}, "active": True}, mode="dry_run", run_id=run_id)
            run["status"] = "prepared"
            store.save(run)
            store.append_log_line(run_id, "temporary detail")
        run = store.create({
            "schema_version": 5,
            "name": "global-cleanup", "active": True, "source": {"repository": "example/global-cleanup"},
            "package": {"name": "global-cleanup", "maintainer": "Test <test@example.test>", "description": "Cleanup"},
        }, mode="build", run_id="global-cleanup-run")
        artifact = Path(run["workspace"]) / "artifacts/global-cleanup.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc", "inspection": {"package": "global-cleanup", "version": "1.0-1", "architecture": "all"}}})
        run["steps"][4]["details"] = {"commands": [{"index": 1, "stdout": "long output", "stderr": ""}]}
        store.save(run)
        store.append_log_line("global-cleanup-run", "temporary detail")
        stale_batch_one = store.load("batch-one")
        status, preview = self.request("POST", "/api/executions/delete-logs", {"all": True, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(preview["count"], 3)
        self.assertIn("global-cleanup-run", preview["ids"])
        status, result = self.request("POST", "/api/executions/delete-logs", {"all": True})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["deleted"]), 3)
        self.assertEqual(result["errors"], [])
        self.assertTrue(all(row["history_deleted"] and row["visible"] is False for row in result["deleted"]))
        self.assertTrue(all(store.execution_history_deletion_path(run_id).is_file() for run_id in ("batch-one", "batch-two", "global-cleanup-run")))
        self.assertTrue(store.load("batch-one")["log_deleted"])
        cleaned = store.load("global-cleanup-run")
        self.assertTrue(cleaned["log_deleted"])
        self.assertTrue(artifact.exists())
        self.assertEqual(cleaned["artifact"]["path"], str(artifact))
        self.assertNotIn("validations", cleaned)
        self.assertEqual(server.get_package("global-cleanup")["lifecycle_display_status"], "validation_needed")

        store.save(stale_batch_one)
        restarted_store = BuildStore(server.DATA / "builds")
        self.assertTrue(restarted_store.execution_history_deleted("batch-one", restarted_store.load("batch-one")))
        status, execution_list = self.request("GET", "/api/executions")
        self.assertEqual(status, 200)
        visible_ids = [row["id"] for row in execution_list["executions"]]
        for run_id in ("batch-one", "batch-two", "global-cleanup-run"):
            self.assertNotIn(run_id, visible_ids)
        self.assertNotIn("global-cleanup-run", [row["id"] for row in server.get_package("global-cleanup").get("history", [])])
        with self.assertRaises(urllib.error.HTTPError) as detail_error:
            self.request("GET", "/api/executions/batch-one")
        self.assertEqual(detail_error.exception.code, 404)
        status, second_preview = self.request("POST", "/api/executions/delete-logs", {"all": True, "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(second_preview["count"], 0)

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
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["apt"]["repository"], "https://repo.example.test")
        self.assertNotIn("github", saved)
        self.assertNotIn("configured", saved["notifications"])

    def test_settings_api_rejects_unknown_and_coerced_fields_without_writing(self):
        settings_path = server.DATA / "settings.json"
        for payload, path in (
            ([], "$"),
            ({"legacy": True}, "$.legacy"),
            ({"general": {"port": 8080}}, "$.general.port"),
            ({"github": {"token_configured": True}}, "$.github.token_configured"),
            ({"notifications": {"configured": True}}, "$.notifications.configured"),
            ({"automation": {"auto_validate_after_successful_build": "false"}}, "$.automation.auto_validate_after_successful_build"),
            ({"workspace_cleanup": {"enabled": 1}}, "$.workspace_cleanup.enabled"),
            ({"security": {"pocket_id_active": False}}, "$.security.pocket_id_active"),
        ):
            with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", "/api/settings", payload)
            self.assertEqual(raised.exception.code, 422)
            self.assertEqual(json.loads(raised.exception.read())["error"]["details"]["path"], path)
            self.assertFalse(settings_path.exists())

        with self.assertRaises(urllib.error.HTTPError):
            self.request("POST", "/api/settings", {
                "legacy": True,
                "github": {"token": "ghlocalvalue12345678901234567890"},
                "notifications": {"token": "private-notification-token"},
            })
        self.assertFalse(settings_path.exists())
        self.assertFalse((server.DATA / "secrets.json").exists())

    def test_execution_deletion_rejects_active_runs_without_cancelling_them(self):
        store, run, artifact = self.successful_build_run("busy-run")
        record_canonical_validation(store, run, attempt_id="active-validation", status="running")
        source = Path(run["workspace"]) / "source/keep"
        source.write_text("active workspace")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("DELETE", "/api/executions/busy-run/logs")
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(json.loads(error.exception.read())["error"]["code"], "execution_active")
        self.assertTrue(source.exists())
        self.assertTrue(artifact.exists())
        self.assertEqual(
            server.validation_service.load_attempt(store, run["id"], "active-validation")["status"],
            "running",
        )
        _, preview = self.request("POST", "/api/executions/delete-logs", {"all": True, "dry_run": True})
        self.assertNotIn(run["id"], preview["ids"])
        _, cleared = self.request("POST", "/api/executions/delete-logs", {"all": True})
        self.assertNotIn(run["id"], [row["id"] for row in cleared["deleted"]])
        _, visible = self.request("GET", "/api/executions")
        self.assertIn(run["id"], [row["id"] for row in visible["executions"]])

    def test_workspace_policy_round_trip_and_automation_requests_cleanup(self):
        store, run, artifact = self.successful_build_run("cleanup-automation")
        source = Path(run["workspace"]) / "source/large-output"
        source.write_text("temporary")
        _, settings = self.request("GET", "/api/settings")
        self.assertEqual(settings["settings"]["workspace_cleanup"], {"enabled": True, "failed_workspaces_to_retain": 5})
        _, saved = self.request("POST", "/api/settings", {"workspace_cleanup": {"enabled": False, "failed_workspaces_to_retain": 2}})
        self.assertEqual(saved["settings"]["workspace_cleanup"], {"enabled": False, "failed_workspaces_to_retain": 2})
        with mock.patch("debbuilder.app.request_maintenance") as request:
            self.complete_manual_run(run["id"])
        request.assert_called_once_with(cleanup=True)
        self.assertTrue(source.exists())
        self.request("POST", "/api/settings", {"workspace_cleanup": {"enabled": True}})
        with mock.patch("debbuilder.app.request_maintenance") as request:
            result = self.complete_manual_run(run["id"])
        self.assertEqual(result["status"], "success")
        request.assert_called_once_with(cleanup=True)
        self.assertTrue(source.exists())
        server.cleanup_workspaces(authorization=self.httpd.cleanup_authorization)
        self.assertFalse(source.exists())
        self.assertTrue(artifact.exists())
        self.assertIsNotNone(server.get_execution(run["id"]))
        _, loaded = self.request("GET", "/api/settings")
        self.assertEqual(loaded["settings"]["workspace_cleanup"]["failed_workspaces_to_retain"], 2)

    def test_requested_maintenance_uses_current_data_and_stops_cleanly(self):
        store, run, _artifact = self.successful_build_run("sweep-run")
        source = Path(run["workspace"]) / "source"
        alternate = server.DATA / "other-data"
        alternate.mkdir()
        with mock.patch.object(server, "DATA", alternate):
            self.assertEqual(server.cleanup_workspaces()["cleaned"], [])
        self.assertTrue(source.exists())
        service = server.create_maintenance_service(self.httpd)
        service.start()
        service.request(cleanup=True)
        deadline = time.monotonic() + 2
        while source.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        service.join(2)
        self.assertFalse(source.exists())
        self.assertFalse(service.is_alive())

    def test_all_safe_settings_sections_can_be_updated(self):
        body = {
            "general": {"app_name": "Package Console", "url": "https://console.example.test"},
            "apt": {"repository": "https://repo.example.test", "distribution": "testing", "component": "main", "architecture": "amd64"},
            "github": {},
            "notifications": {"type": "ntfy", "server_url": "https://ntfy.example.test", "topic": "debbuilder"},
            "automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True},
        }
        status, updated = self.request("POST", "/api/settings", body)
        self.assertEqual(status, 200)
        settings = updated["settings"]
        self.assertEqual(set(settings), {
            "general", "apt", "github", "notifications", "automation",
            "workspace_cleanup", "resource_limits", "resource_limits_status", "security",
        })
        self.assertEqual(set(settings["general"]), {"app_name", "url"})
        self.assertEqual(set(settings["github"]), {"token_configured"})
        self.assertEqual(set(settings["notifications"]), {"type", "server_url", "topic", "token_configured"})
        self.assertEqual(set(settings["security"]), {
            "auth_mode", "oidc_issuer", "oidc_client_id", "oidc_redirect_uri",
            "oidc_client_secret_configured",
        })
        self.assertEqual(settings["general"]["app_name"], "Package Console")
        self.assertEqual(settings["general"]["url"], "https://console.example.test")
        self.assertNotIn("port", settings["general"])
        self.assertNotIn("workdir", settings["general"])
        self.assertNotIn("token", settings["github"])
        self.assertFalse(settings["github"]["token_configured"])
        self.assertNotIn("configured", settings["notifications"])
        self.assertEqual(settings["notifications"]["type"], "ntfy")
        self.assertNotIn("pocket_id_active", settings["security"])
        self.assertTrue(settings["resource_limits_status"]["valid"])
        self.assertIsNone(settings["resource_limits_status"]["diagnostic"])
        self.assertEqual(set(settings["resource_limits_status"]["capability"]), {
            "backend", "available", "reason",
        })
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
        body = {"github": {"token": "ghlocalvalue12345678901234567890"}}
        status, updated = self.request("POST", "/api/settings", body)
        self.assertEqual(status, 200)
        github = updated["settings"]["github"]
        self.assertNotIn("token", github)
        self.assertTrue(github["token_configured"])
        self.assertNotIn("ghlocalvalue", json.dumps(updated))
        secrets_path = Path(self.tmp.name) / "data" / "secrets.json"
        self.assertTrue(secrets_path.exists())
        secrets = json.loads(secrets_path.read_text())
        self.assertEqual(secrets["schema_version"], 1)
        self.assertEqual(secrets["github"]["token"], "ghlocalvalue12345678901234567890")

    def test_masked_secret_sentinel_preserves_every_existing_secret_via_api(self):
        server.update_settings({
            "github": {"token": "ghlocalvalue12345678901234567890"},
            "notifications": {"token": "old-notification-token"},
            "security": {"oidc_client_secret": "old-oidc-client-secret"},
        })
        secret_path = server.DATA / "secrets.json"
        before = secret_path.read_bytes()

        status, response = self.request("POST", "/api/settings", {
            "github": {"token": "masked"},
            "notifications": {"token": "masked"},
            "security": {"oidc_client_secret": "masked"},
        })

        self.assertEqual(status, 200)
        self.assertEqual(secret_path.read_bytes(), before)
        self.assertNotIn("masked", secret_path.read_text())
        self.assertTrue(response["settings"]["github"]["token_configured"])
        self.assertTrue(response["settings"]["notifications"]["token_configured"])
        self.assertTrue(response["settings"]["security"]["oidc_client_secret_configured"])

    def test_empty_secret_inputs_preserve_existing_values_via_api(self):
        server.update_settings({
            "github": {"token": "ghlocalvalue12345678901234567890"},
            "notifications": {"token": "old-notification-token"},
            "security": {"oidc_client_secret": "old-oidc-client-secret"},
        })
        secret_path = server.DATA / "secrets.json"
        before = secret_path.read_bytes()

        status, _response = self.request("POST", "/api/settings", {
            "github": {"token": ""},
            "notifications": {"token": ""},
            "security": {"oidc_client_secret": ""},
        })

        self.assertEqual(status, 200)
        self.assertEqual(secret_path.read_bytes(), before)

    def test_masked_oidc_secret_without_existing_value_is_rejected_via_api(self):
        with mock.patch.dict(os.environ, {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", "/api/settings", {"security": {
                    "auth_mode": "oidc",
                    "oidc_issuer": "https://id.example.test",
                    "oidc_client_id": "debbuilder",
                    "oidc_redirect_uri": "https://apt.example.test/auth/callback",
                    "oidc_client_secret": "masked",
                }})
        self.assertEqual(raised.exception.code, 422)
        error = json.loads(raised.exception.read())["error"]
        self.assertEqual(error["code"], "missing_required_secret")
        self.assertEqual(error["details"]["path"], "$.security.oidc_client_secret")
        self.assertFalse((server.DATA / "settings.json").exists())
        self.assertFalse((server.DATA / "secrets.json").exists())

    def test_late_invalid_oidc_semantics_cannot_partially_replace_github_secret(self):
        server.update_settings({"github": {"token": "gholdvalue123456789012345678901"}})
        settings_path = server.DATA / "settings.json"
        secret_path = server.DATA / "secrets.json"
        settings_before = settings_path.read_bytes()
        secrets_before = secret_path.read_bytes()

        with mock.patch.dict(os.environ, {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", "/api/settings", {
                    "github": {"token": "ghnewvalue123456789012345678901"},
                    "notifications": {"token": "new-notification-token"},
                    "security": {
                        "auth_mode": "oidc",
                        "oidc_issuer": "https://id.example.test",
                        "oidc_client_id": "debbuilder",
                        "oidc_redirect_uri": "https://apt.example.test/auth/callback",
                    },
                })

        self.assertEqual(raised.exception.code, 422)
        self.assertEqual(json.loads(raised.exception.read())["error"]["details"]["path"], "$.security.oidc_client_secret")
        self.assertEqual(settings_path.read_bytes(), settings_before)
        self.assertEqual(secret_path.read_bytes(), secrets_before)
        self.assertEqual(json.loads(secret_path.read_text())["github"]["token"], "gholdvalue123456789012345678901")

    def test_invalid_resource_update_cannot_partially_replace_secret(self):
        server.update_settings({"github": {"token": "gholdvalue123456789012345678901"}})
        settings_path = server.DATA / "settings.json"
        secret_path = server.DATA / "secrets.json"
        before = (settings_path.read_bytes(), secret_path.read_bytes())

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/settings", {
                "github": {"token": "ghnewvalue123456789012345678901"},
                "resource_limits": {"tasks_max": -1},
            })

        self.assertEqual(raised.exception.code, 422)
        self.assertEqual(
            (settings_path.read_bytes(), secret_path.read_bytes()), before,
        )

    def test_explicit_secret_replacements_are_validated_then_persisted_together(self):
        status, _response = self.request("POST", "/api/settings", {
            "github": {"token": "ghreplacement12345678901234567890"},
            "notifications": {"token": "replacement-notification-token"},
            "security": {"oidc_client_secret": "replacement-oidc-secret"},
        })
        self.assertEqual(status, 200)
        secrets = json.loads((server.DATA / "secrets.json").read_text())
        self.assertEqual(secrets["github"]["token"], "ghreplacement12345678901234567890")
        self.assertEqual(secrets["notifications"]["token"], "replacement-notification-token")
        self.assertEqual(secrets["oidc"]["client_secret"], "replacement-oidc-secret")
        self.assertNotIn("masked", json.dumps(secrets))

    def test_resource_only_repair_is_reachable_via_api_but_get_remains_fail_closed(self):
        settings = server.settings_defaults()
        settings["resource_limits"]["memory_max_bytes"] = 1073741824
        settings["resource_limits"]["tasks_max"] = "bad"
        settings_path = server.DATA / "settings.json"
        settings_path.write_text(json.dumps(settings))

        with self.assertRaises(urllib.error.HTTPError) as get_error:
            self.request("GET", "/api/settings")
        self.assertEqual(get_error.exception.code, 503)

        status, response = self.request("POST", "/api/settings", {
            "resource_limits": {"tasks_max": 48},
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["settings"]["resource_limits"]["memory_max_bytes"], 1073741824)
        self.assertEqual(response["settings"]["resource_limits"]["tasks_max"], 48)

        saved = json.loads(settings_path.read_text())
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(set(saved["resource_limits"]), {
            "memory_max_bytes", "tasks_max", "cpu_quota_percent",
            "io_read_bandwidth_max_bytes_per_sec", "io_write_bandwidth_max_bytes_per_sec",
        })

    def test_resource_repair_uses_the_stored_v1_authentication_mode(self):
        settings = server.settings_defaults()
        settings["security"]["auth_mode"] = "header"
        settings["resource_limits"]["tasks_max"] = "bad"
        (server.DATA / "settings.json").write_text(json.dumps(settings))
        payload = {"resource_limits": {"tasks_max": 24}}

        with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
            self.request("POST", "/api/settings", payload)
        self.assertEqual(unauthenticated.exception.code, 503)

        status, response = self.request(
            "POST", "/api/settings", payload, headers={server.AUTH_HEADER: "operator"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["settings"]["resource_limits"]["tasks_max"], 24)

    def test_resource_repair_prevalidates_secrets_before_writing_settings(self):
        settings = server.settings_defaults()
        settings["resource_limits"]["tasks_max"] = "bad"
        settings_path = server.DATA / "settings.json"
        settings_path.write_text(json.dumps(settings))
        secret_path = server.DATA / "secrets.json"
        secret_path.write_text('{"schema_version":1,"github":')
        secret_path.chmod(0o600)
        settings_before = settings_path.read_bytes()
        secrets_before = secret_path.read_bytes()

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/settings", {
                "resource_limits": {"tasks_max": 32},
            })

        self.assertEqual(raised.exception.code, 422)
        error = json.loads(raised.exception.read())["error"]
        self.assertEqual(error["code"], "invalid_secrets_json")
        self.assertEqual(error["details"]["path"], "$")
        self.assertEqual(settings_path.read_bytes(), settings_before)
        self.assertEqual(secret_path.read_bytes(), secrets_before)

    def test_notification_settings_reject_unknown_types(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/settings", {"notifications": {"type": "webhook"}})
        self.assertEqual(ctx.exception.code, 422)

    def test_disabled_ntfy_accepts_an_empty_configuration(self):
        status, updated = self.request("POST", "/api/settings", {
            "notifications": {"type": "none", "server_url": "", "topic": ""},
        })
        self.assertEqual(status, 200)
        notifications = updated["settings"]["notifications"]
        self.assertEqual(notifications["server_url"], "")
        self.assertEqual(notifications["topic"], "")

    def test_repo_settings_reject_secret_like_repository_values(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/settings", {"apt": {"repository": "https://example.invalid/?token=abc12345"}})
        self.assertEqual(ctx.exception.code, 422)

    def test_recipe_metadata_can_be_created_loaded_and_renamed(self):
        workflow = {"schema_version":5,"name":"flood","package":{"name":"flood"},"source":{"repository":"jesec/flood","tracking":"latest_release"},"active":True}
        status, _ = self.request("POST", "/api/workflows/flood", {"workflow": workflow})
        self.assertEqual(status, 200)
        status, loaded = self.request("GET", "/api/workflows/flood")
        self.assertEqual(loaded["package"]["name"], "flood")
        self.assertEqual(loaded["source"]["repository"], "jesec/flood")
        loaded["name"] = "flood-release"
        status, _ = self.request("POST", "/api/workflows/flood-release", {"workflow": loaded, "previous_id":"flood"})
        self.assertEqual(status, 200)
        self.assertFalse((server.USER_WORKFLOWS / "flood.json").exists())
        self.assertTrue((server.USER_WORKFLOWS / "flood-release.json").exists())

    def test_recipe_json_validation_is_canonical_and_does_not_write(self):
        recipe = {
            "schema_version": 5,
            "name": "validated-only", "package": {"name": "validated-only", "version_revision": "1+b1"},
            "build": {"inactivity_timeout": 90, "output": {"mode": "source"}},
            "install": {"directories": []},
        }
        before = list(server.USER_WORKFLOWS.iterdir())
        status, result = self.request("POST", "/api/recipes/validate", {"recipe": recipe})
        self.assertEqual(status, 200)
        self.assertEqual(result["recipe"]["package"]["version_revision"], "1+b1")
        self.assertEqual(result["recipe"]["build"]["inactivity_timeout"], 90)
        self.assertNotIn("path", result["recipe"]["build"]["output"])
        self.assertIsNone(result["collision"])
        self.assertEqual(before, list(server.USER_WORKFLOWS.iterdir()))

    def test_recipe_json_validation_reports_structured_errors(self):
        for recipe, code in (([], "invalid_root"), ({"schema_version": 5, "package": {}}, "missing_id"), ({"schema_version": 5, "name": "demo", "unknown": 1}, "unknown_field"), ({"schema_version": 5, "name": "demo", "build": []}, "invalid_recipe")):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", "/api/recipes/validate", {"recipe": recipe})
            self.assertEqual(raised.exception.code, 422)
            payload = json.loads(raised.exception.read().decode())
            self.assertEqual(payload["error"]["code"], code)
            self.assertIn("message", payload["error"])

        request = urllib.request.Request(
            self.base_url + "/api/recipes/validate",
            data=b'{"recipe":', method="POST", headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        syntax_error = json.loads(raised.exception.read().decode())["error"]
        self.assertEqual(syntax_error["code"], "invalid_json")
        self.assertEqual(syntax_error["details"]["line"], 1)

    def test_all_recipe_authoring_and_run_apis_reject_build_version_source(self):
        before_runs = {path.name for path in (server.DATA / "builds").iterdir()}
        for index, (path, body) in enumerate((
            ("/api/recipes/validate", {"recipe": None}),
            ("/api/recipes/import", {"recipe": None}),
            ("/api/workflows/unsupported-workflow", {"workflow": None}),
            ("/api/upstream-archive/inspect", {"workflow": None}),
            ("/api/run", {"workflow": None, "dry_run": True}),
        )):
            name = f"unsupported-version-{index}"
            recipe = {
                "schema_version": 5, "name": name, "active": True,
                "package": {"name": name},
                "source": {"repository": f"example/{name}", "version": {"source": "build"}},
                "artifact": {
                    "mode": "upstream_archive", "type": "archive", "archive_source": "github_source",
                    "payload": {"mode": "entire_archive"},
                },
            }
            if path == "/api/workflows/unsupported-workflow":
                recipe["name"] = "unsupported-workflow"
                recipe["package"]["name"] = "unsupported-workflow"
                recipe["source"]["repository"] = "example/unsupported-workflow"
            payload = {**body, "recipe" if path.startswith("/api/recipes/") else "workflow": recipe}
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", path, payload)
            self.assertEqual(raised.exception.code, 422)
            error = json.loads(raised.exception.read().decode())["error"]
            self.assertEqual(error["code"], "unsupported_version_source")
            if path != "/api/run":
                self.assertEqual(error["details"]["path"], "$.source.version.source")

        self.assertFalse((server.USER_WORKFLOWS / "unsupported-version-1.json").exists())
        self.assertFalse((server.USER_WORKFLOWS / "unsupported-workflow.json").exists())
        self.assertEqual({path.name for path in (server.DATA / "builds").iterdir()}, before_runs)

    def test_all_recipe_authoring_and_run_apis_reject_derived_service_configured(self):
        before_runs = {path.name for path in (server.DATA / "builds").iterdir()}
        for index, (path, body) in enumerate((
            ("/api/recipes/validate", {"recipe": None}),
            ("/api/recipes/import", {"recipe": None}),
            ("/api/workflows/derived-service-workflow", {"workflow": None}),
            ("/api/upstream-archive/inspect", {"workflow": None}),
            ("/api/run", {"workflow": None, "dry_run": True}),
        )):
            name = f"derived-service-{index}"
            recipe = {
                "schema_version": 5, "name": name, "active": True,
                "package": {"name": name},
                "source": {"repository": f"example/{name}"},
                "artifact": {
                    "mode": "upstream_archive", "type": "archive", "archive_source": "github_source",
                    "payload": {"mode": "entire_archive"},
                },
                "service": {"configured": False, "enabled": False},
            }
            if path == "/api/workflows/derived-service-workflow":
                recipe["name"] = "derived-service-workflow"
                recipe["package"]["name"] = "derived-service-workflow"
                recipe["source"]["repository"] = "example/derived-service-workflow"
            payload = {**body, "recipe" if path.startswith("/api/recipes/") else "workflow": recipe}
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", path, payload)
            self.assertEqual(raised.exception.code, 422)
            error = json.loads(raised.exception.read().decode())["error"]
            self.assertEqual(error["code"], "unknown_field")
            if path != "/api/run":
                self.assertEqual(error["details"].get("path"), "$.service.configured")

        self.assertFalse((server.USER_WORKFLOWS / "derived-service-1.json").exists())
        self.assertFalse((server.USER_WORKFLOWS / "derived-service-workflow.json").exists())
        self.assertEqual({path.name for path in (server.DATA / "builds").iterdir()}, before_runs)

    def test_recipe_json_import_requires_explicit_collision_replacement(self):
        recipe = {"schema_version": 5, "name": "imported", "package": {"name": "imported"}, "install": {"directories": []}}
        status, created = self.request("POST", "/api/recipes/import", {"recipe": recipe, "replace": False})
        self.assertEqual(status, 200)
        self.assertTrue(created["created"])
        self.assertFalse(created["replaced"])
        self.assertTrue((server.USER_WORKFLOWS / "imported.json").exists())

        replacement = {**recipe, "package": {"name": "imported", "version_revision": "1+b1"}}
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/recipes/import", {"recipe": replacement, "replace": False})
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "recipe_exists")
        self.assertEqual(json.loads((server.USER_WORKFLOWS / "imported.json").read_text())["package"]["version_revision"], "1")

        status, replaced = self.request("POST", "/api/recipes/import", {"recipe": replacement, "replace": True})
        self.assertEqual(status, 200)
        self.assertFalse(replaced["created"])
        self.assertTrue(replaced["replaced"])
        self.assertEqual(json.loads((server.USER_WORKFLOWS / "imported.json").read_text())["package"]["version_revision"], "1+b1")

    def test_recipe_json_import_rechecks_collision_after_validation(self):
        recipe = {"schema_version": 5, "name": "late-collision", "package": {"name": "late-collision"}, "install": {"directories": []}}
        status, validated = self.request("POST", "/api/recipes/validate", {"recipe": recipe})
        self.assertEqual(status, 200)
        self.assertIsNone(validated["collision"])

        appeared = server.recipe_document_for_storage({
            "schema_version": 5,
            "name": "late-collision", "package": {"name": "late-collision", "description": "Created concurrently"},
        })
        server.storage.save_json(server.USER_WORKFLOWS / "late-collision.json", appeared)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/recipes/import", {"recipe": validated["recipe"], "replace": False})
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "recipe_exists")
        persisted = json.loads((server.USER_WORKFLOWS / "late-collision.json").read_text())
        self.assertEqual(persisted["package"]["description"], "Created concurrently")

    def test_recipe_json_import_cannot_replace_shipped_recipe(self):
        recipe = {"schema_version": 5, "name": "webapp-recipe", "package": {"name": "webapp"}, "install": {"directories": []}}
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/recipes/import", {"recipe": recipe, "replace": True})
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "readonly_recipe")
        self.assertFalse((server.USER_WORKFLOWS / "webapp-recipe.json").exists())

    def test_reserved_builtin_id_cannot_be_created_imported_deleted_or_renamed(self):
        recipe = {"schema_version": 5, "name": "debbuilder", "package": {"name": "debbuilder"}}
        builtin_path = server.USER_WORKFLOWS / "debbuilder.json"
        original = builtin_path.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/recipes/import", {"recipe": recipe, "replace": True})
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "builtin_recipe_reserved")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/workflows/debbuilder", {"workflow": recipe})
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "builtin_recipe_managed_field")
        self.assertEqual(builtin_path.read_bytes(), original)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("DELETE", "/api/workflows/debbuilder")
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "builtin_recipe_reserved")

        status, _ = self.request("POST", "/api/workflows/rename-source", {
            "workflow": {"schema_version": 5, "name": "rename-source", "package": {"name": "rename-source"}},
        })
        self.assertEqual(status, 200)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/workflows/renamed", {
                "workflow": {"schema_version": 5, "name": "renamed", "package": {"name": "renamed"}},
                "previous_id": "debbuilder",
            })
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(json.loads(raised.exception.read().decode())["error"]["code"], "builtin_recipe_reserved")

    def test_workflow_url_identity_must_match_document_identity(self):
        builtin_path = server.USER_WORKFLOWS / "debbuilder.json"
        original = builtin_path.read_bytes()
        _, builtin = self.request("GET", "/api/workflows/debbuilder")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/workflows/alias", {"workflow": builtin})

        self.assertEqual(raised.exception.code, 422)
        error = json.loads(raised.exception.read().decode())["error"]
        self.assertEqual(error["code"], "recipe_identity_mismatch")
        self.assertEqual(error["details"]["path"], "$.name")
        self.assertFalse((server.USER_WORKFLOWS / "alias.json").exists())
        self.assertEqual(builtin_path.read_bytes(), original)

        ordinary = {"schema_version": 5, "name": "matching", "package": {"name": "matching"}}
        status, result = self.request("POST", "/api/workflows/matching", {"workflow": ordinary})
        self.assertEqual(status, 200)
        self.assertEqual(result["id"], "matching")
        persisted = server.recipe_store.load_recipe(server.USER_WORKFLOWS / "matching.json")
        self.assertEqual(persisted["name"], "matching")
        self.assertNotIn("management", persisted)

    def test_managed_builtin_api_allows_policy_override_and_rejects_managed_edit(self):
        server.builtin_recipe.reconcile_builtin_recipe(server.USER_WORKFLOWS)
        status, listing = self.request("GET", "/api/workflows")
        self.assertEqual(status, 200)
        listed = next(row for row in listing["workflows"] if row["id"] == "debbuilder")
        self.assertEqual(listed["source"], "builtin")
        self.assertTrue(listed["managed"])
        self.assertEqual(listed["editable_paths"], list(server.builtin_recipe.OPERATOR_OVERRIDE_PATHS))
        ordinary = next(row for row in listing["workflows"] if row["id"] == "webapp-recipe")
        self.assertEqual(ordinary["source"], "example")
        self.assertNotIn("managed", ordinary)
        self.assertNotIn("editable_paths", ordinary)
        status, viewed = self.request("GET", "/api/workflows/debbuilder")
        self.assertEqual(status, 200)
        viewed["active"] = False
        viewed["build"]["environment"] = {"HTTP_PROXY": "http://proxy.example.test"}
        status, _ = self.request("POST", "/api/workflows/debbuilder", {
            "workflow": viewed, "previous_id": "debbuilder",
        })
        self.assertEqual(status, 200)
        persisted = server.recipe_store.load_recipe(server.USER_WORKFLOWS / "debbuilder.json")
        self.assertEqual(persisted["management"]["operator_overrides"], {
            "active": False,
            "build": {"environment": {"HTTP_PROXY": "http://proxy.example.test"}},
        })

        original = (server.USER_WORKFLOWS / "debbuilder.json").read_bytes()
        _, viewed = self.request("GET", "/api/workflows/debbuilder")
        viewed["source"]["repository"] = "attacker/fork"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/workflows/debbuilder", {
                "workflow": viewed, "previous_id": "debbuilder",
            })
        self.assertEqual(raised.exception.code, 409)
        error = json.loads(raised.exception.read().decode())["error"]
        self.assertEqual(error["code"], "builtin_recipe_managed_field")
        self.assertEqual(error["details"]["path"], "$.source.repository")
        self.assertEqual((server.USER_WORKFLOWS / "debbuilder.json").read_bytes(), original)

        status, validation = self.request("POST", "/api/recipes/validate", {"recipe": viewed})
        self.assertEqual(status, 200)
        self.assertEqual(validation["collision"], {
            "exists": True, "source": "builtin", "replaceable": False,
        })

    def test_workflow_listing_reports_corrupt_recipe_without_hiding_valid_entries(self):
        (server.USER_WORKFLOWS / "broken.json").write_text('{"schema_version": 2,')

        status, listing = self.request("GET", "/api/workflows")

        self.assertEqual(status, 200)
        self.assertIn("webapp-recipe", [row["id"] for row in listing["workflows"]])
        self.assertEqual(len(listing["errors"]), 1)
        failure = listing["errors"][0]
        self.assertEqual(failure["id"], "broken")
        self.assertEqual(failure["source"], "user")
        self.assertEqual(failure["error"]["code"], "invalid_recipe_json")
        self.assertEqual(failure["error"]["path"], "$")
        self.assertNotIn(str(server.USER_WORKFLOWS), json.dumps(failure))

    def test_shipped_recipe_cannot_be_modified_through_workflow_api(self):
        status, viewed = self.request("GET", "/api/workflows/webapp-recipe")
        self.assertEqual(status, 200)
        viewed["package"]["description"] = "Direct API overwrite"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/workflows/webapp-recipe", {"workflow": viewed, "previous_id": "webapp-recipe"})
        self.assertEqual(raised.exception.code, 403)
        self.assertFalse((server.USER_WORKFLOWS / "webapp-recipe.json").exists())
        self.assertNotEqual(
            json.loads((server.EXAMPLES / "webapp-recipe.json").read_text())["package"].get("description"),
            "Direct API overwrite",
        )

    def test_recipe_json_payload_limit_is_enforced_by_server(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/api/recipes/validate")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2000001")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            error = json.loads(response.read().decode())["error"]
        finally:
            connection.close()
        self.assertEqual(error["code"], "invalid_request")

    def test_user_recipe_can_be_deleted_without_repository_or_system_deletion(self):
        workflow = {"schema_version":5,"name":"temporary","package":{"name":"temporary"},"source":{"repository":"example/temporary","tracking":"latest_release"},"active":True}
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
        packages = server.package_projection_service().load_overrides()
        self.assertNotEqual(packages["temporary"].get("recipe"), "temporary")

    def test_recipe_package_can_select_inventory_item_or_create_new_item(self):
        existing = {"schema_version":5,"name":"webapp-build","package":{"name":"webapp"},"source":{"repository":"example/webapp","tracking":"latest_release"},"active":True}
        status, _ = self.request("POST", "/api/workflows/webapp-build", {"workflow": existing})
        self.assertEqual(status, 200)
        package = server.get_package("webapp")
        self.assertEqual(package["recipe"], "")
        self.assertEqual(package["recipe_error"]["code"], "ambiguous_recipe")
        self.assertEqual(set(package["recipe_error"]["candidates"]), {"webapp-recipe", "webapp-build"})
        new = {"schema_version":5,"name":"new-app","package":{"name":"new-app"},"source":{"repository":"example/new-app","tracking":"latest_release"},"active":True}
        status, _ = self.request("POST", "/api/workflows/new-app", {"workflow": new})
        self.assertEqual(status, 200)
        created = server.get_package("new-app")
        self.assertEqual(created["recipe"], "new-app")
        self.assertEqual(created["source"]["repository"], "example/new-app")

    def test_package_projection_uses_a_disabled_recipe_only_as_the_no_enabled_recipe_fallback(self):
        disabled = {"schema_version": 5, "name": "disabled-fallback", "active": False, "package": {"name": "fallback-app"}, "source": {"repository": "example/fallback"}}
        status, _ = self.request("POST", "/api/workflows/disabled-fallback", {"workflow": disabled})
        self.assertEqual(status, 200)
        records = server.package_projection_service().recipe_records_by_package()
        self.assertEqual(records["fallback-app"]["id"], "disabled-fallback")

        enabled = {"schema_version": 5, "name": "enabled-preferred", "active": True, "package": {"name": "fallback-app"}, "source": {"repository": "example/preferred"}}
        status, _ = self.request("POST", "/api/workflows/enabled-preferred", {"workflow": enabled})
        self.assertEqual(status, 200)
        records = server.package_projection_service().recipe_records_by_package()
        self.assertEqual(records["fallback-app"]["id"], "enabled-preferred")

    def test_recipe_created_from_package_stays_linked_after_canonical_reload(self):
        status, created = self.request("POST", "/api/packages", {"name": "nested-app", "architecture": "amd64", "source": {"type": "github", "repository": "example/nested-app"}})
        self.assertEqual(status, 200)
        workflow = {
            "schema_version": 5, "name": "nested-app", "active": True,
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

    def test_local_repository_package_is_enriched_from_matching_observation(self):
        published = [{
            "Package": "flood", "Version": "4.8.2-0", "Architecture": "amd64",
            "Homepage": None, "Filename": "pool/main/f/flood/flood_4.8.2-0_amd64.deb",
            "Description": "Flood",
        }]
        storage.save_json(server.DATA / "packages.json", [{
            "name": "flood", "apt_version": "4.8.2-0", "upstream_version": "4.8.2-0",
            "source": {"type": "apt-repository", "repository": ""}, "recipe": "flood",
        }])
        (server.USER_WORKFLOWS / "flood.json").write_text(json.dumps({
            "schema_version": 5,
            "name": "flood", "package": {"name": "flood"},
            "source": {"repository": "jesec/flood", "tracking": "latest_release", "version": {"source": "tag"}}, "active": True,
        }))
        recipe = server.read_workflow_file(server.USER_WORKFLOWS / "flood.json")
        recipe_sha = canonical_recipe_sha256(recipe)
        server.upstream_observation_service().store.record_success(
            "flood", recipe_sha, {"display_version": "4.9.0", "display_ref": "v4.9.0"},
        )
        with mock.patch("debbuilder.app.live_published_index", return_value=published):
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
        workflow = {"schema_version":5,"name":"empty","package":{"name":"empty"},"source":{"repository":"example/empty","tracking":"latest_release","version":{"source":"tag"}},"active":True}
        self.request("POST", "/api/workflows/empty", {"workflow": workflow})
        _, loaded = self.request("GET", "/api/workflows/empty")
        self.assertNotIn("steps", loaded)
        duplicate = {**loaded, "name":"empty-copy", "package":{**loaded["package"], "name":"empty-copy"}, "source":{**loaded["source"], "repository":"example/empty-copy"}}
        self.request("POST", "/api/workflows/empty-copy", {"workflow": duplicate})
        _, copied = self.request("GET", "/api/workflows/empty-copy")
        self.assertNotIn("steps", copied)
        self.assertEqual(copied["source"]["version"]["source"], "tag")

    def test_recipe_v5_is_stored_canonically_and_loaded_with_full_sections(self):
        recipe = {
            "schema_version": 5, "name": "v5-demo", "active": True,
            "package": {"name": "v5-demo", "architecture": "all", "runtime_dependencies": ["python3"]},
            "source": {"provider": "github", "repository": "example/v5-demo", "tracking": "latest_release", "version": {"source": "tag"}},
            "build": {"extra_dependencies": ["python3-dev"], "commands": ["python3 -m build"], "working_directory": ".", "output": {"mode": "path", "path": "dist"}},
            "install": {"destination": "/opt/v5-demo", "owner": {"user": "root", "group": "root"}, "config_files": [{"source": "demo.conf", "destination": "/etc/v5-demo.conf", "policy": "replace"}]},
            "service": {"name": "v5-demo.service", "user": "v5-demo", "group": "v5-demo", "command": "/usr/bin/python3 /opt/v5-demo/server.py"},
        }
        status, _ = self.request("POST", "/api/workflows/v5-demo", {"workflow": recipe})
        self.assertEqual(status, 200)
        stored = json.loads((server.USER_WORKFLOWS / "v5-demo.json").read_text())
        self.assertEqual(stored["schema_version"], 5)
        self.assertEqual(stored["runtime_apt_repositories"], [])
        self.assertNotIn("package_name", stored)
        self.assertNotIn("github_repository", stored)
        self.assertNotIn("config_policy", stored["install"])
        self.assertEqual(stored["install"]["config_files"][0]["policy"], "replace")
        self.assertNotIn("configured", stored["service"])
        _, loaded = self.request("GET", "/api/workflows/v5-demo")
        self.assertEqual(loaded["build"]["extra_dependencies"], ["python3-dev"])
        self.assertEqual(loaded["install"]["owner"]["user"], "root")
        self.assertEqual(loaded["service"]["user"], "v5-demo")
        self.assertNotIn("configured", loaded["service"])

    def test_recipe_import_rejects_unversioned_and_v0_through_v4(self):
        for version in (None, 0, 1, 2, 3, 4):
            recipe = {"name": f"old-{version}"}
            if version is not None:
                recipe["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("POST", "/api/recipes/import", {"recipe": recipe})
            self.assertEqual(raised.exception.code, 422)
            error = json.loads(raised.exception.read())["error"]
            self.assertEqual(error["code"], "unsupported_recipe_schema")
            self.assertEqual(error["details"]["path"], "$.schema_version")

    def test_readonly_recipe_cannot_be_deleted(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("DELETE", "/api/workflows/webapp-recipe")
        self.assertEqual(ctx.exception.code, 403)
        self.assertTrue((server.EXAMPLES / "webapp-recipe.json").exists())

    def test_disabled_recipe_cannot_run_via_direct_test_or_build_api(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request("POST", "/api/run", {"workflow":{"schema_version":5,"name":"disabled","active":False}, "dry_run":dry_run})
            self.assertEqual(ctx.exception.code, 409)

    def test_inline_run_rejects_old_schema_before_creating_a_run(self):
        before = set((server.DATA / "builds").iterdir())
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", "/api/run", {"workflow": {"schema_version": 4, "name": "old-inline"}, "dry_run": True})
        self.assertEqual(raised.exception.code, 422)
        self.assertEqual(json.loads(raised.exception.read())["error"]["code"], "unsupported_recipe_schema")
        self.assertEqual(set((server.DATA / "builds").iterdir()), before)

    def test_real_build_uses_structured_v5_pipeline(self):
        workflow = {
            "schema_version": 5, "name": "enabled", "active": True,
            "source": {"repository": "example/enabled", "tracking": "manual", "ref": "v1.0.0"},
        }
        execute, finished = self.terminal_executor("success")
        with mock.patch("debbuilder.app.execute_queued_recipe_run", side_effect=execute):
            status, response = self.request("POST", "/api/run", {"workflow": workflow, "dry_run": False})
            self.assertTrue(finished.wait(2))
        self.assertEqual(status, 202)
        self.assertEqual(response["status"], "queued")
        self.assertEqual(BuildStore(server.DATA / "builds").load(response["run_id"])["mode"], "build")

    def test_auto_validation_does_not_run_after_dry_run(self):
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True}})
        store = BuildStore(server.DATA / "builds")
        run = store.create({"schema_version": 5, "name": "dry-auto", "package": {"name": "dry-auto"}}, mode="dry_run", run_id="dry-run")
        with mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit") as validate:
            response = self.complete_manual_run(run["id"])
        self.assertEqual(response["run_id"], run["id"])
        validate.assert_not_called()

    def test_successful_real_build_auto_validates_latest_artifact_when_enabled(self):
        store, run, artifact = self.successful_build_run(run_id="latest-auto-run", package="latest-auto", version="2.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": False}})

        validation = {"id": "auto-validation", "attempt_id": "auto-validation", "build_run_id": run["id"], "status": "queued"}
        with mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit", return_value=validation) as validate_mock, mock.patch("debbuilder.app.publish_build_artifact") as publish:
            response = self.complete_manual_run(run["id"])
        validate_mock.assert_called_once_with("latest-auto-run", {}, automatic=True, publish_after_success=False)
        publish.assert_not_called()
        self.assertEqual(response["validation"]["status"], "queued")
        self.assertEqual(store.load("latest-auto-run").get("validations"), None)

    def test_auto_validation_uses_returned_build_run_not_previous_artifact(self):
        store, old, old_artifact = self.successful_build_run(run_id="old-auto-run", package="same-auto", version="1.0-1")
        old["created_at"] = "2026-01-01T00:00:00+00:00"
        store.save(old)
        record_canonical_validation(store, old, attempt_id="old-validation")
        _, current, current_artifact = self.successful_build_run(run_id="current-auto-run", package="same-auto", version="2.0-1")
        current["created_at"] = "2026-01-02T00:00:00+00:00"
        store.save(current)
        server.update_settings({"automation": {"auto_validate_after_successful_build": True}})

        validation = {"id": "current-validation", "attempt_id": "current-validation", "build_run_id": "current-auto-run", "status": "queued"}
        with mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit", return_value=validation) as validate_mock:
            self.complete_manual_run(current["id"])
        validate_mock.assert_called_once_with("current-auto-run", {}, automatic=True, publish_after_success=False)
        self.assertEqual(len(server.validation_service.list_attempts(store, "old-auto-run")), 1)
        self.assertIsNone(store.load("current-auto-run").get("validations"))

    def test_auto_validation_failure_records_validation_failed_lifecycle(self):
        store, run, artifact = self.successful_build_run(run_id="failed-auto-run", package="failed-auto", version="3.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True}})

        validation = {"id": "failed-validation", "build_run_id": run["id"], "status": "failed", "error": {"message": "admission failed"}}
        with mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit", return_value=validation):
            response = self.complete_manual_run(run["id"])
        self.assertEqual(response["validation"]["status"], "failed")

    def test_auto_publish_runs_after_successful_auto_validation_when_enabled(self):
        store, run, artifact = self.successful_build_run(run_id="publish-auto-run", package="publish-auto", version="4.0-1")
        server.update_settings({"automation": {"auto_validate_after_successful_build": True, "auto_publish_after_successful_validation": True}})

        validation = {"id": "publish-validation", "attempt_id": "publish-validation", "build_run_id": run["id"], "status": "queued"}
        with mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit", return_value=validation) as validate_mock, mock.patch("debbuilder.app.publish_build_artifact") as publish_mock:
            response = self.complete_manual_run(run["id"])
        validate_mock.assert_called_once_with("publish-auto-run", {}, automatic=True, publish_after_success=True)
        publish_mock.assert_not_called()
        self.assertEqual(response["validation"]["status"], "queued")
        self.assertNotIn("publication", response)

    def test_shutdown_admission_prevents_new_automatic_publication(self):
        store, run, _artifact = self.successful_build_run(
            run_id="shutdown-auto-run", package="shutdown-auto", version="5.0-1",
        )
        gate = MutationGate()
        gate.begin_shutdown()
        settings = {"automation": {
            "auto_validate_after_successful_build": True,
            "auto_publish_after_successful_validation": True,
        }}
        with mock.patch.object(server, "APPLICATION_MUTATION_GATE", gate), \
                mock.patch.object(server.APPLICATION_VALIDATION_MANAGER, "admit", return_value={"status": "queued"}) as validate, \
                mock.patch("debbuilder.app.publish_build_artifact") as publish:
            result = server.run_post_build_automation(
                run["id"], dry_run=False, settings=settings, store=store,
            )
        validate.assert_called_once_with(run["id"], {}, automatic=True, publish_after_success=True)
        publish.assert_not_called()
        self.assertEqual(result["validation"]["status"], "queued")
        self.assertIsNone(result["publication"])

    def test_dry_run_creates_structured_workspace_and_is_visible_in_logs(self):
        workflow = {
            "schema_version": 5,
            "name": "structured", "source": {
                "repository": "example/structured", "tracking": "manual", "ref": "v1.0.0",
            }, "active": True,
            "package": {"name": "structured", "maintainer": "Test <test@example.test>", "description": "Structured test package"},
        }
        def acquire(_recipe, workspace, token="", expected_identity=None):
            source = Path(workspace) / "source"
            (source / "requirements.txt").write_text("requests\n")
            return {"repository":"example/structured","ref":"v1.0.0","tag":"v1.0.0","upstream_version":"1.0.0","debian_version":"1.0.0-1","source_directory":str(source)}
        dependency_state = {"detected":["python3","python3-pip"],"manually_added":[],"required":["python3","python3-pip"],"available":["python3","python3-pip"],"missing":[],"checks":[],"installation_attempted":False}
        with mock.patch("debbuilder.build_pipeline.source_acquisition.acquire_source", side_effect=acquire), mock.patch("debbuilder.build_pipeline.dependency_checker.check_dependencies", return_value=dependency_state):
            status, result = self.request("POST", "/api/run", {"workflow": workflow, "dry_run": True})
            run = self.wait_for_run(result["run_id"])
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(run["status"], "prepared")
        workspace = Path(run["workspace"])
        self.assertEqual(workspace.parent, server.DATA / "builds")
        self.assertTrue((workspace / "recipe.json").exists())
        self.assertEqual([step["status"] for step in run["steps"][:4]], ["success"] * 4)
        self.assertEqual(run["steps"][4]["status"], "skipped")
        self.assertEqual([step["status"] for step in run["steps"][5:]], ["success", "success", "skipped", "skipped", "skipped"])
        _, executions = self.request("GET", "/api/executions")
        row = next(item for item in executions["executions"] if item["id"] == result["run_id"])
        self.assertEqual(row["status"], "prepared")
        _, detail = self.request("GET", f"/api/executions/{result['run_id']}")
        self.assertNotIn("recipe_sha256", detail["execution"])
        self.assertNotIn("workspace", detail["execution"])
        self.assertNotIn("schema_version", detail["execution"])
        self.assertNotIn("log", detail["execution"])
        _, log = self.request("GET", f"/api/executions/{result['run_id']}/logs?verbosity=raw")
        self.assertIn("snapshot", log["log"]["text"])

    def test_successful_build_artifact_validation_returns_async_admission(self):
        validation = {"id": "validation-one", "attempt_id": "validation-one", "build_run_id": "run-one", "status": "queued"}
        with mock.patch("debbuilder.app.admit_validation_attempt", return_value=validation) as validate:
            status, result = self.request("POST", "/api/executions/run-one/validate", {"previous_artifact": ""})
        self.assertEqual(status, 202)
        self.assertEqual(result["validation"]["status"], "queued")
        validate.assert_called_once_with(self.httpd.validation_manager, "run-one", {"previous_artifact": ""})

    def test_validation_admission_status_and_cancellation_http_lifecycle(self):
        store, run, artifact = self.successful_build_run(run_id="async-validation-run", package="async-validation")
        metadata = dependency_preparation.ArtifactMetadata(
            artifact, "async-validation", "1.0-1", "all", "", "", artifact.stat().st_size, "b" * 64,
        )
        entered = threading.Event()

        def execute(_run_id, _attempt_id, event, _automation):
            entered.set()
            event.wait(3)

        image = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None}
        with mock.patch.object(self.httpd.validation_manager, "execute", side_effect=execute), \
                mock.patch("debbuilder.validation_service.admitted_image", return_value=image), \
                mock.patch("debbuilder.validation_service.dependency_preparation.inspect_artifact", return_value=metadata):
            status, admitted = self.request("POST", f"/api/executions/{run['id']}/validate", {})
            self.assertEqual(status, 202)
            validation = admitted["validation"]
            self.assertTrue(entered.wait(2))
            attempt_path = store.run_dir(run["id"]) / "manifests/validation-attempts" / validation["attempt_id"] / "attempt.json"
            self.assertTrue(attempt_path.is_file())
            before = attempt_path.read_bytes()
            status, observed = self.request("GET", validation["status_url"])
            self.assertEqual(status, 200)
            self.assertEqual(observed["validation"]["attempt_id"], validation["attempt_id"])
            self.assertEqual(attempt_path.read_bytes(), before)
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                self.request("GET", "/api/executions/%2F/validations/invalid")
            self.assertEqual(invalid.exception.code, 400)
            invalid_error = json.loads(invalid.exception.read())["error"]
            self.assertEqual(invalid_error["code"], "invalid_validation_identity")
            self.assertEqual(invalid_error["details"], {})
            self.assertEqual(attempt_path.read_bytes(), before)
            status, cancelled = self.request("POST", validation["cancel_url"], {})
            self.assertEqual(status, 200)
            self.assertEqual(cancelled["validation"]["status"], "cancelled")
            status, repeated = self.request("POST", validation["cancel_url"], {})
            self.assertEqual(status, 200)
            self.assertEqual(repeated["validation"]["status"], "cancelled")
        self.assertEqual(store.load(run["id"])["status"], "success")

    def test_invalid_validation_admission_has_no_phantom_attempt(self):
        store, run, _artifact = self.successful_build_run(run_id="invalid-validation-run", package="invalid-validation")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("POST", f"/api/executions/{run['id']}/validate", {"unexpected": True})
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(json.loads(raised.exception.read())["error"]["details"], {})
        root = store.run_dir(run["id"]) / "manifests/validation-attempts"
        self.assertEqual(list(root.iterdir()) if root.exists() else [], [])

    def test_validation_worker_orchestrates_preparation_then_offline_lifecycle(self):
        store = BuildStore(server.DATA / "builds")
        configured = {
            "schema_version": 5,
            "name": "cp1c-orchestration",
            "package": {"name": "cp1c-orchestration"},
            "source": {"repository": "owner/cp1c-orchestration"},
        }
        run = store.create(configured, mode="build", run_id="cp1c-orchestration-run")
        artifact = Path(run["workspace"]) / "artifacts/cp1c-orchestration_1.0-1_all.deb"
        artifact.write_bytes(b"controlled")
        run["status"] = "success"
        run["artifact"] = {"path": str(artifact), "name": artifact.name, "inspection": {
            "maintainer_scripts": [], "package": "cp1c-orchestration", "version": "1.0-1",
        }}
        store.save(run)
        prepared = {"packages": [], "profile_name": "bookworm", "image": {"id": "sha256:" + "a" * 64}}
        validation = {"id": "attempt", "status": "success", "checks": [], "commands": [], "error": None}
        notifier = mock.Mock()
        notifier.notify_validation_result.side_effect = RuntimeError("notification unavailable")
        with mock.patch("debbuilder.app.dependency_preparation.prepare_runtime_dependencies", return_value={"prepared_dependencies": prepared}) as prepare, \
                mock.patch("debbuilder.app.validation_service.load_prepared", return_value=prepared), \
                mock.patch("debbuilder.app.dependency_preparation.begin_lifecycle_attempt") as begin, \
                mock.patch("debbuilder.app.dependency_preparation.complete_lifecycle_attempt") as complete, \
                mock.patch("debbuilder.app.dependency_preparation.SUPERVISOR.register") as register, \
                mock.patch("debbuilder.app.dependency_preparation.SUPERVISOR.unregister") as unregister, \
                mock.patch("debbuilder.app.artifact_validation.validate_artifact", return_value=validation) as lifecycle, \
                mock.patch("debbuilder.app.validation_service.load_automation", return_value={"automatic": True, "publish_after_success": True}) as load_automation, \
                mock.patch("debbuilder.app.publish_build_artifact", return_value={"status": "success"}) as publish, \
                mock.patch("debbuilder.app.notification_service", return_value=notifier), \
                mock.patch("debbuilder.app.request_maintenance"):
            admitted = {
                "inputs": {"profile": "bookworm", "previous_artifact": None},
                "status": "success",
            }
            with mock.patch("debbuilder.app.validation_service.load_attempt", return_value=admitted):
                result = server.execute_validation_attempt(run["id"], "attempt", __import__("threading").Event(), {})
        self.assertIs(result, validation)
        self.assertEqual(result["dependency_preparation"]["package_count"], 0)
        self.assertEqual(prepare.call_args.kwargs["repositories"], [])
        self.assertEqual(prepare.call_args.kwargs["current_artifact"], artifact)
        self.assertEqual(lifecycle.call_args.kwargs["prepared_dependencies"], prepared)
        self.assertEqual(lifecycle.call_args.kwargs["attempt_id"], "attempt")
        self.assertEqual(prepare.call_args.args, (run["id"], "attempt"))
        begin.assert_called_once()
        complete.assert_called_once()
        register.assert_called_once()
        unregister.assert_called_once()
        notifier.notify_validation_result.assert_called_once_with(validation)
        self.assertEqual(load_automation.call_count, 2)
        self.assertEqual(load_automation.call_args.args[0].root, store.root)
        self.assertEqual(load_automation.call_args.args[1:], (run["id"], "attempt"))
        publish.assert_called_once_with(run["id"], {"confirm": "publish:cp1c-orchestration:1.0-1"})

    def test_oidc_settings_round_trip_without_exposing_secret(self):
        payload = {
            "security": {"auth_mode": "oidc", "oidc_issuer": "https://id.example.test", "oidc_client_id": "debbuilder", "oidc_redirect_uri": "https://apt.example.test/auth/callback", "oidc_client_secret": "very-private-client-secret"},
        }
        view = server.update_settings(payload)
        self.assertEqual(view["security"]["auth_mode"], "oidc")
        self.assertTrue(view["security"]["oidc_client_secret_configured"])
        self.assertNotIn("very-private", json.dumps(view))
        self.assertNotIn("build", view)
        server.update_settings({"security": {
            "auth_mode": view["security"]["auth_mode"],
            "oidc_issuer": view["security"]["oidc_issuer"],
            "oidc_client_id": view["security"]["oidc_client_id"],
            "oidc_redirect_uri": view["security"]["oidc_redirect_uri"],
            "oidc_client_secret": "",
        }})
        self.assertEqual(server.oidc_client_secret(server.DATA), "very-private-client-secret")
        disabled = server.update_settings({"security": {"auth_mode": "none", "oidc_issuer": "", "oidc_client_id": "", "oidc_redirect_uri": ""}})
        self.assertEqual(disabled["security"]["auth_mode"], "none")

    def test_cookie_secret_is_generated_once_and_persisted(self):
        with mock.patch.dict(os.environ, {"DEBBUILDER_COOKIE_SECRET": ""}):
            prepared = server.prepare_cookie_secret(server.DATA)
            first = server.cookie_secret(server.DATA)
            server.prepare_cookie_secret(server.DATA)
            second = server.cookie_secret(server.DATA)
        self.assertEqual(prepared, first)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 40)
        self.assertEqual((server.DATA / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_oidc_get_and_head_do_not_create_missing_session_secret(self):
        secret_path = server.DATA / "secrets.json"
        secret_path.unlink(missing_ok=True)
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }

        with mock.patch.object(server, "effective_security", return_value=oidc):
            for method, path in (("GET", "/api/settings"), ("HEAD", "/")):
                with self.subTest(method=method):
                    conn = http.client.HTTPConnection(
                        "127.0.0.1", self.httpd.server_address[1], timeout=5,
                    )
                    conn.request(method, path)
                    response = conn.getresponse()
                    body = response.read().decode()
                    conn.close()
                    self.assertEqual(response.status, 503)
                    self.assertNotIn("secret", body.lower())
                    self.assertFalse(secret_path.exists())

    def test_oidc_request_time_corrupt_session_secret_fails_closed_without_repair(self):
        secret_path = server.DATA / "secrets.json"
        corrupt = b'{"session":{"cookie_secret":"PRIVATE_TEST_VALUE"}'
        secret_path.write_bytes(corrupt)
        secret_path.chmod(0o600)
        oidc = {
            "auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test",
            "oidc_client_id": "debbuilder",
            "oidc_redirect_uri": "https://apt.example.test/auth/callback",
        }

        with mock.patch.object(server, "effective_security", return_value=oidc):
            conn = http.client.HTTPConnection(
                "127.0.0.1", self.httpd.server_address[1], timeout=5,
            )
            conn.request("GET", "/api/settings")
            response = conn.getresponse()
            body = response.read().decode()
            conn.close()

        self.assertEqual(response.status, 503)
        self.assertEqual(secret_path.read_bytes(), corrupt)
        self.assertNotIn("PRIVATE_TEST_VALUE", body)
        self.assertNotIn(str(secret_path), body)

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

    def test_removed_legacy_package_lifecycle_endpoints_return_not_found(self):
        status, created = self.request("POST", "/api/packages", {
            "name": "download-ui",
            "architecture": "amd64",
            "apt_version": "1.0.0",
            "source": {"type": "github", "repository": "example/download-ui"},
        })
        self.assertEqual(status, 200)
        for action in ("refresh-source", "check-updates", "verify-deb", "publish"):
            with self.subTest(action=action), self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request("POST", f"/api/packages/download-ui/{action}", {})
            self.assertEqual(ctx.exception.code, 404)

    def test_static_files_cannot_escape_into_a_prefix_collision_sibling(self):
        sibling = server.STATIC.parent / f"{server.STATIC.name}_backup"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("not public")
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        connection.request("GET", "/../static_backup/secret.txt")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        self.assertNotIn(b"not public", response.read())
        connection.close()

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
