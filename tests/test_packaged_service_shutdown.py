import copy
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

from debbuilder import builtin_recipe, execution_recovery
from debbuilder.build_store import BuildStore
from debbuilder.command_containment import CGROUP_ROOT, containment_capability, loaded_command_units
from debbuilder.command_identity import ACTIVE_COMMAND_FILE
from debbuilder.execution_manager import DEFAULT_SHUTDOWN_TIMEOUT
from debbuilder.systemd_unit import generate_unit

from tests.test_server_signals import recipe


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_RUNTIME_DIRECTORY = Path("/run/systemd/system")


class PackagedServicePolicyTests(unittest.TestCase):
    def test_builtin_recipe_generates_the_self_specific_shutdown_policy(self):
        definition = builtin_recipe.load_builtin_definition()
        service = definition["service"]
        unit = generate_unit(service)

        self.assertEqual(service["timeout_stop_sec"], "20s")
        self.assertGreater(float(service["timeout_stop_sec"].removesuffix("s")), DEFAULT_SHUTDOWN_TIMEOUT)
        self.assertIn("TimeoutStopSec=20s\n", unit)
        self.assertIn("KillMode=control-group\n", unit)
        self.assertIn("KillSignal=SIGTERM\n", unit)
        self.assertIn("Restart=on-failure\n", unit)
        self.assertIn("RestartSec=3s\n", unit)
        # SendSIGKILL=yes is systemd's service default and is verified on the
        # loaded real unit below, so no Recipe-model expansion is needed.
        self.assertNotIn("SendSIGKILL=", unit)
        self.assertNotIn("TimeoutStartSec=", unit)

    def test_ordinary_recipe_is_not_given_the_self_shutdown_budget(self):
        unit = generate_unit({
            "name": "operator.service",
            "description": "Operator service",
            "type": "simple",
            "command": "/bin/true",
            "restart": "on-failure",
        })

        self.assertNotIn("TimeoutStopSec=", unit)
        self.assertNotIn("KillMode=", unit)
        self.assertNotIn("KillSignal=", unit)
        self.assertNotIn("RestartSec=", unit)


class RealSystemdPackagedServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest("real systemd service tests require root")
        if shutil.which("systemctl") is None or not SYSTEMD_RUNTIME_DIRECTORY.is_dir():
            raise unittest.SkipTest("a writable system systemd manager is unavailable")
        capability = containment_capability(refresh=True)
        if not capability.available:
            raise unittest.SkipTest(capability.reason)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="debbuilder-cp3-")
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.repository = self.root / "repository"
        self.output = self.root / "service.log"
        self.unit_name = f"debbuilder-cp3-{os.getpid()}-{uuid.uuid4().hex[:8]}.service"
        self.unit_path = SYSTEMD_RUNTIME_DIRECTORY / self.unit_name
        self.before_command_units = loaded_command_units()
        self.production_fragment = self._show_unit("debbuilder.service", "FragmentPath").get("FragmentPath", "")
        self.installed = False
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._cleanup_service)

    def _systemctl(self, *arguments, check=True, timeout=40):
        completed = subprocess.run(
            ["systemctl", *arguments], capture_output=True, text=True, timeout=timeout,
        )
        if check and completed.returncode != 0:
            self.fail(
                f"systemctl {' '.join(arguments)} failed ({completed.returncode}): "
                f"{completed.stdout}{completed.stderr}"
            )
        return completed

    def _show_unit(self, unit_name, *properties):
        completed = self._systemctl(
            "show", unit_name, *(f"--property={value}" for value in properties), "--no-pager",
            check=False,
        )
        values = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        return values

    def _install_service(
        self, *, ignore_command_cancellation=False, force_process_group=False,
        application_timeout=10, service_timeout=None,
    ):
        definition = builtin_recipe.load_builtin_definition()
        service = copy.deepcopy(definition["service"])
        command = [
            sys.executable, "-m", "tests.shutdown_server", "--port", "0",
            "--shutdown-timeout", str(application_timeout),
        ]
        if ignore_command_cancellation:
            command.append("--ignore-command-cancellation")
        if force_process_group:
            command.append("--force-process-group")
        service.update({
            "name": self.unit_name,
            "command": shlex.join(command),
            "working_directory": str(REPOSITORY_ROOT),
            # Keep the disposable service independent from the host's packaged
            # environment; its isolated values are declared immediately below.
            "environment_files": [],
            "environment": {
                "PYTHONUNBUFFERED": "1",
                "DEBBUILDER_DATA_DIR": str(self.data),
                "DEBBUILDER_REPO_ROOT": str(self.repository),
                "DEBBUILDER_HOST": "127.0.0.1",
                "DEBBUILDER_PORT": "0",
                "DEBBUILDER_AUTH_MODE": "none",
            },
            "standard_output": f"append:{self.output}",
            "standard_error": f"append:{self.output}",
        })
        if service_timeout is not None:
            service["timeout_stop_sec"] = service_timeout
        self.unit_path.write_text(generate_unit(service), encoding="utf-8")
        self.installed = True
        self._systemctl("daemon-reload")

    def _start(self, *, ready_occurrence=1):
        self._systemctl("start", self.unit_name)
        return self._wait_record("READY", occurrence=ready_occurrence, timeout=15)

    def _wait_record(self, prefix, *, occurrence=1, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                lines = self.output.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            matches = [line for line in lines if line.startswith(prefix + " ")]
            if len(matches) >= occurrence:
                return json.loads(matches[occurrence - 1][len(prefix) + 1:])
            time.sleep(0.05)
        state = self._show_unit(self.unit_name, "ActiveState", "SubState", "Result")
        output = self.output.read_text(encoding="utf-8") if self.output.exists() else ""
        self.fail(f"missing {prefix} record {occurrence}; state={state!r}; output={output!r}")

    def _record_count(self, prefix):
        if not self.output.exists():
            return 0
        return sum(
            line.startswith(prefix + " ")
            for line in self.output.read_text(encoding="utf-8").splitlines()
        )

    @staticmethod
    def _post(port, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/run",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def _active_run(self, ready, *, name="packaged-service"):
        status, response = self._post(ready["port"], recipe(name))
        self.assertEqual(status, 202)
        active = self._wait_record("ACTIVE", occurrence=1, timeout=15)
        self.assertEqual(active["run_id"], response["run_id"])
        return response["run_id"]

    def _run_path(self, run_id):
        return self.data / "builds" / run_id

    def _load_run(self, run_id):
        return json.loads((self._run_path(run_id) / "run.json").read_text(encoding="utf-8"))

    def _cleanup_service(self):
        if not self.installed:
            return
        try:
            self._systemctl("stop", self.unit_name, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            pass
        try:
            if (self.data / "builds").is_dir():
                execution_recovery.recover_startup(BuildStore(self.data / "builds"))
        except Exception:
            pass
        self._systemctl("reset-failed", self.unit_name, check=False)
        try:
            self.unit_path.unlink()
        except FileNotFoundError:
            pass
        self._systemctl("daemon-reload", check=False)
        self.assertEqual(
            self._show_unit("debbuilder.service", "FragmentPath").get("FragmentPath", ""),
            self.production_fragment,
        )

    def test_clean_stop_cancels_active_and_queued_runs_and_stays_stopped(self):
        self._install_service()
        ready = self._start()
        active_run_id = self._active_run(ready, name="service-stop-active")
        status, queued = self._post(ready["port"], recipe("service-stop-queued"))
        self.assertEqual(status, 202)

        stopped = self._systemctl("stop", self.unit_name, check=False, timeout=30)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        summary = self._wait_record("SUMMARY", timeout=10)
        state = self._show_unit(self.unit_name, "ActiveState", "Result", "NRestarts")
        self.assertEqual(state["ActiveState"], "inactive")
        self.assertEqual(state["Result"], "success")
        restarts = state["NRestarts"]
        time.sleep(3.2)
        after = self._show_unit(self.unit_name, "ActiveState", "NRestarts")
        self.assertEqual(after, {"NRestarts": restarts, "ActiveState": "inactive"})
        self.assertEqual(self._record_count("READY"), 1)

        for run_id in (active_run_id, queued["run_id"]):
            run = self._load_run(run_id)
            self.assertEqual(run["status"], "cancelled")
            self.assertEqual(run["cancellation"]["reason"], "server_shutdown")
        self.assertEqual(summary["lifecycle_threads"], [])
        self.assertEqual(loaded_command_units(), self.before_command_units)

    def test_restart_is_graceful_and_on_failure_restarts_an_unexpected_kill(self):
        self._install_service()
        first_ready = self._start()
        run_id = self._active_run(first_ready, name="service-restart")
        first = self._show_unit(self.unit_name, "MainPID", "InvocationID", "NRestarts")

        restarted = self._systemctl("restart", self.unit_name, check=False, timeout=30)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        second_ready = self._wait_record("READY", occurrence=2, timeout=15)
        second = self._show_unit(self.unit_name, "MainPID", "InvocationID", "NRestarts", "ActiveState")
        self.assertEqual(second["ActiveState"], "active")
        self.assertNotEqual(second["MainPID"], first["MainPID"])
        self.assertNotEqual(second["InvocationID"], first["InvocationID"])
        self.assertEqual(second_ready["recovery"]["recovered_run_ids"], [])
        self.assertFalse(second_ready["recovery"]["admission_blocked"])
        self.assertEqual(self._load_run(run_id)["status"], "cancelled")
        self.assertEqual(self._record_count("ACTIVE"), 1)

        killed = self._systemctl(
            "kill", "--kill-whom=main", f"--signal={signal.SIGKILL}", self.unit_name,
            check=False,
        )
        self.assertEqual(killed.returncode, 0, killed.stderr)
        third_ready = self._wait_record("READY", occurrence=3, timeout=10)
        third = self._show_unit(self.unit_name, "MainPID", "NRestarts", "Restart", "RestartUSec")
        self.assertNotEqual(third["MainPID"], second["MainPID"])
        self.assertGreaterEqual(int(third["NRestarts"]), int(second["NRestarts"]) + 1)
        self.assertEqual(third["Restart"], "on-failure")
        self.assertEqual(third["RestartUSec"], "3s")
        self.assertFalse(third_ready["recovery"]["admission_blocked"])
        self.assertEqual(self._record_count("ACTIVE"), 1)

    def test_hard_timeout_preserves_sibling_transient_unit_for_startup_recovery(self):
        self._install_service(ignore_command_cancellation=True)
        ready = self._start()
        run_id = self._active_run(ready, name="service-hard-timeout")
        run_path = self._run_path(run_id)
        identity_path = run_path / ACTIVE_COMMAND_FILE
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        self.assertEqual(identity["backend"], "systemd_cgroup")

        main = self._show_unit(
            self.unit_name, "ControlGroup", "TimeoutStopUSec", "KillMode", "KillSignal",
            "SendSIGKILL", "Restart", "RestartUSec", "Type",
        )
        command = self._show_unit(identity["unit_name"], "ControlGroup", "ActiveState")
        self.assertEqual(main["TimeoutStopUSec"], "20s")
        self.assertEqual(main["KillMode"], "control-group")
        self.assertEqual(main["KillSignal"], "15")
        self.assertEqual(main["SendSIGKILL"], "yes")
        self.assertEqual(main["Restart"], "on-failure")
        self.assertEqual(main["RestartUSec"], "3s")
        self.assertEqual(main["Type"], "simple")
        self.assertEqual(command["ActiveState"], "active")
        self.assertEqual(command["ControlGroup"], identity["control_group"])
        self.assertNotEqual(command["ControlGroup"], main["ControlGroup"])
        self.assertEqual(PurePosixPath(command["ControlGroup"]).parent, PurePosixPath(main["ControlGroup"]).parent)

        started = time.monotonic()
        stopped = self._systemctl("stop", self.unit_name, check=False, timeout=35)
        elapsed = time.monotonic() - started
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertGreaterEqual(elapsed, 18)
        result = self._show_unit(
            self.unit_name, "ActiveState", "Result", "ExecMainCode", "ExecMainStatus", "NRestarts",
        )
        self.assertEqual(result["ActiveState"], "failed")
        self.assertEqual(result["Result"], "timeout")
        self.assertEqual(result["ExecMainCode"], "2")
        self.assertEqual(result["ExecMainStatus"], "9")
        self.assertTrue(identity_path.is_file())
        self.assertIn(self._load_run(run_id)["status"], {"running", "cancelling"})
        self.assertEqual(self._show_unit(identity["unit_name"], "ActiveState")["ActiveState"], "active")
        events = (CGROUP_ROOT / identity["control_group"].removeprefix("/") / "cgroup.events").read_text()
        self.assertIn("populated 1", events)
        time.sleep(3.2)
        self.assertEqual(self._record_count("READY"), 1)

        recovered_ready = self._start(ready_occurrence=2)
        recovery = recovered_ready["recovery"]
        self.assertIn(run_id, recovery["recovered_run_ids"])
        self.assertFalse(recovery["admission_blocked"])
        self.assertFalse(identity_path.exists())
        self.assertEqual(self._show_unit(identity["unit_name"], "LoadState")["LoadState"], "not-found")
        self.assertFalse((CGROUP_ROOT / identity["control_group"].removeprefix("/")).exists())
        recovered_run = self._load_run(run_id)
        self.assertEqual(recovered_run["status"], "failed")
        self.assertEqual(recovered_run["error"]["code"], "execution_interrupted")
        self.assertEqual(self._record_count("ACTIVE"), 1)

    def test_hard_timeout_process_group_fallback_remains_fail_closed(self):
        # The shorter outer bound keeps this fallback limitation test focused;
        # the strong-containment test above exercises the packaged 20 seconds.
        self._install_service(
            ignore_command_cancellation=True, force_process_group=True,
            application_timeout=0, service_timeout="3s",
        )
        ready = self._start()
        run_id = self._active_run(ready, name="service-fallback-timeout")
        identity_path = self._run_path(run_id) / ACTIVE_COMMAND_FILE
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        self.assertEqual(identity["backend"], "process_group")
        main_group = self._show_unit(self.unit_name, "ControlGroup")["ControlGroup"]
        process_group = Path(f"/proc/{identity['pid']}/cgroup").read_text().strip().split("::", 1)[1]
        self.assertEqual(process_group, main_group)

        stopped = self._systemctl("stop", self.unit_name, check=False, timeout=10)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        result = self._show_unit(self.unit_name, "Result", "ExecMainStatus")
        self.assertEqual(result["Result"], "timeout")
        self.assertEqual(result["ExecMainStatus"], "9")
        with self.assertRaises(ProcessLookupError):
            os.killpg(identity["pgid"], 0)
        self.assertTrue(identity_path.is_file())
        self.assertIn(self._load_run(run_id)["status"], {"running", "cancelling"})

        recovered_ready = self._start(ready_occurrence=2)
        recovery = recovered_ready["recovery"]
        self.assertTrue(recovery["admission_blocked"])
        blocker = next(row for row in recovery["blockers"] if row.get("run_id") == run_id)
        self.assertEqual(blocker["backend"], "process_group")
        self.assertIn("descendants cannot be excluded", blocker["reason"])
        self.assertEqual(self._record_count("ACTIVE"), 1)
        self.assertTrue(identity_path.is_file())
        self.assertIn(self._load_run(run_id)["status"], {"running", "cancelling"})


if __name__ == "__main__":
    unittest.main()
