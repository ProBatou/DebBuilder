import tempfile
import unittest

from debbuilder.dependency_checker import DependencyError, check_dependencies


class DependencyCheckerTests(unittest.TestCase):
    def test_reports_detected_manual_available_and_missing_separately(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            installed = command.endswith(" python3") or command.endswith(" cargo")
            return {"status":"success" if installed else "failed", "stdout":"ii \n" if installed else "", "exit_code":0 if installed else 1,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(DependencyError) as raised:
                check_dependencies(["python3", "python3-pip"], ["cargo", "python3"], workspace=workspace, runner=runner)
        state = raised.exception.details
        self.assertEqual(state["detected"], ["python3", "python3-pip"])
        self.assertEqual(state["manually_added"], ["cargo", "python3"])
        self.assertEqual(state["required"], ["python3", "python3-pip", "cargo"])
        self.assertEqual(state["available"], ["python3", "cargo"])
        self.assertEqual(state["missing"], ["python3-pip"])
        self.assertFalse(state["installation_attempted"])
        self.assertEqual(len(calls), 3)

    def test_all_available_returns_exploitable_state(self):
        runner = lambda command, **kwargs: {"status":"success", "stdout":"ii ", "exit_code":0,"command":command,"arguments":command.split(),"working_directory":kwargs["working_directory"],"duration":0.01}
        with tempfile.TemporaryDirectory() as workspace:
            state = check_dependencies(["nodejs"], ["npm"], workspace=workspace, runner=runner)
        self.assertEqual(state["available"], ["nodejs", "npm"])
        self.assertEqual(state["missing"], [])

    def test_invalid_package_name_is_rejected_without_running_a_command(self):
        def runner(*_args, **_kwargs):
            raise AssertionError("runner must not be called")
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(DependencyError) as raised:
                check_dependencies(["python3;id"], [], workspace=workspace, runner=runner)
        self.assertEqual(raised.exception.code, "invalid_dependency")


if __name__ == "__main__":
    unittest.main()
