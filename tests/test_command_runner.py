import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.command_runner import parse_command, resolve_working_directory, run_command


class CommandRunnerTests(unittest.TestCase):
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
            with mock.patch("debbuilder.command_runner.subprocess.run", wraps=__import__("subprocess").run) as invoked:
                result = run_command(command, workspace=root, working_directory="source")
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["working_directory"], str(root / "source"))
            self.assertEqual(result["configured_working_directory"], "source")
            self.assertEqual(result["stdout"].strip(), "source")
            self.assertIs(invoked.call_args.kwargs["shell"], False)

    def test_redacts_secret_environment_values_from_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import os; print(os.environ[\"API_TOKEN\"])'"
            result = run_command(command, workspace=temporary, environment={"API_TOKEN": "super-private-value"})
            self.assertEqual(result["status"], "success")
            self.assertNotIn("super-private-value", result["stdout"])
            self.assertIn("[REDACTED]", result["stdout"])
            self.assertNotIn("super-private-value", json.dumps(result))

    def test_redacts_secret_command_options_from_audit_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_command("printf --token super-secret", workspace=temporary)
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertIn("[REDACTED]", result["command"])

    def test_timeout_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(1)'"
            result = run_command(command, workspace=temporary, timeout=0.01)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["timed_out"])
            self.assertIsNone(result["exit_code"])


if __name__ == "__main__":
    unittest.main()
