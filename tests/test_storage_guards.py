import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app, build_pipeline, resource_limits, storage_inventory
from debbuilder.automation_identity import automation_attempt_key, normalize_upstream_identity
from debbuilder.api_errors import canonical_error_payload
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.execution_cancellation import CancellationControl
from debbuilder.execution_manager import ExecutionManager
from debbuilder.recipe_schema import recipe_for_storage


def recipe(name="guard"):
    return recipe_for_storage({
        "schema_version": 5, "name": name, "active": True,
        "package": {"name": name, "architecture": "all"},
        "source": {"repository": f"example/{name}", "tracking": "manual", "ref": "v1"},
    })


def capacity(state, *, repository_state="normal"):
    builds = {
        "pressure_state": state,
        "available_bytes": 100,
        "effective_start_bytes": 200,
        "effective_target_bytes": 300,
        "available_percent": 4.25,
    }
    return {"builds": builds, "repository": {"pressure_state": repository_state}}


class FakeInventory:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    def collect_capacity(self):
        self.calls += 1
        selected = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(selected, BaseException):
            raise selected
        return selected

    def collect(self):
        raise AssertionError("storage guard performed a recursive collection")


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = BuildStore(self.root / "builds")
        self.store.root.mkdir()

    def create_queued(self, name="guard", **metadata):
        run = build_pipeline.create_pipeline_run(
            recipe(name), store=self.store, dry_run=False, recipe_id=name, **metadata,
        )
        self.store.transition_status(run["id"], expected="pending", status="queued")
        return self.store.load(run["id"])

    def execute(self, run, inventory):
        with mock.patch.object(app.automation_service, "complete_with_automation", side_effect=lambda result, **_kwargs: result), \
             mock.patch.object(app, "request_maintenance") as maintenance, \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run") as pipeline:
            result = app.execute_queued_recipe_run(
                run["id"], store=self.store, expected_initial_status="queued",
                storage_inventory=inventory,
            )
        return result, pipeline, maintenance

    def test_pressure_and_measurement_fail_closed_and_request_maintenance(self):
        for measured, code in (
            (capacity("pressure"), "storage_pressure_admission_blocked"),
            (RuntimeError("private /path"), "storage_measurement_unavailable"),
        ):
            with self.subTest(code=code), mock.patch.object(app, "request_maintenance") as request:
                inventory = FakeInventory(measured)
                with self.assertRaises(app.RunAdmissionError) as captured:
                    app._require_storage_admission_clear(inventory)
                self.assertEqual(captured.exception.code, code)
                self.assertEqual(captured.exception.status, 503)
                self.assertNotIn("/path", str(captured.exception.as_dict()))
                request.assert_called_once_with(refresh=True, cleanup=True)
                self.assertEqual(inventory.calls, 1)

    def test_missing_inventory_stays_fail_closed_without_maintenance_service(self):
        previous = app.APPLICATION_MAINTENANCE_SERVICE
        app.APPLICATION_MAINTENANCE_SERVICE = None
        self.addCleanup(setattr, app, "APPLICATION_MAINTENANCE_SERVICE", previous)
        with self.assertRaises(app.RunAdmissionError) as captured:
            app._require_storage_admission_clear(None)
        self.assertEqual(captured.exception.code, "storage_measurement_unavailable")

    def test_repository_pressure_does_not_control_build_guard(self):
        app._require_storage_admission_clear(
            FakeInventory(capacity("normal", repository_state="pressure")),
        )

    def test_disabled_cleanup_policy_does_not_disable_guard(self):
        inventory = storage_inventory.StorageInventory(
            self.root, self.root, policy_provider=lambda: {
                "enabled": False, "failed_workspaces_to_retain": 5,
                "pressure_minimum_free_bytes": 200,
                "pressure_minimum_free_percent": 1,
                "pressure_target_free_bytes": 300,
                "pressure_target_free_percent": 2,
            },
        )
        measurement = {
            "measurement_state": "ready", "measured_scope": "builds", "device_id": 1,
            "total_bytes": 1000, "used_bytes": 900, "free_bytes": 100,
            "available_bytes": 100, "available_percent": 10.0, "utilized_percent": 90.0,
        }
        with mock.patch.object(storage_inventory, "_measure_builds_filesystem", return_value=measurement), \
             mock.patch.object(storage_inventory, "_measure_repository_filesystem", return_value=measurement), \
             self.assertRaises(app.RunAdmissionError) as captured:
            app._require_storage_admission_clear(inventory)
        self.assertEqual(captured.exception.code, "storage_pressure_admission_blocked")
        run = self.create_queued("cleanup-disabled")
        with mock.patch.object(storage_inventory, "_measure_builds_filesystem", return_value=measurement), \
             mock.patch.object(storage_inventory, "_measure_repository_filesystem", return_value=measurement):
            result, pipeline, _maintenance = self.execute(run, inventory)
        pipeline.assert_not_called()
        self.assertEqual(result["error"]["code"], "storage_pressure_execution_blocked")

    def test_shared_inventory_preserves_hysteresis_until_target(self):
        policy = {
            "enabled": True, "failed_workspaces_to_retain": 5,
            "pressure_minimum_free_bytes": 200, "pressure_minimum_free_percent": 1,
            "pressure_target_free_bytes": 300, "pressure_target_free_percent": 2,
        }
        inventory = storage_inventory.StorageInventory(
            self.root, self.root, policy_provider=lambda: policy,
        )
        values = iter((100, 250, 250, 300))

        def measured(_root, **_kwargs):
            available = next(values)
            return {
                "measurement_state": "ready", "measured_scope": "builds", "device_id": 1,
                "total_bytes": 1000, "used_bytes": 1000 - available,
                "free_bytes": available, "available_bytes": available,
                "available_percent": float(available / 10),
                "utilized_percent": float((1000 - available) / 10),
            }

        repository = {
            "measurement_state": "ready", "measured_scope": "repository", "device_id": 1,
            "total_bytes": 1000, "used_bytes": 0, "free_bytes": 1000,
            "available_bytes": 1000, "available_percent": 100.0, "utilized_percent": 0.0,
        }
        with mock.patch.object(storage_inventory, "_measure_builds_filesystem", side_effect=measured), \
             mock.patch.object(storage_inventory, "_measure_repository_filesystem", return_value=repository):
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "pressure")
            with self.assertRaises(app.RunAdmissionError) as blocked:
                app._require_storage_admission_clear(inventory)
            self.assertEqual(blocked.exception.code, "storage_pressure_admission_blocked")
            run = self.create_queued("shared-hysteresis")
            result, pipeline, _maintenance = self.execute(run, inventory)
            self.assertEqual(result["error"]["code"], "storage_pressure_execution_blocked")
            pipeline.assert_not_called()
            app._require_storage_admission_clear(inventory)

    def test_repeated_admission_refusals_release_reservations_and_recover(self):
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
            run = store.transition_status(run_id, expected=expected_initial_status, status="running")
            run.update({"status": "success", "finished_at": app.utc_now(), "duration": 0.0})
            store.save(run)
            finished.set()
            return run

        manager = ExecutionManager(self.store, queue_capacity=1, execute=execute)
        manager.start()
        self.addCleanup(manager.stop, timeout=5)
        canonical = recipe("reservation-release")
        contract = resource_limits.default_contract()
        for _ in range(3):
            with self.assertRaises(app.RunAdmissionError):
                app._enqueue_prepared_recipe_run(
                    manager, canonical, contract, dry_run=False,
                    storage_inventory=FakeInventory(capacity("pressure")),
                )
            with manager._condition:
                self.assertEqual(manager._reservations, set())
                self.assertEqual(manager._admission_leases, set())
                self.assertEqual(manager._reservation_run_ids, {})
            self.assertEqual(list(self.store.root.glob("*/run.json")), [])
        admitted = app._enqueue_prepared_recipe_run(
            manager, canonical, contract, dry_run=False,
            storage_inventory=FakeInventory(capacity("normal")),
        )
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.store.load(admitted["run_id"])["status"], "success")

    def test_admission_error_precedence_recipe_recovery_and_queue(self):
        pressure = FakeInventory(capacity("pressure"))
        manager = ExecutionManager(self.store, queue_capacity=1, execute=lambda *_args, **_kwargs: None)
        with self.assertRaises(app.RunAdmissionError) as invalid:
            app.enqueue_recipe_run(
                manager, {"schema_version": 4, "name": "invalid"},
                storage_inventory=pressure,
            )
        self.assertEqual(invalid.exception.code, "unsupported_recipe_schema")
        self.assertEqual(pressure.calls, 0)

        manager.start()
        self.addCleanup(manager.stop, timeout=5)
        with mock.patch.object(
            app.command_containment, "containment_cleanup_blocker", return_value="unresolved",
        ), self.assertRaises(app.RunAdmissionError) as recovery:
            app._enqueue_prepared_recipe_run(
                manager, recipe("recovery-first"), resource_limits.default_contract(),
                dry_run=False, storage_inventory=pressure,
            )
        self.assertEqual(recovery.exception.code, "execution_recovery_unresolved")
        self.assertEqual(pressure.calls, 0)

        with manager.reserve(), self.assertRaises(app.RunAdmissionError) as full:
            app._enqueue_prepared_recipe_run(
                manager, recipe("queue-first"), resource_limits.default_contract(),
                dry_run=False, storage_inventory=pressure,
            )
        self.assertEqual(full.exception.code, "execution_queue_full")
        self.assertEqual(pressure.calls, 0)

    def test_worker_pressure_terminalizes_without_pipeline_and_preserves_metadata(self):
        run = self.create_queued()
        immutable = {key: run[key] for key in (
            "recipe_id", "recipe_sha256", "origin", "automation", "admission_sha256",
        )}
        result, pipeline, maintenance = self.execute(run, FakeInventory(capacity("pressure")))
        self.assertEqual(result["status"], "failed")
        pipeline.assert_not_called()
        failed = self.store.load(run["id"])
        self.assertEqual(failed["error"]["code"], "storage_pressure_execution_blocked")
        self.assertEqual(failed["error"]["stage"], "storage_guard")
        self.assertIsNotNone(failed["finished_at"])
        self.assertEqual({key: failed[key] for key in immutable}, immutable)
        self.assertGreaterEqual(maintenance.call_count, 1)

    def test_worker_measurement_error_terminalizes_without_pipeline(self):
        run = self.create_queued("measurement")
        _result, pipeline, _maintenance = self.execute(
            run, FakeInventory(RuntimeError("private failure")),
        )
        pipeline.assert_not_called()
        failed = self.store.load(run["id"])
        self.assertEqual(failed["error"]["code"], "storage_measurement_unavailable")
        self.assertEqual(failed["error"]["details"]["reason"], "builds_capacity_measurement_failed")

    def test_cancellation_and_storage_guard_settle_one_terminal_owner(self):
        cancelled = self.create_queued("cancel-wins")
        control = CancellationControl()
        self.assertTrue(control.request_cancel()["accepted"])
        with mock.patch.object(app.build_pipeline, "execute_pipeline_run") as pipeline:
            result = app.execute_queued_recipe_run(
                cancelled["id"], store=self.store, expected_initial_status="queued",
                cancellation_control=control,
                storage_inventory=FakeInventory(capacity("pressure")),
            )
        pipeline.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["cancellation"]["stage"], "storage_guard")
        self.assertIsNone(result["error"])

        failed = self.create_queued("storage-wins")
        control = CancellationControl()
        result, pipeline, _maintenance = self.execute(failed, FakeInventory(capacity("pressure")))
        pipeline.assert_not_called()
        self.assertEqual(result["status"], "failed")
        # The equivalent manager-owned control cannot be claimed after the
        # storage terminalization commit point.
        owned = CancellationControl()
        owned_run = self.create_queued("storage-owned")
        app._terminalize_storage_guard_failure(
            self.store, owned_run["id"],
            {"stage": "storage_guard", "code": "storage_pressure_execution_blocked", "message": "blocked", "details": {}},
            cancellation_control=owned,
        )
        self.assertFalse(owned.request_cancel()["accepted"])

    def test_queued_automation_run_notifies_existing_terminal_path(self):
        configured = recipe_for_storage({
            "schema_version": 5, "name": "automated", "active": True,
            "automation": {"enabled": True, "policy": "build"},
            "package": {"name": "automated", "architecture": "all"},
            "source": {"repository": "example/automated", "tracking": "latest_release"},
        })
        identity = normalize_upstream_identity({
            "provider": "github", "repository": "example/automated",
            "tracking": "latest_release", "source_type": "release_asset",
            "payload_kind": "deb", "release_id": "10", "asset_id": "20",
            "asset_name": "automated_1.0_all.deb", "expected_size": 7,
            "resolved_ref": "v1", "resolved_version": "1.0",
            "expected_package": "automated", "expected_architecture": "all",
        })
        metadata = {
            "attempt_key": automation_attempt_key(
                "automated", identity, canonical_recipe_sha256(configured),
            ),
            "generation": 0, "policy": "build",
            "expected_upstream_identity": identity,
        }
        run = build_pipeline.create_pipeline_run(
            configured, store=self.store, dry_run=False, recipe_id="automated",
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation=metadata,
        )
        self.store.transition_status(run["id"], expected="pending", status="queued")
        owner = mock.Mock()
        with mock.patch.object(app, "APPLICATION_AUTOMATION_ORCHESTRATOR", owner), \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run") as pipeline:
            result = app.execute_queued_recipe_run(
                run["id"], store=self.store, expected_initial_status="queued",
                storage_inventory=FakeInventory(capacity("pressure")),
            )
        self.assertEqual(result["status"], "failed")
        pipeline.assert_not_called()
        owner.on_run_terminal.assert_called_once_with(run["id"])

    def test_worker_normal_enters_pipeline(self):
        run = self.create_queued("normal")

        def terminalize(run_id, **_kwargs):
            with self.store.locked_run(run_id):
                current = self.store.load(run_id)
                current.update({"status": "success", "finished_at": app.utc_now(), "duration": 0.0})
                self.store.save(current)
            return current

        with mock.patch.object(app.automation_service, "complete_with_automation", side_effect=lambda result, **_kwargs: result), \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run", side_effect=terminalize) as pipeline:
            app.execute_queued_recipe_run(
                run["id"], store=self.store, expected_initial_status="queued",
                storage_inventory=FakeInventory(capacity("normal")),
            )
        pipeline.assert_called_once()

    def test_worker_ignores_repository_only_pressure(self):
        run = self.create_queued("repository-only")

        def terminalize(run_id, **_kwargs):
            current = self.store.load(run_id)
            current.update({"status": "success", "finished_at": app.utc_now(), "duration": 0.0})
            self.store.save(current)
            return current

        inventory = FakeInventory(capacity("normal", repository_state="pressure"))
        with mock.patch.object(app.automation_service, "complete_with_automation", side_effect=lambda result, **_kwargs: result), \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run", side_effect=terminalize) as pipeline:
            result = app.execute_queued_recipe_run(
                run["id"], store=self.store, expected_initial_status="queued",
                storage_inventory=inventory,
            )
        self.assertEqual(result["status"], "success")
        pipeline.assert_called_once()

    def test_manual_storage_failure_notifies_as_failed_without_lifecycle_start(self):
        run = self.create_queued("manual-notification")
        notifications = mock.Mock()
        with mock.patch.object(app, "notification_service", return_value=notifications), \
             mock.patch.object(app, "notify_lifecycle") as lifecycle, \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run") as pipeline:
            result = app.execute_queued_recipe_run(
                run["id"], store=self.store, expected_initial_status="queued",
                storage_inventory=FakeInventory(capacity("pressure")),
            )
        pipeline.assert_not_called()
        lifecycle.assert_not_called()
        self.assertEqual(result["status"], "failed")
        notifications.notify_automatic_completion.assert_called_once()
        notified = notifications.notify_automatic_completion.call_args.args[0]
        self.assertEqual(notified["error"]["stage"], "storage_guard")

    def test_second_queued_run_is_blocked_when_capacity_falls_after_first(self):
        first = self.create_queued("first")
        second = self.create_queued("second")
        inventory = FakeInventory(capacity("normal"), capacity("pressure"))

        def pipeline(run_id, **_kwargs):
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run.update({"status": "success", "finished_at": app.utc_now(), "duration": 0.0})
                self.store.save(run)
            return run

        manager = ExecutionManager(
            self.store,
            execute=lambda run_id, **kwargs: app.execute_queued_recipe_run(
                run_id, storage_inventory=inventory, **kwargs,
            ),
        )
        with mock.patch.object(app.automation_service, "complete_with_automation", side_effect=lambda result, **_kwargs: result), \
             mock.patch.object(app.build_pipeline, "execute_pipeline_run", side_effect=pipeline) as execute:
            manager.start()
            with manager._condition:
                manager._queue.extend((first["id"], second["id"]))
                manager._condition.notify_all()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.store.load(second["id"])["status"] != "failed":
                time.sleep(0.01)
            manager.stop(timeout=5)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0], first["id"])
        self.assertEqual(self.store.load(second["id"])["error"]["code"], "storage_pressure_execution_blocked")

    def test_storage_terminalization_persistence_failure_stops_before_next_run(self):
        first = self.create_queued("persist-first")
        second = self.create_queued("persist-second")
        entered = threading.Event()
        release = threading.Event()

        class BlockingPressure(FakeInventory):
            def collect_capacity(inner_self):
                entered.set()
                release.wait(2)
                return super().collect_capacity()

        inventory = BlockingPressure(capacity("pressure"))
        manager = ExecutionManager(
            self.store,
            execute=lambda run_id, **kwargs: app.execute_queued_recipe_run(
                run_id, storage_inventory=inventory, **kwargs,
            ),
        )
        original_save = self.store.save

        def fail_terminal(run):
            if run["id"] == first["id"] and run.get("status") == "failed":
                raise OSError("persistence unavailable")
            return original_save(run)

        with mock.patch.object(app.build_pipeline, "execute_pipeline_run") as pipeline:
            manager.start()
            with manager._condition:
                manager._queue.extend((first["id"], second["id"]))
                manager._condition.notify_all()
            self.assertTrue(entered.wait(2))
            with mock.patch.object(self.store, "save", side_effect=fail_terminal):
                release.set()
                manager.worker.join(2)
            manager.stop(timeout=5)
        pipeline.assert_not_called()
        self.assertFalse(manager.accepting)
        self.assertEqual(self.store.load(first["id"])["status"], "queued")
        self.assertEqual(self.store.load(second["id"])["status"], "queued")

    def test_preallocated_pending_is_not_submitted_under_pressure_then_reuses_id(self):
        run = build_pipeline.create_pipeline_run(
            recipe("preallocated"), store=self.store, dry_run=False,
            recipe_id="preallocated", run_id="preallocated-run",
        )
        manager = ExecutionManager(self.store, execute=lambda *_args, **_kwargs: None)
        manager.start()
        self.addCleanup(manager.stop, timeout=5)
        kwargs = {
            "run_id": run["id"], "dry_run": False,
            "origin": run["origin"], "automation": run["automation"],
        }
        callback = mock.Mock()
        with self.assertRaises(app.RunAdmissionError) as captured:
            app._enqueue_prepared_preallocated_recipe_run(
                manager, recipe("preallocated"), resource_limits.default_contract(),
                storage_inventory=FakeInventory(capacity("pressure")),
                created_callback=callback, **kwargs,
            )
        self.assertEqual(captured.exception.code, "storage_pressure_admission_blocked")
        self.assertEqual(self.store.load(run["id"])["status"], "pending")
        callback.assert_not_called()
        self.assertEqual(list(self.store.root.glob("*/run.json")), [self.store.run_dir(run["id"]) / "run.json"])
        recovered = app._enqueue_prepared_preallocated_recipe_run(
            manager, recipe("preallocated"), resource_limits.default_contract(),
            storage_inventory=FakeInventory(capacity("normal")),
            created_callback=callback, **kwargs,
        )
        self.assertEqual(recovered["run_id"], run["id"])
        self.assertEqual(len(list(self.store.root.glob("*/run.json"))), 1)
        callback.assert_called_once()

    def test_preallocated_pressure_before_creation_creates_no_workspace(self):
        manager = ExecutionManager(self.store, execute=lambda *_args, **_kwargs: None)
        manager.start()
        self.addCleanup(manager.stop, timeout=5)
        callback = mock.Mock()
        with self.assertRaises(app.RunAdmissionError) as captured:
            app._enqueue_prepared_preallocated_recipe_run(
                manager, recipe("new-preallocated"), resource_limits.default_contract(),
                run_id="new-preallocated-run", dry_run=False,
                origin={"kind": "manual", "trigger": "manual", "reason": None},
                automation=None,
                storage_inventory=FakeInventory(capacity("pressure")),
                created_callback=callback,
            )
        self.assertEqual(captured.exception.code, "storage_pressure_admission_blocked")
        self.assertFalse(self.store.run_dir("new-preallocated-run").exists())
        callback.assert_not_called()
        admitted = app._enqueue_prepared_preallocated_recipe_run(
            manager, recipe("new-preallocated"), resource_limits.default_contract(),
            run_id="new-preallocated-run", dry_run=False,
            origin={"kind": "manual", "trigger": "manual", "reason": None},
            automation=None, storage_inventory=FakeInventory(capacity("normal")),
            created_callback=callback,
        )
        self.assertEqual(admitted["run_id"], "new-preallocated-run")
        self.assertEqual(len(list(self.store.root.glob("*/run.json"))), 1)
        callback.assert_called_once()

    def test_public_error_bounds_storage_details(self):
        payload = canonical_error_payload({"error": {
            "code": "storage_pressure_admission_blocked", "message": "private",
            "details": {
                "state": "pressure", "available_bytes": 12, "start_bytes": 20,
                "target_bytes": 30, "available_percent": 4.2,
                "path": "/private/builds", "traceback": "secret",
            },
        }}, 503, "/api/run")
        self.assertEqual(payload["error"]["details"], {
            "state": "pressure", "available_bytes": 12, "start_bytes": 20,
            "target_bytes": 30, "available_percent": 4.2,
        })


if __name__ == "__main__":
    unittest.main()
