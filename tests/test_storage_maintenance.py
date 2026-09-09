import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from debbuilder import app, maintenance, workspace_cleanup
from debbuilder.build_store import BuildStore
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

        def cleanup(*, authorization, should_stop):
            self.assertIs(authorization, server.cleanup_authorization)
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
                )

            self.assertEqual(result["status"], "success")
            request.assert_called_once_with(refresh=True, cleanup=True)
            sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
