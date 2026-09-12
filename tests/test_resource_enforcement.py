import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import debbuilder.command_containment as containment
from debbuilder.command_containment import (
    CGROUP_ROOT,
    ContainmentCapability,
    ContainmentError,
    SystemdCommandContainment,
    resource_limit_capability,
)
from debbuilder.command_identity import IdentityRecorder, recording_identities
from debbuilder.resource_limits import empty_policy
from debbuilder.command_runner import run_command
from debbuilder.execution_cancellation import ExecutionCancelled
from debbuilder.resource_limits import ResourceLimitError


class _Recorder:
    def __init__(self):
        self.value = None

    def record(self, value):
        if self.value is not None:
            raise RuntimeError("identity already exists")
        self.value = value

    def update(self, expected, updated):
        if self.value != expected:
            return False
        self.value = updated
        return True

    def clear(self, expected):
        if self.value != expected:
            return False
        self.value = None
        return True


class ResourceEnforcementFallbackTests(unittest.TestCase):
    def setUp(self):
        runtime_blocker = mock.patch.object(containment, "_RUNTIME_CLEANUP_ERROR", "")
        runtime_blocker.start()
        self.addCleanup(runtime_blocker.stop)

    def run_with_completed(self, completed):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        recorder = _Recorder()
        available = {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": ["memory"], "reason": "verified",
        }
        with recording_identities(
            "resource-precedence", record=recorder.record, update=recorder.update,
            clear=recorder.clear, resource_policy={"memory_max_bytes": 1024},
        ), mock.patch(
            "debbuilder.command_runner.containment_capability",
            return_value=ContainmentCapability("systemd_cgroup", True, "verified"),
        ), mock.patch(
            "debbuilder.command_runner.resource_limit_capability", return_value=available,
        ), mock.patch(
            "debbuilder.command_runner._stream_systemd_cgroup", return_value=completed,
        ):
            return run_command("true", workspace=temporary.name)

    def test_finite_policy_never_uses_process_group_when_capability_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = _Recorder()
            unavailable = {
                "backend": "process_group", "available": False,
                "requested_controls": ["memory"], "reason": "controller unavailable",
            }
            with recording_identities(
                "no-fallback", record=recorder.record, update=recorder.update,
                clear=recorder.clear, resource_policy={"memory_max_bytes": 1024},
            ), mock.patch(
                "debbuilder.command_runner.resource_limit_capability", return_value=unavailable,
            ), mock.patch("debbuilder.command_runner._stream_process_group") as fallback:
                result = run_command("true", workspace=temporary)
        fallback.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "resource_limit_enforcement_failed")
        self.assertEqual(result["resource_control"]["verification"], "failed")
        self.assertIsNone(recorder.value)

    def test_latched_probe_cleanup_blocks_recorded_command_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = _Recorder()
            with recording_identities(
                "probe-blocked", record=recorder.record, update=recorder.update,
                clear=recorder.clear,
            ), mock.patch(
            "debbuilder.command_runner.containment_cleanup_blocker",
                return_value="probe unit remains",
            ), mock.patch("debbuilder.command_runner._stream_process_group") as fallback:
                result = run_command("true", workspace=temporary)
        fallback.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "command_containment_termination_failed")
        self.assertIsNone(recorder.value)

    def test_capability_probe_that_latches_during_call_never_selects_a_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = _Recorder()
            with recording_identities(
                "probe-race", record=recorder.record, update=recorder.update,
                clear=recorder.clear,
            ), mock.patch(
                "debbuilder.command_runner.containment_cleanup_blocker",
                side_effect=["", "probe cleanup became unresolved"],
            ), mock.patch(
                "debbuilder.command_runner.containment_capability",
                return_value=ContainmentCapability("process_group", False, "probe failed"),
            ), mock.patch(
                "debbuilder.command_runner._stream_process_group",
            ) as fallback, mock.patch(
                "debbuilder.command_runner._stream_systemd_cgroup",
            ) as strong:
                result = run_command("true", workspace=temporary)
        fallback.assert_not_called()
        strong.assert_not_called()
        self.assertEqual(result["error_code"], "command_containment_termination_failed")

    def test_execution_time_resolution_error_is_structured_enforcement_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = _Recorder()
            with recording_identities(
                "resolution-failure", record=recorder.record, update=recorder.update,
                clear=recorder.clear,
                resource_policy={"io_write_bandwidth_max_bytes_per_sec": 1024},
            ), mock.patch(
                "debbuilder.command_runner.resource_limit_capability",
                side_effect=ResourceLimitError(
                    "resource_limits_unavailable", "injected target failure",
                    details={"control": "io"},
                ),
            ):
                result = run_command("true", workspace=temporary)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "resource_limit_enforcement_failed")
        self.assertEqual(result["resource_control"]["verification"], "failed")
        self.assertIsNone(recorder.value)

    def test_start_transient_rejection_is_enforcement_failure_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = _Recorder()
            available = {
                "backend": "systemd_cgroup", "available": True,
                "requested_controls": ["memory"], "reason": "verified",
            }
            with recording_identities(
                "start-rejected", record=recorder.record, update=recorder.update,
                clear=recorder.clear, resource_policy={"memory_max_bytes": 1024},
            ), mock.patch(
                "debbuilder.command_runner.containment_capability",
                return_value=ContainmentCapability("systemd_cgroup", True, "verified"),
            ), mock.patch(
                "debbuilder.command_runner.resource_limit_capability", return_value=available,
            ), mock.patch(
                "debbuilder.command_runner._stream_systemd_cgroup",
                side_effect=ContainmentError("StartTransientUnit rejected properties"),
            ), mock.patch("debbuilder.command_runner._stream_process_group") as fallback:
                result = run_command("true", workspace=temporary)
        fallback.assert_not_called()
        self.assertEqual(result["error_code"], "resource_limit_enforcement_failed")

    def test_cancel_timeout_and_cleanup_precedence_over_resource_evidence(self):
        control = {
            "backend": "systemd_cgroup", "enforcement_required": True,
            "verification": "verified", "requested": {"memory_max_bytes": 1024},
            "unit_result": "oom-kill", "observations": {"memory_events": {"oom_kill": 1}},
            "outcome": {"code": "command_resource_limit_exceeded", "control": "memory_max_bytes"},
        }
        common = {
            "exit_code": None, "process_exit_code": -9, "stdout": "", "stderr": "",
            "timed_out": False, "timeout_reason": "", "killed": True,
            "termination_error": "", "cancellation_requested": True, "cancelled": True,
            "cancellation": {"reason": "server_shutdown"}, "resource_control": control,
        }
        with self.assertRaises(ExecutionCancelled) as cancelled:
            self.run_with_completed(common)
        self.assertEqual(cancelled.exception.command_result["status"], "cancelled")
        self.assertEqual(cancelled.exception.command_result["error_code"], "")

        timed_out = self.run_with_completed({
            **common, "cancellation_requested": False, "cancelled": False,
            "cancellation": {}, "timed_out": True, "timeout_reason": "maximum_runtime",
        })
        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(timed_out["error_code"], "")
        self.assertEqual(timed_out["resource_control"]["outcome"]["control"], "memory_max_bytes")

        with self.assertRaises(ExecutionCancelled) as cleanup_failed:
            self.run_with_completed({**common, "termination_error": "cgroup remains", "cancelled": False})
        self.assertEqual(cleanup_failed.exception.command_result["status"], "failed")
        self.assertEqual(
            cleanup_failed.exception.command_result["error_code"],
            "command_containment_termination_failed",
        )

    def test_unproved_runtime_teardown_latches_and_blocks_the_next_command(self):
        unresolved = {
            "exit_code": None, "process_exit_code": -9, "stdout": "", "stderr": "",
            "timed_out": True, "timeout_reason": "maximum_runtime", "killed": True,
            "termination_error": "runtime cgroup remains", "cancellation_requested": False,
            "cancelled": False, "resource_control": {},
        }
        first = self.run_with_completed(unresolved)
        self.assertEqual(first["error_code"], "command_containment_termination_failed")
        self.assertEqual(containment.runtime_cleanup_blocker(), "runtime cgroup remains")

        second = self.run_with_completed({
            **unresolved, "exit_code": 0, "process_exit_code": 0, "timed_out": False,
            "timeout_reason": "", "killed": False, "termination_error": "",
        })
        self.assertEqual(second["error_code"], "command_containment_termination_failed")
        self.assertIn("containment cleanup is unresolved", second["stderr"])

    def test_resolved_teardown_metadata_error_does_not_latch(self):
        resolved = {
            "exit_code": None, "process_exit_code": -9, "stdout": "", "stderr": "",
            "timed_out": True, "timeout_reason": "maximum_runtime", "killed": True,
            "termination_error": "stopping identity update was ambiguous",
            "containment_gone": True, "cancellation_requested": False,
            "cancelled": False, "resource_control": {},
        }
        result = self.run_with_completed(resolved)
        self.assertEqual(result["error_code"], "command_containment_termination_failed")
        self.assertEqual(containment.runtime_cleanup_blocker(), "")

    def test_safety_gate_serializes_runtime_blocker_publication(self):
        attempting = threading.Event()
        published = threading.Event()

        def latch():
            attempting.set()
            containment.latch_runtime_cleanup_blocker("runtime cgroup remains")
            published.set()

        thread = threading.Thread(target=latch)
        with containment.containment_safety_gate():
            thread.start()
            self.assertTrue(attempting.wait(1))
            self.assertFalse(published.wait(0.05))
        self.assertTrue(published.wait(1))
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_post_start_resource_verification_mismatch_is_enforcement_failure(self):
        result = self.run_with_completed({
            "exit_code": 0, "process_exit_code": 0, "stdout": "", "stderr": "",
            "timed_out": False, "timeout_reason": "", "killed": False,
            "termination_error": "", "cancellation_requested": False, "cancelled": False,
            "resource_control": {
                "backend": "systemd_cgroup", "enforcement_required": True,
                "verification": "failed", "requested": {"memory_max_bytes": 1024},
                "unit_result": "success", "observations": {},
                "outcome": {"code": "resource_limit_enforcement_failed", "control": None},
            },
        })
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "resource_limit_enforcement_failed")

    def test_memory_max_counter_alone_is_authenticated_limit_evidence(self):
        command = SystemdCommandContainment(
            mock.Mock(),
            IdentityRecorder("memory-max", mock.Mock(), mock.Mock()),
            {
                "command_id": "a" * 32,
                "resource_limits": {**empty_policy(), "memory_max_bytes": 1024},
                "control_group": "",
            },
            None,
            None,
        )
        command.resource_observations["memory_events"] = {"max": 1}
        with mock.patch.object(command, "_sample_resources"):
            control = command.resource_control()
        self.assertEqual(control["outcome"], {
            "code": "command_resource_limit_exceeded", "control": "memory_max_bytes",
        })

class RealResourceEnforcementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capability = resource_limit_capability(
            {
                "memory_max_bytes": 64 * 1024 * 1024,
                "tasks_max": 16,
                "cpu_quota_percent": 50,
                "io_read_bandwidth_max_bytes_per_sec": 8 * 1024 * 1024,
                "io_write_bandwidth_max_bytes_per_sec": 8 * 1024 * 1024,
            },
            workspace=Path(__file__).parent,
            refresh=True,
        )

    def setUp(self):
        if not self.capability["available"]:
            self.skipTest(self.capability["reason"])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def execute(self, command, policy, **kwargs):
        recorder = _Recorder()
        clear_observations = []
        run_id = f"resource-test-{time.time_ns()}"

        def clear_after_disappearance(expected):
            if expected.get("backend") == "systemd_cgroup" and expected.get("control_group"):
                cgroup = CGROUP_ROOT / expected["control_group"].lstrip("/")
                clear_observations.append(not cgroup.exists())
            return recorder.clear(expected)

        with recording_identities(
            run_id,
            record=recorder.record,
            update=recorder.update,
            clear=clear_after_disappearance,
            resource_policy=policy,
        ):
            result = run_command(
                command,
                workspace=self.workspace,
                inactivity_timeout=10,
                maximum_runtime=15,
                **kwargs,
            )
        self.assertIsNone(recorder.value)
        self.assertTrue(clear_observations)
        self.assertTrue(all(clear_observations))
        return result

    def test_memory_oom_is_classified_and_cleaned(self):
        result = self.execute(
            "python3 -c 'x=bytearray(256*1024*1024)'",
            {"memory_max_bytes": 32 * 1024 * 1024},
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "command_resource_limit_exceeded")
        self.assertEqual(result["resource_control"]["outcome"]["control"], "memory_max_bytes")
        self.assertEqual(result["termination_error"], "")

    def test_terminal_oom_evidence_is_preserved_when_oom_policy_stops_the_command(self):
        result = self.execute(
            "python3 -c 'x=[]\ntry:\n while True: x.append(bytearray(1024*1024))\nexcept MemoryError: pass'",
            {"memory_max_bytes": 64 * 1024 * 1024},
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "command_resource_limit_exceeded")
        control = result["resource_control"]
        self.assertEqual(control["outcome"]["control"], "memory_max_bytes")
        self.assertTrue(
            control["observations"]["memory_events"].get("max", 0) > 0
            or control["unit_result"] == "oom-kill"
        )

    def test_tasks_denial_is_classified_even_when_command_exits_zero(self):
        result = self.execute(
            "python3 -c 'import os;\ntry: os.fork()\nexcept OSError: pass'",
            {"tasks_max": 1},
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "command_resource_limit_exceeded")
        self.assertGreater(result["resource_control"]["observations"]["tasks_denied"], 0)

    def test_cpu_throttling_is_bounded_observation_not_failure(self):
        result = self.execute(
            "python3 -c 'import time; end=time.monotonic()+0.7\nwhile time.monotonic()<end: pass'",
            {"cpu_quota_percent": 10},
        )
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["resource_control"]["outcome"])
        self.assertGreater(result["resource_control"]["observations"]["cpu_throttled_periods"], 0)

    def test_io_bandwidth_properties_and_observation_are_informational(self):
        result = self.execute(
            "python3 -c 'from pathlib import Path; Path(\"io.bin\").write_bytes(b\"x\"*(2*1024*1024))'",
            {"io_write_bandwidth_max_bytes_per_sec": 8 * 1024 * 1024},
        )
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["resource_control"]["outcome"])
        self.assertEqual(result["resource_control"]["requested"]["io_write_bandwidth_max_bytes_per_sec"], 8 * 1024 * 1024)

    def test_cancellation_keeps_terminal_precedence_and_observations(self):
        cancellation = threading.Event()
        timer = threading.Timer(0.3, cancellation.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(ExecutionCancelled) as raised:
            self.execute(
                "python3 -c 'import time; time.sleep(30)'",
                {"memory_max_bytes": 128 * 1024 * 1024},
                cancellation_event=cancellation,
                on_cancel=lambda: {"reason": "user_requested"},
            )
        result = raised.exception.command_result
        self.assertTrue(result["cancellation_requested"])
        self.assertIn("resource_control", result)
        self.assertEqual(result.get("error_code", ""), "")


if __name__ == "__main__":
    unittest.main()
