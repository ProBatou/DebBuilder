import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from debbuilder import app, maintenance, storage_inventory, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.lifecycle import MutationGate
from debbuilder.storage_inventory import StorageInventory


class FakeInventory:
    def __init__(self):
        self.collected = threading.Event()
        self.calls = 0

    def collect(self):
        self.calls += 1
        self.collected.set()
        return {"state": "ready"}


class StorageMaintenanceTests(unittest.TestCase):
    def test_history_cleanup_refreshes_inventory_without_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, repo = base / "data", base / "repo"
            data.mkdir()
            repo.mkdir()
            (repo / "published.deb").write_bytes(b"published")
            store = BuildStore(data / "builds")
            run = store.create({
                "schema_version": 5, "name": "history", "active": True,
                "package": {"name": "history", "maintainer": "A <a@example.test>", "description": "History"},
                "source": {"repository": "owner/history"},
            }, mode="build", run_id="history")
            run["status"] = "failed"
            store.save(run)
            inventory = StorageInventory(data, repo)
            before = inventory.collect()
            self.assertEqual(before["runs"]["count"], 1)
            service = maintenance.MaintenanceService(inventory, refresh_interval=3600)
            service.start()
            try:
                store.clear_log_history(run["id"])
                service.request(refresh=True)
                self.assertEqual(inventory.snapshot()["state"], "collecting")
                deadline = time.monotonic() + 3
                while inventory.snapshot()["runs"]["count"] != 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                after = inventory.snapshot()
                self.assertEqual(after["runs"]["count"], 0)
                self.assertEqual(after["bytes"]["repository"], before["bytes"]["repository"])
                self.assertFalse(inventory.needs_refresh())
            finally:
                service.stop()
                service.join(2)

    def test_application_maintenance_holds_mutation_lease_before_cleanup(self):
        inventory = FakeInventory()
        gate = MutationGate()
        server = SimpleNamespace(
            cleanup_authorization=workspace_cleanup.CleanupAuthorization(),
            mutation_gate=gate,
            storage_inventory=inventory,
        )
        called = threading.Event()

        def maintain(*, authorization, should_stop, inventory):
            self.assertIs(authorization, server.cleanup_authorization)
            self.assertIs(inventory, server.storage_inventory)
            self.assertEqual(gate.active, 1)
            self.assertFalse(should_stop())
            called.set()

        with mock.patch.object(app, "maintain_run_storage", side_effect=maintain):
            service = app.create_maintenance_service(server)
            service.request(cleanup=True)
            service.start()
            self.assertTrue(called.wait(2))
            service.stop()
            service.join(2)

        self.assertEqual(gate.active, 0)
        self.assertFalse(service.is_alive())

    def test_closed_mutation_gate_prevents_new_cleanup_pass(self):
        inventory = FakeInventory()
        gate = MutationGate()
        server = SimpleNamespace(
            cleanup_authorization=workspace_cleanup.CleanupAuthorization(),
            mutation_gate=gate,
            storage_inventory=inventory,
        )
        gate.begin_shutdown()
        with mock.patch.object(app, "maintain_run_storage") as maintain:
            service = app.create_maintenance_service(server)
            service.request(cleanup=True)
            service.start()
            self.assertTrue(inventory.collected.wait(2))
            service.stop()
            service.join(2)

        maintain.assert_not_called()
        self.assertFalse(service.is_alive())

    def test_request_returns_while_inventory_collection_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()
        cleanup = mock.Mock()

        class BlockingInventory:
            def collect(self):
                entered.set()
                release.wait(3)

        service = maintenance.MaintenanceService(
            BlockingInventory(), cleanup=cleanup, refresh_interval=3600,
        )
        service.start()
        self.assertTrue(entered.wait(2))

        requester = threading.Thread(target=lambda: service.request(cleanup=True))
        requester.start()
        requester.join(0.5)

        self.assertFalse(requester.is_alive())
        cleanup.assert_not_called()
        service.stop()
        release.set()
        service.join(2)
        self.assertFalse(service.is_alive())

    def test_stop_discards_a_pending_cleanup_request(self):
        entered = threading.Event()
        release = threading.Event()
        cleanup = mock.Mock()

        class BlockingInventory:
            def __init__(self):
                self.calls = 0

            def collect(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(3)

        inventory = BlockingInventory()
        service = maintenance.MaintenanceService(
            inventory, cleanup=cleanup, refresh_interval=3600,
        )
        service.start()
        self.assertTrue(entered.wait(2))
        service.request(cleanup=True)
        service.stop()
        release.set()
        service.join(2)

        cleanup.assert_not_called()
        self.assertEqual(inventory.calls, 1)
        self.assertFalse(service.is_alive())

    def test_periodic_interval_requests_cleanup_and_inventory_on_one_worker(self):
        inventory = FakeInventory()
        cleanup_called = threading.Event()
        cleanup = mock.Mock(side_effect=cleanup_called.set)
        service = maintenance.MaintenanceService(
            inventory, cleanup=cleanup, refresh_interval=0.03,
        )

        service.start()
        self.assertTrue(inventory.collected.wait(2))
        self.assertEqual(
            sum(thread.name == "storage-maintenance" for thread in threading.enumerate()),
            1,
        )
        self.assertTrue(cleanup_called.wait(2))
        deadline = time.monotonic() + 2
        while inventory.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        service.join(2)

        self.assertGreaterEqual(inventory.calls, 2)
        self.assertGreaterEqual(cleanup.call_count, 1)
        self.assertEqual(
            sum(thread.name == "storage-maintenance" for thread in threading.enumerate()),
            0,
        )

    def test_request_storm_is_bounded_to_one_followup_pass(self):
        inventory = FakeInventory()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        calls = 0
        service = None

        def cleanup():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                release_first.wait(2)
            elif calls == 2:
                # A request emitted by the follow-up itself must not form an
                # unbounded chain of immediately repeated scans.
                service.request(cleanup=True)
                second_entered.set()

        service = maintenance.MaintenanceService(
            inventory, cleanup=cleanup, refresh_interval=3600,
        )
        service.request(cleanup=True)
        service.start()
        self.assertTrue(first_entered.wait(2))
        for _index in range(100):
            service.request(cleanup=True)
        release_first.set()
        self.assertTrue(second_entered.wait(2))
        time.sleep(0.05)
        service.stop()
        service.join(2)

        self.assertEqual(calls, 2)
        self.assertFalse(service.is_alive())

    def test_cleanup_failure_does_not_kill_periodic_worker(self):
        inventory = FakeInventory()
        recovered = threading.Event()
        calls = 0

        def cleanup():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("isolated cleanup failure")
            recovered.set()

        service = maintenance.MaintenanceService(
            inventory, cleanup=cleanup, refresh_interval=0.03,
        )
        with self.assertLogs("debbuilder.maintenance", level="ERROR"):
            service.start()
            service.request(cleanup=True)
            self.assertTrue(recovered.wait(2))
        self.assertTrue(service.is_alive())
        service.stop()
        service.join(2)

    def test_application_cleanup_observes_service_stop_signal(self):
        inventory = FakeInventory()
        server = SimpleNamespace(
            cleanup_authorization=workspace_cleanup.CleanupAuthorization(),
            storage_inventory=inventory,
        )
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        observed_stop = []

        def cleanup(*, authorization, should_stop, policy):
            self.assertIs(authorization, server.cleanup_authorization)
            self.assertTrue(policy["enabled"])
            cleanup_entered.set()
            release_cleanup.wait(2)
            observed_stop.append(should_stop())

        with mock.patch.object(app, "cleanup_workspaces", side_effect=cleanup):
            service = app.create_maintenance_service(server)
            service.request(cleanup=True)
            service.start()
            self.assertTrue(cleanup_entered.wait(2))
            service.stop()
            release_cleanup.set()
            service.join(2)

        self.assertEqual(observed_stop, [True])
        self.assertFalse(service.is_alive())

    def test_one_non_daemon_worker_refreshes_and_stops_cleanly(self):
        inventory = FakeInventory()
        cleanup = mock.Mock()
        service = maintenance.MaintenanceService(
            inventory, cleanup=cleanup, refresh_interval=3600,
        )

        service.start()
        self.assertTrue(inventory.collected.wait(2))
        self.assertIsNotNone(service.thread)
        self.assertFalse(service.thread.daemon)
        with self.assertRaisesRegex(RuntimeError, "already started"):
            service.start()
        service.request(cleanup=True)
        deadline = time.monotonic() + 2
        while cleanup.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        service.join(2)

        self.assertEqual(cleanup.call_count, 1)
        self.assertFalse(service.is_alive())

    def test_recovery_blocked_service_observes_but_cannot_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = BuildStore(base / "data/builds")
            run = store.create({
                "schema_version": 5,
                "name": "blocked",
                "package": {"name": "blocked", "maintainer": "A <a@example.test>", "description": "A"},
                "source": {"repository": "owner/blocked"},
            }, mode="build", run_id="blocked")
            source = Path(run["workspace"]) / "source/keep"
            source.write_text("keep")
            run["status"] = "success"
            store.save(run)
            authorization = workspace_cleanup.CleanupAuthorization({
                "code": "execution_recovery_unresolved",
                "message": "blocked by recovery",
            })
            inventory = StorageInventory(base / "data", base / "repo")
            cleanup_finished = threading.Event()

            def cleanup():
                try:
                    return workspace_cleanup.apply_retention(
                        store, authorization=authorization,
                    )
                finally:
                    cleanup_finished.set()

            service = maintenance.MaintenanceService(
                inventory,
                cleanup=cleanup,
                refresh_interval=3600,
            )
            service.start()
            service.request(cleanup=True)
            self.assertTrue(cleanup_finished.wait(2))
            service.stop()
            service.join(2)

            self.assertIn(inventory.snapshot()["state"], {"ready", "partial"})
            self.assertTrue(source.is_file())

    def test_execution_completion_only_requests_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create({
                "schema_version": 5,
                "name": "queued",
                "package": {"name": "queued", "maintainer": "A <a@example.test>", "description": "A"},
                "source": {"repository": "owner/queued"},
            }, mode="build", run_id="queued")
            request = mock.Mock()
            service = mock.Mock()
            service.request = request

            def execute(run_id, **_kwargs):
                return {"run_id": run_id, "status": "success"}

            with mock.patch.object(app.build_pipeline, "execute_pipeline_run", side_effect=execute), \
                    mock.patch.object(app.automation_service, "complete_with_automation", side_effect=lambda result, **_kwargs: result), \
                    mock.patch.object(app, "APPLICATION_MAINTENANCE_SERVICE", service), \
                    mock.patch.object(app.workspace_cleanup, "apply_retention") as sweep:
                result = app.execute_queued_recipe_run(
                    run["id"], store=store, expected_initial_status="pending",
                    storage_inventory=mock.Mock(
                        collect_capacity=mock.Mock(return_value={
                            "builds": {"pressure_state": "normal"},
                        }),
                    ),
                )

            self.assertEqual(result["status"], "success")
            request.assert_called_once_with(refresh=True, cleanup=True)
            sweep.assert_not_called()

    def test_application_maintenance_orders_cleanup_before_pruning(self):
        calls = []
        authorization = workspace_cleanup.CleanupAuthorization()
        with mock.patch.object(app, "cleanup_workspaces", side_effect=lambda **_kwargs: calls.append("cleanup") or {"errors": []}), \
                mock.patch.object(app.storage_pruning, "apply_pruning", side_effect=lambda *_args, **_kwargs: calls.append("pruning") or {"errors": []}), \
                mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": {"enabled": True, "failed_workspaces_to_retain": 5}}):
            result = app.maintain_run_storage(authorization=authorization)

        self.assertEqual(calls, ["cleanup", "pruning"])
        self.assertIn("workspace_cleanup", result)
        self.assertIn("storage_pruning", result)
        self.assertIn("storage_pressure", result)

    def test_application_maintenance_orders_pressure_after_retention_and_pruning(self):
        calls = []
        inventory = mock.Mock()
        inventory.collect_capacity.return_value = {
            "builds": {
                "measurement_state": "ready",
                "pressure_state": "pressure",
                "available_bytes": 50,
                "effective_target_bytes": 200,
            },
            "repository": {
                "measurement_state": "ready",
                "pressure_state": "normal",
                "same_as_builds": False,
            },
        }
        policy = {"enabled": True, "failed_workspaces_to_retain": 5}
        authorization = workspace_cleanup.CleanupAuthorization()
        with mock.patch.object(
            app, "cleanup_workspaces",
            side_effect=lambda **_kwargs: calls.append("retention") or {
                "retained": ["failed"], "errors": [],
            },
        ), mock.patch.object(
            app.storage_pruning, "apply_pruning",
            side_effect=lambda *_args, **_kwargs: calls.append("pruning") or {"errors": []},
        ), mock.patch.object(
            app.workspace_cleanup, "apply_storage_pressure",
            side_effect=lambda *_args, **_kwargs: calls.append("pressure") or {"errors": []},
        ), mock.patch.object(
            app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"},
        ), mock.patch.object(
            app, "app_settings", return_value={"workspace_cleanup": policy},
        ):
            app.maintain_run_storage(
                authorization=authorization,
                inventory=inventory,
            )

        self.assertEqual(calls, ["retention", "pruning", "pressure"])
        inventory.collect_capacity.assert_called_once()

    def test_application_maintenance_preserves_pressure_cleanup_errors(self):
        inventory = mock.Mock()
        inventory.collect_capacity.return_value = {
            "builds": {
                "measurement_state": "ready", "pressure_state": "pressure",
                "available_bytes": 50, "effective_target_bytes": 200,
            },
            "repository": {"measurement_state": "ready", "pressure_state": "normal"},
        }
        pressure_result = {
            "pressure_checked": True, "initial_state": "pressure",
            "candidates_considered": 1, "cleaned": [], "skipped": [],
            "errors": [{"id": "failed", "error": "marker filesystem failure"}],
            "target_reached": False, "final_state": "pressure",
        }
        with mock.patch.object(app, "cleanup_workspaces", return_value={
            "retained": ["failed"], "pressure_candidates": ["failed"], "errors": [],
        }), mock.patch.object(
            app.storage_pruning, "apply_pruning", return_value={"errors": []},
        ), mock.patch.object(
            app.workspace_cleanup, "apply_storage_pressure", return_value=pressure_result,
        ), mock.patch.object(
            app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"},
        ), mock.patch.object(
            app, "app_settings", return_value={"workspace_cleanup": {"enabled": True}},
        ), self.assertLogs("debbuilder.app", level="WARNING"):
            result = app.maintain_run_storage(inventory=inventory)

        self.assertEqual(result["storage_pressure"], pressure_result)

    def test_real_maintenance_pressure_loop_preserves_hysteresis_until_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data = base / "data"
            repo = base / "repo"
            repo.mkdir()
            store = BuildStore(data / "builds")
            roots = {}
            for index, run_id in enumerate(("oldest", "middle", "newest"), start=1):
                run = store.create({
                    "schema_version": 5,
                    "name": run_id,
                    "package": {
                        "name": run_id,
                        "maintainer": "A <a@example.test>",
                        "description": run_id,
                    },
                    "source": {"repository": f"owner/{run_id}"},
                }, mode="build", run_id=run_id)
                root = Path(run["workspace"])
                (root / "source").mkdir(exist_ok=True)
                (root / "source/data").write_text("disposable")
                run["status"] = "failed"
                run["finished_at"] = f"2026-09-0{index}T12:00:00+00:00"
                store.save(run)
                roots[run_id] = root
            policy = {
                "enabled": True,
                "failed_workspaces_to_retain": 3,
                "pressure_minimum_free_bytes": 100,
                "pressure_minimum_free_percent": 10,
                "pressure_target_free_bytes": 200,
                "pressure_target_free_percent": 20,
            }
            available = iter((50, 150, 200))
            observed_states = []

            def measure_builds(*_args, **_kwargs):
                value = next(available)
                return {
                    "measurement_state": "ready",
                    "measured_scope": "builds",
                    "device_id": 1,
                    "total_bytes": 1000,
                    "used_bytes": 1000 - value,
                    "free_bytes": value,
                    "available_bytes": value,
                    "available_percent": value / 10,
                    "utilized_percent": (1000 - value) / 10,
                }

            inventory = StorageInventory(data, repo, policy_provider=lambda: policy)
            original_capacity = inventory.collect_capacity

            def collect_capacity(*args, **kwargs):
                result = original_capacity(*args, **kwargs)
                observed_states.append(result["builds"]["pressure_state"])
                return result

            inventory.collect_capacity = mock.Mock(side_effect=collect_capacity)
            with mock.patch.object(app, "DATA", data), \
                    mock.patch.object(app, "REPOSITORY_ROOT", repo), \
                    mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": policy}), \
                    mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                    mock.patch.object(app.storage_pruning, "apply_pruning", return_value={"errors": []}), \
                    mock.patch.object(storage_inventory, "_measure_builds_filesystem", side_effect=measure_builds), \
                    mock.patch.object(storage_inventory, "_measure_repository_filesystem", return_value={
                        "measurement_state": "ready", "measured_scope": "repository",
                        "device_id": 2, "total_bytes": 1000, "used_bytes": 100,
                        "free_bytes": 900, "available_bytes": 900,
                        "available_percent": 90.0, "utilized_percent": 10.0,
                    }), \
                    mock.patch.object(inventory, "collect") as recursive_collect:
                result = app.maintain_run_storage(
                    authorization=workspace_cleanup.CleanupAuthorization(),
                    inventory=inventory,
                )

            self.assertEqual(observed_states, ["pressure", "pressure", "normal"])
            self.assertEqual(
                [row["id"] for row in result["storage_pressure"]["cleaned"]],
                ["oldest", "middle"],
            )
            self.assertTrue(result["storage_pressure"]["target_reached"])
            self.assertFalse((roots["oldest"] / "source").exists())
            self.assertFalse((roots["middle"] / "source").exists())
            self.assertTrue((roots["newest"] / "source").exists())
            recursive_collect.assert_not_called()

    def test_real_maintenance_stops_on_intermediate_capacity_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, repo = base / "data", base / "repo"
            repo.mkdir()
            store = BuildStore(data / "builds")
            roots = {}
            for index, run_id in enumerate(("first", "second"), start=1):
                run = store.create({
                    "schema_version": 5, "name": run_id,
                    "package": {"name": run_id, "maintainer": "A <a@example.test>", "description": run_id},
                    "source": {"repository": f"owner/{run_id}"},
                }, mode="build", run_id=run_id)
                root = Path(run["workspace"])
                (root / "source/data").write_text("disposable")
                run["status"] = "failed"
                run["finished_at"] = f"2026-09-0{index}T12:00:00+00:00"
                store.save(run)
                roots[run_id] = root
            policy = {
                "enabled": True, "failed_workspaces_to_retain": 2,
                "pressure_minimum_free_bytes": 100, "pressure_minimum_free_percent": 10,
                "pressure_target_free_bytes": 200, "pressure_target_free_percent": 20,
            }
            measurements = iter((
                {
                    "measurement_state": "ready", "measured_scope": "builds", "device_id": 1,
                    "total_bytes": 1000, "used_bytes": 950, "free_bytes": 50,
                    "available_bytes": 50, "available_percent": 5.0, "utilized_percent": 95.0,
                },
                {"measurement_state": "error", "pressure_state": "measurement_error"},
            ))
            inventory = StorageInventory(data, repo, policy_provider=lambda: policy)
            with mock.patch.object(app, "DATA", data), \
                    mock.patch.object(app, "REPOSITORY_ROOT", repo), \
                    mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": policy}), \
                    mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                    mock.patch.object(app, "cleanup_workspaces", return_value={
                        "retained": ["first", "second"],
                        "pressure_candidates": ["first", "second"], "errors": [],
                    }), \
                    mock.patch.object(app.storage_pruning, "apply_pruning", return_value={"errors": []}), \
                    mock.patch.object(storage_inventory, "_measure_builds_filesystem", side_effect=lambda *_args, **_kwargs: next(measurements)), \
                    mock.patch.object(storage_inventory, "_measure_repository_filesystem", return_value={
                        "measurement_state": "ready", "device_id": 2, "total_bytes": 1000,
                        "used_bytes": 100, "free_bytes": 900, "available_bytes": 900,
                        "available_percent": 90.0, "utilized_percent": 10.0,
                    }):
                result = app.maintain_run_storage(
                    authorization=workspace_cleanup.CleanupAuthorization(),
                    inventory=inventory,
                )

            self.assertEqual(result["storage_pressure"]["final_state"], "measurement_error")
            self.assertEqual(
                [row["id"] for row in result["storage_pressure"]["cleaned"]], ["first"],
            )
            self.assertFalse((roots["first"] / "source").exists())
            self.assertTrue((roots["second"] / "source").is_dir())

    def test_maintain_storage_disabled_measures_pressure_without_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, repo = base / "data", base / "repo"
            repo.mkdir()
            store = BuildStore(data / "builds")
            run = store.create({
                "schema_version": 5, "name": "disabled",
                "package": {"name": "disabled", "maintainer": "A <a@example.test>", "description": "disabled"},
                "source": {"repository": "owner/disabled"},
            }, mode="build", run_id="disabled")
            root = Path(run["workspace"])
            (root / "source/data").write_text("disposable")
            (root / "toolchain").mkdir()
            run["status"] = "failed"
            run["finished_at"] = "2026-09-01T12:00:00+00:00"
            store.save(run)
            policy = {"enabled": False, "failed_workspaces_to_retain": 0}
            inventory = mock.Mock()
            inventory.collect_capacity.return_value = {
                "builds": {
                    "measurement_state": "ready", "pressure_state": "pressure",
                    "available_bytes": 50, "effective_target_bytes": 200,
                },
                "repository": {"measurement_state": "ready", "pressure_state": "normal", "same_as_builds": False},
            }
            with mock.patch.object(app, "DATA", data), \
                    mock.patch.object(app, "REPOSITORY_ROOT", repo), \
                    mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": policy}), \
                    mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                    mock.patch.object(app.storage_pruning, "apply_pruning", return_value={"errors": [], "pruned": []}):
                result = app.maintain_run_storage(
                    authorization=workspace_cleanup.CleanupAuthorization(), inventory=inventory,
                )

            self.assertEqual(result["storage_pressure"]["initial_state"], "pressure")
            self.assertEqual(result["storage_pressure"]["cleaned"], [])
            self.assertTrue((root / "source").is_dir())
            self.assertTrue((root / "toolchain").is_dir())
            self.assertEqual(result["storage_pruning"]["pruned"], [])
            inventory.collect_capacity.assert_called_once()

    def test_maintenance_uses_builds_pressure_only_for_separate_or_shared_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, repo = base / "data", base / "repo"
            repo.mkdir()
            store = BuildStore(data / "builds")

            def make_run(run_id):
                run = store.create({
                    "schema_version": 5, "name": run_id,
                    "package": {"name": run_id, "maintainer": "A <a@example.test>", "description": run_id},
                    "source": {"repository": f"owner/{run_id}"},
                }, mode="build", run_id=run_id)
                root = Path(run["workspace"])
                (root / "source/data").write_text("disposable")
                run["status"] = "failed"
                run["finished_at"] = "2026-09-01T12:00:00+00:00"
                store.save(run)
                return root

            separate_root = make_run("separate")
            shared_root = make_run("shared")
            policy = {"enabled": True, "failed_workspaces_to_retain": 2}
            normal_builds = {
                "builds": {
                    "measurement_state": "ready", "pressure_state": "normal",
                    "available_bytes": 300, "effective_target_bytes": 200,
                },
                "repository": {
                    "measurement_state": "ready", "pressure_state": "pressure", "same_as_builds": False,
                },
            }
            pressure_builds = {
                "builds": {
                    "measurement_state": "ready", "pressure_state": "pressure",
                    "available_bytes": 50, "effective_target_bytes": 200,
                },
                "repository": {
                    "measurement_state": "ready", "pressure_state": "normal", "same_as_builds": False,
                },
            }
            target_shared = {
                "builds": {
                    "measurement_state": "ready", "pressure_state": "normal",
                    "available_bytes": 200, "effective_target_bytes": 200,
                },
                "repository": {
                    "measurement_state": "ready", "pressure_state": "normal", "same_as_builds": True,
                },
            }
            with mock.patch.object(app, "DATA", data), \
                    mock.patch.object(app, "REPOSITORY_ROOT", repo), \
                    mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": policy}), \
                    mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                    mock.patch.object(app, "cleanup_workspaces", return_value={
                        "retained": ["separate"], "pressure_candidates": ["separate"], "errors": [],
                    }), \
                    mock.patch.object(app.storage_pruning, "apply_pruning", return_value={"errors": []}):
                repository_only = mock.Mock()
                repository_only.collect_capacity.return_value = normal_builds
                first = app.maintain_run_storage(inventory=repository_only)
                self.assertEqual(first["storage_pressure"]["cleaned"], [])
                self.assertTrue((separate_root / "source").is_dir())

                separate = mock.Mock()
                separate.collect_capacity.side_effect = [pressure_builds, target_shared]
                second = app.maintain_run_storage(inventory=separate)
                self.assertEqual([row["id"] for row in second["storage_pressure"]["cleaned"]], ["separate"])
                self.assertEqual(separate.collect_capacity.call_count, 2)

            with mock.patch.object(app, "DATA", data), \
                    mock.patch.object(app, "REPOSITORY_ROOT", repo), \
                    mock.patch.object(app, "app_settings", return_value={"workspace_cleanup": policy}), \
                    mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                    mock.patch.object(app, "cleanup_workspaces", return_value={
                        "retained": ["shared"], "pressure_candidates": ["shared"], "errors": [],
                    }), \
                    mock.patch.object(app.storage_pruning, "apply_pruning", return_value={"errors": []}):
                shared = mock.Mock()
                shared.collect_capacity.side_effect = [
                    {**pressure_builds, "repository": {**pressure_builds["builds"], "same_as_builds": True}},
                    target_shared,
                ]
                third = app.maintain_run_storage(inventory=shared)
                self.assertEqual([row["id"] for row in third["storage_pressure"]["cleaned"]], ["shared"])
                self.assertEqual(shared.collect_capacity.call_count, 2)
                self.assertFalse((shared_root / "source").exists())

    def test_validation_publication_and_reconciliation_request_destructive_maintenance(self):
        notifier = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            artifact = workspace / "package.deb"
            artifact.write_bytes(b"controlled")
            (workspace / "recipe.json").write_text("{}")
            run = {
                "status": "success",
                "workspace": str(workspace),
                "artifact": {"path": str(artifact)},
            }
            store = mock.Mock()
            store.load.return_value = run
            prepared = {
                "packages": [],
                "profile_name": "bookworm",
                "image": {"id": "sha256:" + "a" * 64},
            }
            with mock.patch.object(app, "BuildStore", return_value=store), \
                    mock.patch.object(app, "validate_recipe_metadata", return_value={"runtime_apt_repositories": []}), \
                    mock.patch.object(app, "notification_service", return_value=notifier), \
                    mock.patch.object(app, "request_maintenance") as request, \
                    mock.patch.object(app.dependency_preparation, "prepare_runtime_dependencies", return_value={"prepared": prepared}), \
                    mock.patch.object(app.dependency_preparation, "begin_lifecycle_attempt"), \
                    mock.patch.object(app.dependency_preparation, "complete_lifecycle_attempt"), \
                    mock.patch.object(app.dependency_preparation.SUPERVISOR, "register"), \
                    mock.patch.object(app.dependency_preparation.SUPERVISOR, "unregister"), \
                    mock.patch.object(app.validation_service, "load_attempt", return_value={
                        "inputs": {"profile": "bookworm", "previous_artifact": None},
                    }), \
                    mock.patch.object(app.validation_service, "load_prepared", return_value=prepared), \
                    mock.patch.object(app.validation_service, "load_automation", return_value={
                        "automatic": False, "publish_after_success": False,
                    }), \
                    mock.patch.object(app.artifact_validation, "validate_artifact", return_value={"status": "success"}):
                app.execute_validation_attempt("run", "attempt", threading.Event(), {})
        request.assert_called_once_with(refresh=True, cleanup=True)

        with mock.patch.object(app, "notification_service", return_value=notifier), \
                mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                mock.patch.object(app, "request_maintenance") as request, \
                mock.patch.object(app.artifact_publication, "publish_artifact", return_value={"status": "success"}):
            app.publish_build_artifact("run")
        request.assert_called_once_with(refresh=True, cleanup=True)

        with mock.patch.object(app, "repo_settings", return_value={"distribution": "bookworm", "component": "main"}), \
                mock.patch.object(app, "request_maintenance") as request, \
                mock.patch.object(app.artifact_publication, "reconcile_publication", return_value={"status": "success"}):
            app.reconcile_build_publication("run")
        request.assert_called_once_with(refresh=True, cleanup=True)


if __name__ == "__main__":
    unittest.main()
