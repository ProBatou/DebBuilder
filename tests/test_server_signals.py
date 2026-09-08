import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from debbuilder.command_containment import CGROUP_ROOT, containment_capability, loaded_command_units
from debbuilder.command_identity import ACTIVE_COMMAND_FILE


def recipe(name="signal-fixture"):
    return {
        "schema_version": 1,
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "DEV Test <dev@example.test>",
            "description": "Graceful shutdown signal fixture",
        },
        "source": {
            "provider": "github",
            "repository": f"owner/{name}",
            "tracking": "latest_release",
            "version": {"source": "tag"},
        },
    }


class ServerSignalSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.repository = self.root / "repository"
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            output_thread = getattr(process, "output_thread", None)
            if output_thread is not None:
                output_thread.join(timeout=2)
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()

    def launch(self, *, shutdown_timeout=10, stuck_worker=False, pause_phase=None):
        environment = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "DEBBUILDER_DATA_DIR": str(self.data),
            "DEBBUILDER_REPO_ROOT": str(self.repository),
            "DEBBUILDER_HOST": "127.0.0.1",
            "DEBBUILDER_PORT": "0",
            "DEBBUILDER_AUTH_MODE": "none",
        }
        if pause_phase == "authentication":
            environment["DEBBUILDER_AUTH_MODE"] = "oidc"
        command = [
            sys.executable, "-m", "tests.shutdown_server", "--port", "0",
            "--shutdown-timeout", str(shutdown_timeout),
        ]
        if stuck_worker:
            command.append("--stuck-worker")
        if pause_phase is not None:
            command.extend(("--pause-phase", pause_phase))
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.processes.append(process)
        process.output_lines = queue.Queue()

        def copy_output():
            for line in process.stdout:
                process.output_lines.put(line)
            process.output_lines.put(None)

        process.output_thread = threading.Thread(
            target=copy_output, name="shutdown-fixture-output", daemon=True,
        )
        process.output_thread.start()
        record = self.read_record(process, "PHASE" if pause_phase else "READY", timeout=15)
        return process, record

    def read_record(self, process, prefix, *, timeout):
        deadline = time.monotonic() + timeout
        output = []
        while time.monotonic() < deadline:
            try:
                line = process.output_lines.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if line is None:
                break
            output.append(line)
            if line.startswith(prefix + " "):
                return json.loads(line[len(prefix) + 1:])
        self.fail(
            f"subprocess did not emit {prefix}; returncode={process.poll()}; output={''.join(output)!r}"
        )

    def finish(self, process, requested_signal, *, expected_outcome=0):
        process.send_signal(requested_signal)
        summary = self.read_record(process, "SUMMARY", timeout=15)
        returncode = process.wait(timeout=5)
        process.output_thread.join(timeout=2)
        process.stdout.close()
        self.assertEqual(returncode, expected_outcome)
        self.assertEqual(summary["outcome"], expected_outcome)
        return summary

    def finish_startup_signal(self, process, requested_signal, *, listener_created=True):
        process.send_signal(requested_signal)
        process.stdin.write("x")
        process.stdin.flush()
        summary = self.read_record(process, "SUMMARY", timeout=15)
        returncode = process.wait(timeout=5)
        self.assertEqual(returncode, 0)
        self.assertEqual(summary["outcome"], 0)
        self.assertEqual(summary["listener_closed"], listener_created)
        self.assertEqual(summary["lifecycle_threads"], [])
        return summary

    @staticmethod
    def post(port, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_idle_sigterm_exits_cleanly(self):
        process, _ready = self.launch()
        summary = self.finish(process, signal.SIGTERM)
        self.assertEqual(summary["runs"], [])

    def test_idle_sigint_exits_cleanly(self):
        process, _ready = self.launch()
        summary = self.finish(process, signal.SIGINT)
        self.assertEqual(summary["runs"], [])

    def test_active_command_sigterm_uses_available_containment_and_restart_needs_no_repair(self):
        before_units = loaded_command_units()
        process, ready = self.launch()
        status, response = self.post(
            ready["port"], "/api/run", {"workflow": recipe(), "dry_run": False},
        )
        self.assertEqual(status, 202)
        active = self.read_record(process, "ACTIVE", timeout=15)
        self.assertEqual(active["run_id"], response["run_id"])
        run_dir = self.data / "builds" / response["run_id"]
        identity_path = run_dir / ACTIVE_COMMAND_FILE
        identity = json.loads(identity_path.read_text())
        capability = containment_capability()
        expected_backend = "systemd_cgroup" if capability.available else "process_group"
        self.assertEqual(identity["backend"], expected_backend)

        summary = self.finish(process, signal.SIGTERM)
        persisted = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation"]["reason"], "server_shutdown")
        self.assertFalse(identity_path.exists())
        self.assertFalse(set(loaded_command_units()) - before_units)
        if identity["backend"] == "systemd_cgroup":
            self.assertFalse((CGROUP_ROOT / identity["control_group"].removeprefix("/")).exists())
        else:
            with self.assertRaises(ProcessLookupError):
                os.killpg(identity["process_group"], 0)
        self.assertEqual(summary["runs"][0]["status"], "cancelled")

        restarted, restart_ready = self.launch()
        recovery = restart_ready["recovery"]
        self.assertEqual(recovery.get("recovered_run_ids"), [])
        self.assertFalse(recovery["admission_blocked"])
        self.assertEqual(recovery["blockers"], [])
        restarted_summary = self.finish(restarted, signal.SIGTERM)
        self.assertEqual(restarted_summary["runs"][0]["status"], "cancelled")

    def test_incomplete_target_keeps_non_daemon_owner_until_external_termination(self):
        process, ready = self.launch(shutdown_timeout=0, stuck_worker=True)
        status, response = self.post(
            ready["port"], "/api/run", {"workflow": recipe("stuck-worker"), "dry_run": False},
        )
        self.assertEqual(status, 202)
        active = self.read_record(process, "ACTIVE", timeout=10)
        self.assertEqual(active["run_id"], response["run_id"])

        process.send_signal(signal.SIGTERM)
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        self.assertIsNone(process.poll())
        process.kill()
        process.wait(timeout=5)

        restarted, restart_ready = self.launch()
        recovery = restart_ready["recovery"]
        self.assertTrue(recovery["admission_blocked"])
        self.assertIn(response["run_id"], [row.get("run_id") for row in recovery["blockers"]])
        self.finish(restarted, signal.SIGTERM)

    def test_sigterm_during_each_stateful_startup_phase_unwinds_safely(self):
        for phase in (
            "directories", "recovery", "authentication", "migration",
            "reconciliation", "manager_start", "retention",
        ):
            with self.subTest(phase=phase):
                process, record = self.launch(pause_phase=phase)
                self.assertEqual(record["name"], phase)
                self.finish_startup_signal(
                    process, signal.SIGTERM, listener_created=phase != "directories",
                )

                restarted, ready = self.launch()
                self.assertFalse(ready["recovery"]["admission_blocked"])
                self.finish(restarted, signal.SIGTERM)

    def test_shutdown_waits_while_startup_owned_secret_persistence_is_in_progress(self):
        process, record = self.launch(pause_phase="authentication")
        self.assertEqual(record["name"], "authentication")

        process.send_signal(signal.SIGTERM)
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)
        self.assertFalse((self.data / "secrets.json").exists())

        process.stdin.write("x")
        process.stdin.flush()
        summary = self.read_record(process, "SUMMARY", timeout=15)
        returncode = process.wait(timeout=5)
        self.assertEqual(returncode, 0)
        self.assertEqual(summary["outcome"], 0)
        self.assertTrue(summary["listener_closed"])
        self.assertEqual(summary["lifecycle_threads"], [])
        self.assertTrue((self.data / "secrets.json").exists())

    def test_sigint_during_recovery_uses_the_same_startup_cleanup(self):
        process, record = self.launch(pause_phase="recovery")
        self.assertEqual(record["name"], "recovery")
        self.finish_startup_signal(process, signal.SIGINT)

    def test_import_does_not_prepare_mutable_application_directories(self):
        import_data = self.root / "import-only-data"
        environment = {
            **os.environ,
            "DEBBUILDER_DATA_DIR": str(import_data),
        }
        completed = subprocess.run(
            [sys.executable, "-c", "import debbuilder.app"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(import_data.exists())


if __name__ == "__main__":
    unittest.main()
