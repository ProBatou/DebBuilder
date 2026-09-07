import tempfile
import unittest
from pathlib import Path

from debbuilder import build_pipeline, execution_service, package_store
from debbuilder.build_models import utc_now
from debbuilder.build_store import BuildStore


def recipe(name="projection-cancel"):
    return {
        "name": name,
        "active": True,
        "package": {
            "name": name, "architecture": "all",
            "maintainer": "Demo <demo@example.test>", "description": "Demo",
        },
        "source": {"repository": f"owner/{name}"},
    }


class CancellationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = BuildStore(Path(self.temporary.name) / "builds")

    def tearDown(self):
        self.temporary.cleanup()

    def cancellation(self, *, completed=False):
        metadata = {
            "code": "execution_cancelled",
            "reason": "user_requested",
            "phase": "pipeline",
            "stage": "build",
            "requested_at": utc_now(),
        }
        if completed:
            metadata["completed_at"] = utc_now()
        return metadata

    def test_cancelling_is_active_and_list_projection_stays_compact(self):
        run = self.store.create(recipe(), mode="build")
        run.update({"status": "cancelling", "cancellation": self.cancellation()})
        run["steps"][4]["status"] = "running"
        self.store.save(run)

        listed = execution_service.list_executions(self.store, lambda item: item["recipe_id"])[0]
        detail = execution_service.get_execution(self.store, run["id"])
        self.assertEqual(listed["run_id"], run["id"])
        self.assertTrue(listed["lifecycle_active"])
        self.assertNotIn("cancellation", listed)
        self.assertEqual(detail["cancellation"]["kind"], "cancelling")
        self.assertEqual(detail["cancellation"]["stage"], "build")
        self.assertIsNone(detail["diagnostic"])

    def test_cancelled_detail_is_terminal_neutral_and_preserves_partial_logs(self):
        run = self.store.create(recipe(), mode="build")
        partial = {
            "index": 2, "status": "cancelled", "cancelled": True,
            "stdout": "partial output\n", "stderr": "termination output\n",
            "working_directory": "source", "duration": 0.2,
        }
        run.update({
            "status": "cancelled", "error": None,
            "cancellation": self.cancellation(completed=True),
        })
        run["steps"][4].update({
            "status": "cancelled", "summary": "Execution cancelled by user",
            "details": {"commands": [partial]}, "error": None,
        })
        self.store.save(run)
        self.store.append_log_line(run["id"], "partial output")

        detail = execution_service.get_execution(self.store, run["id"])
        log = execution_service.get_log(self.store, run["id"], verbosity="verbose")
        self.assertFalse(detail["lifecycle_active"])
        self.assertIsNone(detail["error"])
        self.assertEqual(detail["steps"][4]["status"], "cancelled")
        self.assertEqual(detail["cancellation"]["kind"], "cancelled")
        self.assertEqual(detail["cancellation"]["title"], "Run cancelled")
        self.assertIsNone(detail["diagnostic"])
        self.assertTrue(log["complete"])
        self.assertIn("partial output", log["text"])
        self.assertIn("termination output", log["text"])

    def test_termination_failure_remains_a_failure_diagnostic(self):
        run = self.store.create(recipe(), mode="build")
        error = {
            "stage": "build", "code": "execution_cancellation_termination_failed",
            "message": "Cancellation could not safely terminate the active process group",
            "details": {"stage": "build", "termination_error": "group remained alive"},
        }
        run.update({
            "status": "failed", "error": error,
            "cancellation": self.cancellation(completed=True),
        })
        run["steps"][4].update({"status": "failed", "error": error})
        self.store.save(run)

        detail = execution_service.get_execution(self.store, run["id"])
        self.assertEqual(detail["diagnostic"]["code"], "execution_cancellation_termination_failed")
        self.assertEqual(detail["diagnostic"]["phase"], "build")
        self.assertEqual(detail["cancellation"]["status"], "failed")

    def test_cancelled_real_build_is_retryable_but_not_validatable_or_publishable(self):
        run = self.store.create(recipe(), mode="build")
        run.update({"status": "cancelled", "cancellation": self.cancellation(completed=True)})
        actions = package_store.allowed_actions("cancelled", run["recipe_id"], run)
        self.assertEqual(actions, {"test": True, "build": True, "validate": False, "publish": False})
        self.assertEqual(package_store.derive_lifecycle_status("cancelled"), "cancelled")

    def test_cancelled_test_is_terminal_and_not_ready_for_build_promotion(self):
        run = self.store.create(recipe("cancelled-test"), mode="dry_run")
        run.update({"status": "cancelled", "cancellation": self.cancellation(completed=True)})
        self.store.save(run)

        detail = execution_service.get_execution(self.store, run["id"])
        self.assertFalse(detail["lifecycle_active"])
        self.assertFalse(detail["ready_for_build"])
        self.assertEqual(detail["allowed_actions"], {"validate": False, "publish": False})
        self.assertEqual(detail["status"], "cancelled")
        self.assertNotEqual(detail["status"], "prepared")


if __name__ == "__main__":
    unittest.main()
