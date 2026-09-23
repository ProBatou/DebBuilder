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
from debbuilder.resource_limits import empty_policy
from debbuilder.settings_store import SettingsDocumentError, save_settings


def recipe(policy=None):
    return {
        "schema_version": 5,
        "name": "resource-admission",
        "active": True,
        "resource_limits": {**empty_policy(), **(policy or {})},
        "package": {"name": "resource-admission"},
        "source": {"repository": "owner/resource-admission", "tracking": "manual", "ref": "v1.0.0"},
    }


class ResourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        runtime_blocker = mock.patch.object(containment, "_CLEANUP_BLOCKERS", {})
        runtime_blocker.start()
        self.addCleanup(runtime_blocker.stop)
        generations = mock.patch.object(containment, "_CLEANUP_BLOCKER_GENERATIONS", {})
        generations.start()
        self.addCleanup(generations.stop)
        revision = mock.patch.object(containment, "_CLEANUP_BLOCKER_REVISION", 0)
        revision.start()
        self.addCleanup(revision.stop)
        saturation = mock.patch.object(containment, "_CLEANUP_BLOCKERS_SATURATED", False)
        saturation.start()
        self.addCleanup(saturation.stop)
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
        containment._publish_cleanup_blocker(
            "probe", "probe cgroup remains",
            containment.starting_metadata("resource-limit-capability-probe", "a" * 32),
        )
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability",
            return_value=self.capability([]),
        ), mock.patch(
            "debbuilder.app.command_containment.prove_containment_absent",
            return_value=containment.AbsenceProof(
                containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS, "probe remains",
            ),
        ), self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "execution_recovery_unresolved")
        self.assertEqual(raised.exception.details["reason"], "1 containment cleanup object(s) unresolved")
        self.assertNotIn("debbuilder-command-", json.dumps(raised.exception.details))
        self.assertFalse(self.store.root.exists())
        self.assertEqual(manager.queued_run_ids, ())

    def test_unresolved_runtime_cleanup_blocks_the_next_admission_without_a_run(self):
        manager = self.manager()
        containment.latch_runtime_cleanup_blocker(
            "prior runtime cgroup remains", containment.starting_metadata("resource-admission", "b" * 32))
        with mock.patch(
            "debbuilder.app.command_containment.resource_limit_capability",
            return_value=self.capability([]),
        ) as capability, mock.patch(
            "debbuilder.app.command_containment.prove_containment_absent",
            return_value=containment.AbsenceProof(
                containment.AbsenceStatus.STILL_PRESENT_AND_OWNED, "runtime remains",
            ),
        ), self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "execution_recovery_unresolved")
        self.assertEqual(raised.exception.details["reason"], "1 containment cleanup object(s) unresolved")
        self.assertNotIn("debbuilder-command-", json.dumps(raised.exception.details))
        capability.assert_not_called()
        self.assertFalse(self.store.root.exists())
        self.assertEqual(manager.queued_run_ids, ())

    def test_malformed_resource_settings_block_admission_until_explicit_bounded_repair(self):
        settings = app.settings_defaults()
        settings["resource_limits"]["memory_max_bytes"] = 1073741824
        settings["resource_limits"]["tasks_max"] = "64"
        raw = json.dumps(settings)
        (self.data / "settings.json").write_text(raw)
        manager = self.manager()
        with self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "settings_invalid")
        self.assertEqual(raised.exception.details["path"], "$.resource_limits.tasks_max")
        self.assertFalse(self.store.root.exists())
        with self.assertRaises(ValueError):
            app.settings_view()
        with self.assertRaises(ValueError):
            app.update_settings({"general": {"app_name": "must-not-rewrite"}})
        self.assertEqual((self.data / "settings.json").read_text(), raw)
        repaired = app.update_settings({"resource_limits": {"tasks_max": 32}})
        self.assertEqual(repaired["resource_limits"]["memory_max_bytes"], 1073741824)
        self.assertEqual(repaired["resource_limits"]["tasks_max"], 32)
        self.assertEqual(
            json.loads((self.data / "settings.json").read_text())["resource_limits"],
            repaired["resource_limits"],
        )

    def test_unversioned_settings_block_admission_without_rewrite(self):
        settings = app.settings_defaults()
        del settings["schema_version"]
        raw = json.dumps(settings)
        (self.data / "settings.json").write_text(raw)
        manager = self.manager()
        with self.assertRaises(app.RunAdmissionError) as raised:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(raised.exception.code, "settings_invalid")
        self.assertEqual(raised.exception.details["path"], "$.schema_version")
        with self.assertRaises(SettingsDocumentError):
            app.update_settings({"resource_limits": {"tasks_max": 32}})
        self.assertEqual((self.data / "settings.json").read_text(), raw)


if __name__ == "__main__":
    unittest.main()
