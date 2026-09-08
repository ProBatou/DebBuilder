import json
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.ui import behavior_lab


ROOT = Path(__file__).resolve().parents[1]


class BehaviorLabTests(unittest.TestCase):
    def start_lab(self, scenario="showcase", module="tests.ui.behavior_lab", host="127.0.0.1"):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = [sys.executable, "-m", module, "--port", str(port)]
        if module == "tests.ui.behavior_lab":
            command[3:3] = ["--scenario", scenario, "--host", host]
        process = subprocess.Popen(
            command,
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        lines = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if line:
                lines.append(line.rstrip())
                if line.startswith("Runtime: "):
                    break
            elif process.poll() is not None:
                self.fail(process.stderr.read())
        else:
            process.terminate()
            self.fail("Behavior Lab did not report its runtime")
        url = next(line.removeprefix("URL: ") for line in lines if line.startswith("URL: "))
        runtime = Path(next(line.removeprefix("Runtime: ") for line in lines if line.startswith("Runtime: ")))
        self.addCleanup(self.stop_lab, process, runtime)
        return process, url, runtime

    @staticmethod
    def api(url, path, body=None, method=None):
        request = Request(f"{url}{path}", data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"} if body is not None else {}, method=method or ("POST" if body is not None else "GET"))
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def blocked(self, url, path, body=None, method=None):
        with self.assertRaises(HTTPError) as rejected:
            self.api(url, path, body, method)
        self.assertEqual(rejected.exception.code, 403)
        payload = json.load(rejected.exception)
        self.assertEqual(payload["error"]["code"], "behavior_lab_action_blocked")
        self.assertNotIn("/tmp/", payload["error"]["message"])

    def wait_for(self, callback):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(.05)
        self.fail("Behavior Lab did not reach the expected state")

    def submit(self, url, name, *, dry_run=False):
        workflow = self.api(url, f"/api/workflows/{name}")
        return self.api(url, "/api/run", {"workflow": workflow, "dry_run": dry_run})["run_id"]

    @staticmethod
    def stop_lab(process, runtime):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
        shutil.rmtree(runtime, ignore_errors=True)

    def test_showcase_registry_and_unknown_scenario(self):
        selected = behavior_lab.scenario("showcase")
        self.assertEqual(selected.name, "showcase")
        with self.assertRaisesRegex(ValueError, "Unknown Behavior Lab scenario"):
            behavior_lab.scenario("missing")

    def test_scenario_listing(self):
        listed = subprocess.run([sys.executable, "-m", "tests.ui.behavior_lab", "--list-scenarios"], cwd=ROOT, text=True, capture_output=True, check=True)
        for name in behavior_lab.SCENARIOS:
            self.assertIn(f"{name}:", listed.stdout)

    def test_seed_failure_removes_its_new_runtime(self):
        before = set(Path("/tmp").glob("debbuilder-behavior-lab-*"))

        def fail_seed(_data_dir, _repository_root):
            raise RuntimeError("seed failure")

        with self.assertRaisesRegex(RuntimeError, "seed failure"):
            behavior_lab.create_isolated_runtime(
                behavior_lab.Scenario("failed-seed", "test only", fail_seed)
            )
        self.assertEqual(set(Path("/tmp").glob("debbuilder-behavior-lab-*")), before)

    def test_default_and_lan_bind_are_reported(self):
        _loopback, loopback_url, _runtime = self.start_lab()
        self.assertTrue(loopback_url.startswith("http://127.0.0.1:"))
        _lan, lan_url, _lan_runtime = self.start_lab(host="0.0.0.0")
        self.assertTrue(lan_url.startswith("http://127.0.0.1:"))
        with urlopen(f"{lan_url}/api/status", timeout=5) as response:
            self.assertTrue(json.load(response)["ok"])

    def test_cli_validation_and_bind_failures_are_concise(self):
        for port in ("0", "-1", "65536", "not-a-port"):
            invalid = subprocess.run([sys.executable, "-m", "tests.ui.behavior_lab", "--port", port], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("port", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)
        invalid = subprocess.run([sys.executable, "-m", "tests.ui.behavior_lab", "--host", "not-an-address", "--port", "8765"], cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("Behavior Lab: cannot bind", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        try:
            collision = subprocess.run([sys.executable, "-m", "tests.ui.behavior_lab", "--port", str(held.getsockname()[1])], cwd=ROOT, text=True, capture_output=True)
            self.assertNotEqual(collision.returncode, 0)
            self.assertIn("Behavior Lab: cannot bind", collision.stderr)
            self.assertNotIn("Traceback", collision.stderr)
        finally:
            held.close()

    def test_showcase_uses_isolated_runtime_and_normal_server(self):
        process, url, runtime = self.start_lab()
        self.assertTrue(runtime.is_dir())
        self.assertNotEqual(runtime / "data", Path("/var/lib/debbuilder"))
        self.assertTrue((runtime / "data" / "builds").is_dir())
        self.assertRegex(url, r"^http://127\.0\.0\.1:[1-9]\d*$")
        with urlopen(f"{url}/api/executions", timeout=5) as response:
            executions = json.load(response)["executions"]
        self.assertTrue(any(row["id"] == "ui-01-prepared" for row in executions))
        with urlopen(f"{url}/", timeout=5) as response:
            self.assertIn("DebBuilder", response.read().decode())
        request = Request(f"{url}/api/run", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as blocked:
            urlopen(request, timeout=5)
        self.assertEqual(blocked.exception.code, 403)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(runtime.exists())

    def test_behavior_lab_default_denies_recipe_and_admin_mutations(self):
        _process, url, _runtime = self.start_lab("build-failure")
        workflow = self.api(url, "/api/workflows/build-failure")
        changed = json.loads(json.dumps(workflow))
        changed["build"]["commands"] = ["/usr/bin/true"]
        self.blocked(url, "/api/run", {"workflow": changed, "dry_run": False})
        replacement = json.loads(json.dumps(workflow))
        replacement["name"] = "replacement"
        self.blocked(url, "/api/run", {"workflow": replacement, "dry_run": False})
        self.blocked(url, "/api/workflows/build-failure", {"workflow": changed})
        self.blocked(url, "/api/recipes/import", {"recipe": changed})
        self.blocked(url, "/api/settings", {"notifications": {"type": "webhook"}})
        self.blocked(url, "/api/notifications/test", {})
        self.blocked(url, "/api/packages", {"name": "other"})
        self.blocked(url, "/api/executions/delete-logs", {"all": True})
        self.blocked(url, "/api/executions/missing/publish", {})
        self.blocked(url, "/api/executions/missing/reconcile-publication", {})
        self.blocked(url, "/api/workflows/build-failure", method="DELETE")
        self.blocked(url, "/api/packages/build-failure", method="DELETE")
        self.assertEqual(self.api(url, "/api/workflows/build-failure"), workflow)
        # The untouched, captured fixture is still the only executable input.
        run_id = self.submit(url, "build-failure")
        failed = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}")["execution"] if self.api(url, f"/api/executions/{run_id}")["execution"]["status"] == "failed" else None)
        self.assertEqual(failed["error"]["stage"], "build")

    def test_two_instances_have_distinct_ports_and_mutable_state(self):
        _first, first_url, first_runtime = self.start_lab()
        _second, second_url, second_runtime = self.start_lab()
        self.assertNotEqual(first_url, second_url)
        self.assertNotEqual(first_runtime, second_runtime)
        self.assertTrue(first_runtime.is_dir())
        self.assertTrue(second_runtime.is_dir())

    def test_cancellation_running_has_live_logs_and_cancels(self):
        _process, url, _runtime = self.start_lab("cancellation-running")
        self.blocked(url, "/api/executions/not-a-scenario-run/cancel", {})
        run_id = self.submit(url, "cancellation-running")
        running = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}")["execution"] if self.api(url, f"/api/executions/{run_id}")["execution"]["status"] == "running" else None)
        log = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}/logs?verbosity=raw")["log"] if "process tree started" in self.api(url, f"/api/executions/{run_id}/logs?verbosity=raw")["log"]["text"] else None)
        self.assertEqual(running["status"], "running")
        self.assertIn("process tree started", log["text"])
        cancellation = self.api(url, f"/api/executions/{run_id}/cancel", {})["cancellation"]
        self.assertEqual(cancellation["status"], "cancelling")
        terminal = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}")["execution"] if self.api(url, f"/api/executions/{run_id}")["execution"]["status"] == "cancelled" else None)
        self.assertEqual(terminal["status"], "cancelled")

    def test_queued_cancellation_never_starts_fixture(self):
        _process, url, _runtime = self.start_lab("queued-cancellable")
        run_id = self.submit(url, "queued-cancellable")
        queued = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}")["execution"] if self.api(url, f"/api/executions/{run_id}")["execution"]["status"] == "queued" else None)
        cancelled = self.api(url, f"/api/executions/{run_id}/cancel", {})["cancellation"]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.api(url, f"/api/executions/{run_id}")["execution"]["status"], "cancelled")

    def test_build_failure_and_prepared_test_are_canonical_states(self):
        _process, url, _runtime = self.start_lab("build-failure")
        run_id = self.submit(url, "build-failure")
        failed = self.wait_for(lambda: self.api(url, f"/api/executions/{run_id}")["execution"] if self.api(url, f"/api/executions/{run_id}")["execution"]["status"] == "failed" else None)
        self.assertEqual(failed["error"]["stage"], "build")
        self.assertIn("behavior-lab stderr", self.api(url, f"/api/executions/{run_id}/logs?verbosity=raw")["log"]["text"])
        _prepared_process, prepared_url, _prepared_runtime = self.start_lab("prepared-test")
        prepared = self.api(prepared_url, "/api/executions/ui-01-prepared")["execution"]
        self.assertEqual(prepared["status"], "prepared")
        self.assertTrue(next(step for step in prepared["steps"] if step["name"] == "staging")["details"]["preview"])

    def test_legacy_cancellation_command_uses_same_scenario(self):
        _process, url, _runtime = self.start_lab(module="tests.ui.cancellation_dev_server")
        self.assertEqual(self.api(url, "/api/workflows/cancellation-running")["name"], "cancellation-running")

    def test_recovery_scenarios_use_startup_reconciliation(self):
        _process, url, _runtime = self.start_lab("recovery")
        recovered = self.api(url, "/api/executions/behavior-lab-recovery-run")["execution"]
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"]["code"], "execution_interrupted")
        _blocked_process, blocked_url, _blocked_runtime = self.start_lab("recovery-blocked")
        blocked = self.api(blocked_url, "/api/executions/behavior-lab-recovery-run")["execution"]
        self.assertEqual(blocked["recovery"]["status"], "blocked")
        workflow = self.api(blocked_url, "/api/workflows/recovery-blocked")
        request = Request(f"{blocked_url}/api/run", data=json.dumps({"workflow": workflow, "dry_run": False}).encode(), method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as rejected:
            urlopen(request, timeout=5)
        self.assertEqual(rejected.exception.code, 503)

    def test_graceful_shutdown_starts_active_and_queued_runs(self):
        process, url, runtime = self.start_lab("graceful-shutdown")
        rows = self.wait_for(lambda: self.api(url, "/api/executions")["executions"] if len(self.api(url, "/api/executions")["executions"]) == 2 else None)
        self.assertEqual({row["status"] for row in rows}, {"running", "queued"})
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 0)
        summary = process.stdout.read()
        self.assertIn('"id": "behavior-lab-shutdown-queued", "reason": "server_shutdown", "status": "cancelled"', summary)
        self.assertIn('"id": "behavior-lab-shutdown-active", "reason": "server_shutdown", "status": "cancelled"', summary)
        self.assertFalse(runtime.exists())


if __name__ == "__main__":
    unittest.main()
