import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.command_runner import controlled_environment, parse_command, resolve_working_directory, run_command
from debbuilder.workspace_cleanup import _require_unused_workspace


class CommandRunnerTests(unittest.TestCase):
    def _process_tree_command(self, workspace: Path, levels: int, *, ignore_term: bool = False) -> tuple[str, Path]:
        script = workspace / "process_tree.py"
        marker = workspace / "pids"
        script.write_text(
            "import os,signal,subprocess,sys,time\n"
            "level=int(sys.argv[1])\n"
            "marker=sys.argv[2]\n"
            "ignore=sys.argv[3] == 'ignore'\n"
            "fd=os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "os.write(fd, (str(os.getpid()) + '\\n').encode())\n"
            "os.close(fd)\n"
            "if ignore: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "if level: subprocess.Popen([sys.executable, __file__, str(level-1), marker, sys.argv[3]])\n"
            "while True: time.sleep(0.05)\n"
        )
        command = " ".join((
            shlex.quote(sys.executable), shlex.quote(str(script)), str(levels),
            shlex.quote(str(marker)), "ignore" if ignore_term else "default",
        ))
        return command, marker

    def _assert_processes_gone(self, pids: list[int]) -> None:
        deadline = time.monotonic() + 1
        remaining = set(pids)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    remaining.remove(pid)
            if remaining:
                time.sleep(0.01)
        self.assertEqual(remaining, set(), f"processes still exist: {sorted(remaining)}")

    def test_controlled_environment_preserves_source_home_without_workspace_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            source_home = Path(temporary) / "real-home"
            source_home.mkdir()
            environment = controlled_environment(
                workspace,
                environ={"HOME": str(source_home), "PATH": "/custom/bin", "UNRELATED": "ignored"},
            )
        self.assertEqual(environment["HOME"], str(source_home))
        self.assertNotEqual(environment["HOME"], str(workspace.resolve()))
        self.assertNotIn("UNRELATED", environment)

    def test_controlled_environment_does_not_hardcode_root_when_home_is_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = controlled_environment(temporary, environ={"PATH": "/custom/bin"})
        self.assertNotIn("HOME", environment)
        self.assertNotIn("/root", environment.values())

    def test_recipe_environment_can_explicitly_override_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_home = str(Path(temporary) / "source-home")
            recipe_home = str(Path(temporary) / "recipe-home")
            environment = controlled_environment(
                temporary,
                additions={"HOME": recipe_home, "CUSTOM": "value"},
                environ={"HOME": source_home, "PATH": "/custom/bin"},
            )
        self.assertEqual(environment["HOME"], recipe_home)
        self.assertEqual(environment["CUSTOM"], "value")

    def test_rejects_shell_operators_and_cd_with_clear_errors(self):
        rejected = ["echo ok && touch bad", "echo ok | tee out", "echo ok > out", "echo $(id)", "echo `id`", "one; two"]
        for command in rejected:
            with self.subTest(command=command):
                result = run_command(command, workspace=tempfile.gettempdir())
                self.assertEqual(result["status"], "failed")
                self.assertIn("unsupported", result["stderr"])
        with self.assertRaisesRegex(ValueError, "working_directory"):
            parse_command("cd source")

    def test_quoted_metacharacter_is_an_argument_not_a_shell_operator(self):
        self.assertEqual(parse_command("printf '%s' 'a|b'"), ["printf", "%s", "a|b"])

    def test_working_directory_must_exist_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            self.assertEqual(resolve_working_directory(root, "source"), root / "source")
            with self.assertRaisesRegex(ValueError, "escapes"):
                resolve_working_directory(root, "../outside")
            with self.assertRaisesRegex(ValueError, "relative"):
                resolve_working_directory(root, "/tmp")

    def test_executes_without_shell_and_reports_real_cwd_and_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            command = f"{shlex.quote(sys.executable)} -c 'import os; print(os.path.basename(os.getcwd()))'"
            with mock.patch("debbuilder.command_runner.subprocess.Popen", wraps=__import__("subprocess").Popen) as invoked:
                result = run_command(command, workspace=root, working_directory="source")
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["working_directory"], str(root / "source"))
            self.assertEqual(result["configured_working_directory"], "source")
            self.assertEqual(result["stdout"].strip(), "source")
            self.assertIs(invoked.call_args.kwargs["shell"], False)
            self.assertIs(invoked.call_args.kwargs["start_new_session"], True)

    def test_command_runs_in_its_own_session_and_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import os; print(os.getpid(), os.getpgid(0), os.getsid(0))'"
            result = run_command(command, workspace=temporary)
        pid, process_group, session = map(int, result["stdout"].split())
        self.assertEqual(result["status"], "success")
        self.assertEqual((process_group, session), (pid, pid))

    def test_streams_stdout_and_stderr_while_preserving_final_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            chunks = []
            command = f"{shlex.quote(sys.executable)} -c 'import sys,time; print(\"out-one\", flush=True); print(\"err-one\", file=sys.stderr, flush=True); time.sleep(.05); print(\"out-two\", flush=True)'"
            result = run_command(command, workspace=temporary, on_output=chunks.append)
        self.assertEqual(result["status"], "success")
        self.assertIn("out-one", result["stdout"])
        self.assertIn("out-two", result["stdout"])
        self.assertIn("err-one", result["stderr"])
        self.assertTrue(any(row["stream"] == "stdout" and "out-one" in row["text"] for row in chunks))
        self.assertTrue(any(row["stream"] == "stderr" and "err-one" in row["text"] for row in chunks))

    def test_timeout_preserves_output_emitted_during_graceful_termination(self):
        with tempfile.TemporaryDirectory() as temporary:
            chunks = []
            command = f"{shlex.quote(sys.executable)} -c 'import signal,time; signal.signal(signal.SIGTERM, lambda *_: (print(\"term-output\", flush=True), exit(0))); print(\"started\", flush=True); time.sleep(5)'"
            result = run_command(command, workspace=temporary, inactivity_timeout=0.2, on_output=chunks.append)
        self.assertTrue(result["timed_out"])
        self.assertIn("started", result["stdout"])
        self.assertIn("term-output", result["stdout"])
        self.assertTrue(any("term-output" in item["text"] for item in chunks))

    def test_redacts_secret_environment_values_from_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import os; print(os.environ[\"API_TOKEN\"])'"
            result = run_command(command, workspace=temporary, environment={"API_TOKEN": "super-private-value"})
            self.assertEqual(result["status"], "success")
            self.assertNotIn("super-private-value", result["stdout"])
            self.assertIn("[REDACTED]", result["stdout"])
            self.assertNotIn("super-private-value", json.dumps(result))

    def test_redacts_secret_values_from_streamed_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            chunks = []
            command = f"{shlex.quote(sys.executable)} -c 'import os; print(os.environ[\"API_TOKEN\"], flush=True)'"
            result = run_command(command, workspace=temporary, environment={"API_TOKEN": "super-private-value"}, on_output=chunks.append)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("super-private-value", json.dumps(chunks + [result]))
        self.assertIn("[REDACTED]", json.dumps(chunks))

    def test_redacts_secret_command_options_from_audit_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_command("printf --token super-secret", workspace=temporary)
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertIn("[REDACTED]", result["command"])

    def test_silent_command_hits_inactivity_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(1)'"
            result = run_command(command, workspace=temporary, inactivity_timeout=0.05)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["timeout_reason"], "inactivity")
            self.assertIsNone(result["exit_code"])
            self.assertIn("without stdout/stderr activity", result["stderr"])

    def test_stdout_activity_resets_inactivity_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import time; [print(i, flush=True) or time.sleep(.03) for i in range(4)]'"
            result = run_command(command, workspace=temporary, inactivity_timeout=0.08)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["timed_out"])
        self.assertIn("3", result["stdout"])

    def test_stderr_activity_resets_inactivity_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import sys,time; [print(i, file=sys.stderr, flush=True) or time.sleep(.03) for i in range(4)]'"
            result = run_command(command, workspace=temporary, inactivity_timeout=0.08)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["timed_out"])
        self.assertIn("3", result["stderr"])

    def test_maximum_runtime_is_optional_and_absolute(self):
        with tempfile.TemporaryDirectory() as temporary:
            noisy = f"{shlex.quote(sys.executable)} -c 'import time; [print(i, flush=True) or time.sleep(.03) for i in range(20)]'"
            limited = run_command(noisy, workspace=temporary, inactivity_timeout=0.2, maximum_runtime=0.08)
            unlimited = run_command(f"{shlex.quote(sys.executable)} -c 'print(\"ok\")'", workspace=temporary, inactivity_timeout=0.2, maximum_runtime=None)
        self.assertEqual(limited["status"], "failed")
        self.assertTrue(limited["timed_out"])
        self.assertEqual(limited["timeout_reason"], "maximum_runtime")
        self.assertIn("maximum runtime", limited["stderr"])
        self.assertEqual(unlimited["status"], "success")
        self.assertFalse(unlimited["timed_out"])

    def test_none_disables_inactivity_timeout_without_disabling_maximum_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(.12); print(\"done\")'"
            result = run_command(command, workspace=temporary, inactivity_timeout=None, maximum_runtime=1)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["timed_out"])
        self.assertIn("done", result["stdout"])

    def test_zero_and_negative_timeout_values_are_rejected_not_disabled(self):
        for name, kwargs in (
            ("zero inactivity", {"inactivity_timeout": 0}),
            ("negative inactivity", {"inactivity_timeout": -1}),
            ("zero runtime", {"maximum_runtime": 0}),
            ("negative runtime", {"maximum_runtime": -1}),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                result = run_command("true", workspace=temporary, **kwargs)
            self.assertEqual(result["status"], "failed")
            self.assertIn("positive number or None", result["stderr"])

    def test_inactivity_timeout_stops_parent_and_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            command, marker = self._process_tree_command(workspace, 1)
            result = run_command(command, workspace=workspace, inactivity_timeout=0.5)
            pids = [int(row) for row in marker.read_text().splitlines()]
            self.assertEqual(len(pids), 2)
            self._assert_processes_gone(pids)
            _require_unused_workspace(workspace)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["timeout_reason"], "inactivity")
        self.assertEqual(result["termination_error"], "")

    def test_inactivity_timeout_stops_parent_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            command, marker = self._process_tree_command(workspace, 2)
            result = run_command(command, workspace=workspace, inactivity_timeout=0.5)
            pids = [int(row) for row in marker.read_text().splitlines()]
            self.assertEqual(len(pids), 3)
            self._assert_processes_gone(pids)
            _require_unused_workspace(workspace)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["timeout_reason"], "inactivity")
        self.assertEqual(result["termination_error"], "")

    def test_timeout_cleans_up_process_that_ignores_terminate(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(5)'"
            result = run_command(command, workspace=temporary, inactivity_timeout=0.2)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["timeout_reason"], "inactivity")
        self.assertTrue(result["killed"])
        self.assertEqual(result["process_exit_code"], -9)

    def test_timeout_force_kills_tree_that_ignores_terminate(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            command, marker = self._process_tree_command(workspace, 2, ignore_term=True)
            result = run_command(command, workspace=workspace, inactivity_timeout=0.5)
            pids = [int(row) for row in marker.read_text().splitlines()]
            self.assertEqual(len(pids), 3)
            self._assert_processes_gone(pids)
            _require_unused_workspace(workspace)
        self.assertTrue(result["killed"])
        self.assertEqual(result["termination_error"], "")


if __name__ == "__main__":
    unittest.main()
