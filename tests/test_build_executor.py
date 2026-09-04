import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from debbuilder.build_executor import BuildError, execute_build, select_commands, validate_build_plan


def recipe(commands=None, working_directory=".", environment=None, output=None):
    return {"build": {"commands": commands or [], "working_directory": working_directory, "environment": environment or {}, "inactivity_timeout": 300, "maximum_runtime": None, "output": output or {"mode":"path","path":"dist"}}}


DETECTION = {"proposed_commands": ["npm ci", "npm run build"]}


class BuildExecutorTests(unittest.TestCase):
    def test_static_project_build_is_a_real_noop_with_source_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "asset.sh").write_text("#!/bin/sh\n")
            configured = recipe([], output={"mode": "source", "path": ""})
            result = execute_build(configured, {"project_type": "static", "proposed_commands": []}, source, dry_run=False)
        self.assertTrue(result["executed"])
        self.assertEqual(result["reason"], "static_noop")

    def test_detected_python_source_application_is_a_real_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "server.py").write_text("print('hello')\n")
            configured = recipe([], output={"mode": "paths", "paths": ["server.py"]})
            detection = {"project_type": "python", "build_mode": "source", "proposed_commands": []}
            result = execute_build(configured, detection, source, dry_run=False)
        self.assertTrue(result["executed"])
        self.assertEqual(result["reason"], "source_noop")
        self.assertEqual(result["commands"], [])

    def test_static_project_rejects_commands_and_path_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            detection = {"project_type": "static", "proposed_commands": []}
            with self.assertRaises(BuildError) as commands:
                validate_build_plan(recipe(["true"], output={"mode": "source", "path": ""}), detection, temporary, dry_run=False)
            self.assertEqual(commands.exception.code, "static_build_commands_not_allowed")
            with self.assertRaises(BuildError) as output:
                validate_build_plan(recipe([], output={"mode": "path", "path": "dist"}), detection, temporary, dry_run=False)
            self.assertEqual(output.exception.code, "invalid_static_output")

    def test_configured_commands_override_detection_proposals(self):
        selection = select_commands(["make all"], DETECTION["proposed_commands"], dry_run=False)
        self.assertEqual(selection, {"source":"recipe", "commands":["make all"], "confirmed":True})

    def test_proposals_are_dry_run_only_until_confirmed(self):
        selection = select_commands([], DETECTION["proposed_commands"], dry_run=True)
        self.assertEqual(selection["source"], "detection_proposal")
        self.assertFalse(selection["confirmed"])
        with self.assertRaises(BuildError) as raised:
            select_commands([], DETECTION["proposed_commands"], dry_run=False)
        self.assertEqual(raised.exception.code, "build_commands_not_confirmed")

    def test_dry_run_validates_commands_cwd_environment_and_output_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "frontend").mkdir()
            result = execute_build(recipe(["printf '%s' ok"], "frontend", {"NODE_ENV":"production"}), {}, source, dry_run=True, runner=lambda *_a, **_k: self.fail("must not execute"))
        self.assertFalse(result["executed"])
        self.assertEqual(result["plan"]["commands"][0]["arguments"], ["printf", "%s", "ok"])
        self.assertEqual(result["plan"]["configured_working_directory"], "frontend")
        self.assertEqual(result["plan"]["environment_keys"], ["NODE_ENV"])
        self.assertFalse(result["output"]["exists"])

    def test_dry_run_rejects_shell_operator_before_any_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as raised:
                execute_build(recipe(["echo first", "echo bad && touch file"]), {}, temporary, dry_run=True)
        self.assertEqual(raised.exception.code, "invalid_build_command")
        self.assertIn("unsupported shell", str(raised.exception))

    def test_multiple_output_paths_are_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "dist").mkdir()
            (source / "package.json").write_text("{}")
            result = execute_build(recipe(["true"], output={"mode": "paths", "paths": ["dist", "package.json"]}), {}, source, dry_run=False)
        self.assertEqual(result["output"]["kind"], "collection")
        self.assertEqual([row["configured_path"] for row in result["output"]["paths"]], ["dist", "package.json"])

    def test_dry_run_redacts_secret_options_in_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = execute_build(recipe(["tool --token private-value"]), {}, temporary, dry_run=True)
        self.assertNotIn("private-value", str(result))
        self.assertIn("[REDACTED]", result["plan"]["commands"][0]["command"])

    def test_real_build_preserves_order_environment_cwd_and_result_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "work").mkdir()
            commands = [
                f"{shlex.quote(sys.executable)} -c 'import os,pathlib; pathlib.Path(\"one\").write_text(os.environ[\"VALUE\"])'",
                f"{shlex.quote(sys.executable)} -c 'import pathlib; pathlib.Path(\"two\").write_text(pathlib.Path(\"one\").read_text())'",
            ]
            (source / "dist").mkdir()
            result = execute_build(recipe(commands, "work", {"VALUE":"ok"}), {}, source, dry_run=False)
            self.assertEqual((source / "work/two").read_text(), "ok")
            self.assertEqual([row["index"] for row in result["commands"]], [1, 2])
            for row in result["commands"]:
                for field in ("arguments", "working_directory", "status", "exit_code", "stdout", "stderr", "duration", "timed_out"):
                    self.assertIn(field, row)
            self.assertEqual(result["output"]["path"], str(source / "dist"))

    def test_real_build_forwards_streaming_output_with_command_index(self):
        streamed = []
        def runner(command, **kwargs):
            kwargs["on_output"]({"stream": "stdout", "text": "working\n"})
            return {"command":command,"arguments":[command],"working_directory":"/source","configured_working_directory":".","status":"success","exit_code":0,"stdout":"working\n","stderr":"","duration":0.1,"timed_out":False}
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "dist").mkdir()
            execute_build(recipe(["build"], output={"mode":"path","path":"dist"}), {}, source, dry_run=False, runner=runner, on_output=lambda index, item: streamed.append((index, item)))
        self.assertEqual(streamed, [(1, {"stream": "stdout", "text": "working\n"})])

    def test_build_forwards_inactivity_and_maximum_runtime_limits(self):
        seen = {}
        def runner(command, **kwargs):
            seen.update(kwargs)
            return {"command":command,"arguments":[command],"working_directory":"/source","configured_working_directory":".","status":"success","exit_code":0,"stdout":"","stderr":"","duration":0.1,"timed_out":False}
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "dist").mkdir()
            configured = recipe(["build"], output={"mode":"path","path":"dist"})
            configured["build"]["inactivity_timeout"] = 42
            configured["build"]["maximum_runtime"] = 900
            result = execute_build(configured, {}, source, dry_run=False, runner=runner)
        self.assertEqual(result["plan"]["inactivity_timeout"], 42)
        self.assertEqual(result["plan"]["maximum_runtime"], 900)
        self.assertEqual(seen["inactivity_timeout"], 42)
        self.assertEqual(seen["maximum_runtime"], 900)

    def test_stops_at_first_failed_command_with_real_error(self):
        calls = []
        def runner(command, **_kwargs):
            calls.append(command)
            return {"command":command,"arguments":[command],"working_directory":"/source","configured_working_directory":".","status":"failed","exit_code":7,"stdout":"partial","stderr":"specific failure","duration":0.2,"timed_out":False}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as raised:
                execute_build(recipe(["first", "never"]), {}, temporary, dry_run=False, runner=runner)
        self.assertEqual(raised.exception.code, "build_command_failed")
        self.assertIn("exit code 7", str(raised.exception))
        self.assertEqual(calls, ["first"])
        self.assertEqual(raised.exception.details["failed_command"]["stderr"], "specific failure")

    def test_timeout_is_a_distinct_build_error(self):
        result = {"command":"slow","arguments":["slow"],"working_directory":"/source","configured_working_directory":".","status":"failed","exit_code":None,"stdout":"","stderr":"timed out","duration":1.0,"timed_out":True}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as raised:
                execute_build(recipe(["slow"]), {}, temporary, dry_run=False, runner=lambda *_a, **_k: result)
        self.assertEqual(raised.exception.code, "build_command_timeout")

    def test_missing_path_output_and_source_output(self):
        success = lambda command, **_kwargs: {"command":command,"arguments":[command],"working_directory":"/source","configured_working_directory":".","status":"success","exit_code":0,"stdout":"","stderr":"","duration":0.1,"timed_out":False}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as raised:
                execute_build(recipe(["true"]), {}, temporary, dry_run=False, runner=success)
            self.assertEqual(raised.exception.code, "expected_output_missing")
            source_result = execute_build(recipe(["true"], output={"mode":"source","path":""}), {}, temporary, dry_run=False, runner=success)
            self.assertEqual(source_result["output"]["mode"], "source")
            self.assertEqual(source_result["output"]["path"], str(Path(temporary).resolve()))

    def test_output_path_rejects_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "link").symlink_to(source.parent)
            for path in ("../outside", str(source.parent), "link/output"):
                with self.subTest(path=path), self.assertRaises(BuildError) as raised:
                    validate_build_plan(recipe(["true"], output={"mode":"path","path":path}), {}, source, dry_run=True)
                self.assertEqual(raised.exception.code, "unsafe_output_path")


if __name__ == "__main__":
    unittest.main()
