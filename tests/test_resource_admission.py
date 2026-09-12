import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app
import debbuilder.command_containment as containment
from debbuilder.build_store import BuildStore
from debbuilder.execution_manager import ExecutionManager
from debbuilder.resource_limits import ResourceLimitError, empty_policy
from debbuilder.settings_store import save_settings


def recipe(policy=None):
    return {
        "schema_version": 3,
        "name": "resource-admission",
        "active": True,
        "resource_limits": {**empty_policy(), **(policy or {})},
        "package": {"name": "resource-admission"},
        "source": {"repository": "owner/resource-admission"},
    }


class ResourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        runtime_blocker = mock.patch.object(containment, "_RUNTIME_CLEANUP_ERROR", "")
        runtime_blocker.start()
        self.addCleanup(runtime_blocker.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.data.mkdir()
        self.store = BuildStore(self.data / "builds")
        self.old_data = app.DATA
        app.DATA = self.data
        self.addCleanup(setattr, app, "DATA", self.old_data)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop(timeout=5)

    def manager(self, execute=None):
        manager = ExecutionManager(self.store, execute=execute)
        manager.start()
        self.managers.append(manager)
        return manager

    @staticmethod
    def capability(controls):
        return {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": controls, "reason": "verified",
        }

    def save_policy(self, policy):
        settings = app.settings_defaults()
        settings["resource_limits"] = {**empty_policy(), **policy}
        save_settings(self.data, settings)

    def test_admission_snapshots_strictest_policy_immutably(self):
        started = threading.Event()
        release = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            release.wait(3)
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "prepared"
                store.save(run)
            return {"run_id": run_id, "status": "prepared"}

        self.addCleanup(release.set)
        self.save_policy({"memory_max_bytes": 1000, "tasks_max": 100})
        workflow = recipe({"memory_max_bytes": 2000, "tasks_max": 50})
        manager = self.manager(execute=execute)
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability",
            return_value=self.capability(["memory", "tasks"]),
        ):
            response = app.enqueue_recipe_run(manager, workflow, dry_run=True)
        self.assertTrue(started.wait(2))
        admitted = self.store.load(response["run_id"])
        self.assertEqual(admitted["resource_limits"]["effective"]["memory_max_bytes"], 1000)
        self.assertEqual(admitted["resource_limits"]["origins"]["memory_max_bytes"], "global")
        self.assertEqual(admitted["resource_limits"]["effective"]["tasks_max"], 50)
        self.assertEqual(admitted["resource_limits"]["origins"]["tasks_max"], "recipe")

        self.save_policy({"memory_max_bytes": 10, "tasks_max": 10})
        workflow["resource_limits"]["tasks_max"] = 5
        unchanged = self.store.load(response["run_id"])
        self.assertEqual(unchanged["resource_limits"], admitted["resource_limits"])
        release.set()

    def test_unavailable_capability_rejects_without_workspace_or_phantom_reservation(self):
        self.save_policy({"memory_max_bytes": 1000})
        manager = self.manager()
        unavailable = {
            "backend": "process_group", "available": False,
            "requested_controls": ["memory"], "reason": "memory controller unavailable",
        }
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability", return_value=unavailable,
        ), self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "resource_limits_unavailable")
        self.assertFalse(self.store.root.exists())
        self.assertEqual(manager.queued_run_ids, ())

    def test_unresolved_probe_cleanup_blocks_even_unlimited_admission_without_a_run(self):
        manager = self.manager()
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability",
            return_value=self.capability([]),
        ), mock.patch(
            "debbuilder.app.command_containment.probe_cleanup_blocker",
            return_value="probe cgroup remains",
        ), self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "execution_recovery_unresolved")
        self.assertFalse(self.store.root.exists())
        self.assertEqual(manager.queued_run_ids, ())

    def test_unresolved_runtime_cleanup_blocks_the_next_admission_without_a_run(self):
        manager = self.manager()
        containment.latch_runtime_cleanup_blocker("prior runtime cgroup remains")
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability",
            return_value=self.capability([]),
        ) as capability, self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "execution_recovery_unresolved")
        capability.assert_not_called()
        self.assertFalse(self.store.root.exists())
        self.assertEqual(manager.queued_run_ids, ())

    def test_malformed_resource_settings_block_admission_but_settings_remain_repairable(self):
        (self.data / "settings.json").write_text(json.dumps({"resource_limits": {"tasks_max": "64"}}))
        manager = self.manager()
        with self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "resource_settings_invalid")
        self.assertFalse(self.store.root.exists())
        view = app.settings_view()
        self.assertFalse(view["resource_limits_status"]["valid"])
        with self.assertRaises(ResourceLimitError) as unrelated:
            app.update_settings({"general": {"app_name": "must-not-rewrite"}})
        self.assertEqual(unrelated.exception.code, "resource_settings_repair_required")
        self.assertEqual(
            json.loads((self.data / "settings.json").read_text())["resource_limits"]["tasks_max"],
            "64",
        )
        repaired = app.update_settings({"resource_limits": empty_policy()})
        self.assertTrue(repaired["resource_limits_status"]["valid"])

    def test_partial_repair_preserves_other_valid_stored_ceilings(self):
        (self.data / "settings.json").write_text(json.dumps({
            "resource_limits": {"memory_max_bytes": 1073741824, "tasks_max": "bad"},
        }))
        loaded = app.settings_view()
        self.assertFalse(loaded["resource_limits_status"]["valid"])
        self.assertEqual(loaded["resource_limits"]["memory_max_bytes"], 1073741824)

        repaired = app.update_settings({"resource_limits": {"tasks_max": 32}})

        self.assertTrue(repaired["resource_limits_status"]["valid"])
        self.assertEqual(repaired["resource_limits"]["memory_max_bytes"], 1073741824)
        self.assertEqual(repaired["resource_limits"]["tasks_max"], 32)
        stored = json.loads((self.data / "settings.json").read_text())["resource_limits"]
        self.assertEqual(stored["memory_max_bytes"], 1073741824)
        self.assertEqual(stored["tasks_max"], 32)


if __name__ == "__main__":
    unittest.main()
