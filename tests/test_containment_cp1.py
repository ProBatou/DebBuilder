"""CP1 containment diagnostics, blocker identities, and read-only absence proof."""
import unittest
import tempfile
from dataclasses import replace
from unittest import mock

import debbuilder.command_containment as containment
from debbuilder.command_containment import AbsenceStatus, UnitSnapshot
from debbuilder.command_identity import IdentityRecorder, recording_identities
from debbuilder.command_runner import run_command


class CP1ContainmentTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(containment, "_CLEANUP_BLOCKERS", {})
        patch.start()
        self.addCleanup(patch.stop)
        generations = mock.patch.object(containment, "_CLEANUP_BLOCKER_GENERATIONS", {})
        generations.start()
        self.addCleanup(generations.stop)
        revision = mock.patch.object(containment, "_CLEANUP_BLOCKER_REVISION", 0)
        revision.start()
        self.addCleanup(revision.stop)
        saturation = mock.patch.object(containment, "_CLEANUP_BLOCKERS_SATURATED", False)
        saturation.start()
        self.addCleanup(saturation.stop)
        self.identity = containment.starting_metadata("cp1-run", "a" * 32)
        self.identity.update({
            "invocation_id": "b" * 32,
            "control_group": containment.expected_control_group(self.identity["unit_name"]),
            "containment_state": "active",
        })
        self.snapshot = UnitSnapshot(
            unit_name=self.identity["unit_name"],
            description=containment.unit_description("cp1-run", "a" * 32),
            transient=True, invocation_id="b" * 32,
            control_group=self.identity["control_group"], active_state="active",
            sub_state="running", service_type="exec", exit_type="cgroup",
            kill_mode="control-group", result="success", main_pid=123,
            exec_main_code=0, exec_main_status=0,
        )

    def connection(self, snapshot):
        class Connection:
            def __init__(self):
                self.stopped = False
                self.closed = False
            def snapshot(self, _unit):
                return snapshot
            def stop(self, _unit):
                self.stopped = True
            def close(self):
                self.closed = True
        return Connection()

    def test_property_mismatches_fail_closed_and_name_the_value(self):
        for field, name, observed, expected in (
            ("service_type", "Type", "simple", "exec"),
            ("exit_type", "ExitType", "main", "cgroup"),
            ("kill_mode", "KillMode", "process", "control-group"),
        ):
            with self.subTest(name=name):
                connection = self.connection(replace(self.snapshot, **{field: observed}))
                command = containment.SystemdCommandContainment(
                    connection, IdentityRecorder("cp1-run", mock.Mock(), mock.Mock()),
                    self.identity, None, None,
                )
                result = command._terminate_record(self.identity, persist_stopping=False)
                self.assertFalse(result.gone)
                self.assertFalse(connection.stopped)
                self.assertIn(f"{name} expected={expected} observed={observed}", result.error)
                containment.latch_runtime_cleanup_blocker(result.error, self.identity)
                self.assertEqual(len(containment.cleanup_blockers()), 1)
                self.assertEqual(containment.cleanup_blockers()[0].invocation_id, "b" * 32)

    def test_property_diagnostic_does_not_expose_arbitrary_value(self):
        result = containment._snapshot_matches_ownership(
            replace(self.snapshot, kill_mode="/private/secret"), self.identity)
        self.assertIn("observed=<invalid>", result.reason)
        self.assertNotIn("/private/secret", result.reason)

    def test_runner_publishes_exact_blocker_for_teardown_mismatch(self):
        for field, observed in (("service_type", "simple"), ("exit_type", "main"),
                                ("kill_mode", "process")):
            with self.subTest(field=field):
                containment._CLEANUP_BLOCKERS.clear()
                recorded = []
                connection = self.connection(replace(self.snapshot, **{field: observed}))
                def stream(*_args, **_kwargs):
                    recorded.append(self.identity)
                    command = containment.SystemdCommandContainment(
                        connection, IdentityRecorder("cp1-run", mock.Mock(), mock.Mock()),
                        self.identity, None, None)
                    terminated = command._terminate_record(self.identity, persist_stopping=False)
                    return {
                        "exit_code": None, "process_exit_code": None, "stdout": "", "stderr": "",
                        "timed_out": True, "timeout_reason": "maximum_runtime", "killed": False,
                        "termination_error": terminated.error, "containment_gone": terminated.gone,
                        "containment_identity": self.identity, "cancellation_requested": False,
                        "cancelled": False, "resource_control": {},
                    }
                with tempfile.TemporaryDirectory() as temporary, recording_identities(
                    "cp1-run", record=lambda value: recorded.append(value),
                    clear=lambda _value: None, update=lambda _old, _new: True,
                ), mock.patch("debbuilder.command_runner.containment_capability",
                              return_value=containment.ContainmentCapability("systemd_cgroup", True, "ok")), \
                    mock.patch("debbuilder.command_runner._stream_systemd_cgroup", side_effect=stream):
                    result = run_command("true", workspace=temporary)
                self.assertEqual(result["error_code"], "command_containment_termination_failed")
                self.assertEqual(len(containment.cleanup_blockers()), 1)
                self.assertEqual(containment.cleanup_blockers()[0].invocation_id, "b" * 32)
                self.assertFalse(connection.stopped)

    def test_absence_outcomes_and_aba(self):
        for snapshot, group_absent, expected in (
            (None, True, AbsenceStatus.PROVEN_GONE),
            (None, False, AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS),
            (self.snapshot, False, AbsenceStatus.STILL_PRESENT_AND_OWNED),
            (replace(self.snapshot, invocation_id="c" * 32), False,
             AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS),
            (replace(self.snapshot, transient=False), False,
             AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS),
        ):
            with self.subTest(expected=expected, snapshot=snapshot):
                connection = self.connection(snapshot)
                with mock.patch.object(containment, "_cgroup_is_absent", return_value=group_absent):
                    proof = containment.prove_containment_absent(
                        self.identity, connection_factory=lambda: connection)
                self.assertEqual(proof.status, expected)
                self.assertFalse(connection.stopped)
                self.assertTrue(connection.closed)

    def test_proof_errors_and_boot_mismatch(self):
        connection = self.connection(None)
        with mock.patch.object(containment, "_cgroup_is_absent", side_effect=PermissionError("denied")):
            self.assertEqual(containment.prove_containment_absent(
                self.identity, connection_factory=lambda: connection).status,
                AbsenceStatus.PROOF_UNAVAILABLE)
        class Broken:
            def snapshot(self, _unit):
                raise containment.ContainmentError("D-Bus failed")
            def close(self):
                pass
        self.assertEqual(containment.prove_containment_absent(
            self.identity, connection_factory=Broken).status, AbsenceStatus.PROOF_UNAVAILABLE)
        previous = {**self.identity, "boot_id": "0" * 8 + "-0000-0000-0000-000000000000"}
        with mock.patch.object(containment, "_cgroup_is_absent", return_value=True):
            self.assertEqual(containment.prove_containment_absent(
                previous, connection_factory=lambda: self.connection(None)).status,
                AbsenceStatus.PROVEN_GONE)
            self.assertEqual(containment.prove_containment_absent(
                previous, connection_factory=lambda: self.connection(self.snapshot)).status,
                AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS)

    def test_multiple_kinds_are_independent_and_capacity_fails_closed(self):
        other = containment.starting_metadata("cp1-other", "c" * 32)
        containment.latch_runtime_cleanup_blocker("first", self.identity)
        containment._publish_cleanup_blocker("probe", "second", other)
        self.assertEqual([b.kind for b in containment.cleanup_blockers()], ["probe", "runtime"])
        self.assertIn("2 containment cleanup object", containment.containment_cleanup_blocker())
        del containment._CLEANUP_BLOCKERS[(
            "runtime", self.identity["boot_id"], self.identity["unit_name"], "b" * 32)]
        self.assertIn("1 containment cleanup object", containment.containment_cleanup_blocker())
        self.assertEqual(containment.cleanup_blockers()[0].reason, "second")
        with mock.patch.object(containment, "MAX_CLEANUP_BLOCKERS", 1):
            containment.latch_runtime_cleanup_blocker("overflow", self.identity)
        self.assertEqual(len(containment.cleanup_blockers()), 1)
        self.assertIn("capacity exceeded", containment.containment_cleanup_blocker())

    def test_reused_unit_name_retains_both_uncertain_incarnations(self):
        containment.latch_runtime_cleanup_blocker("old", self.identity)
        newer = {**self.identity, "invocation_id": "c" * 32}
        containment.latch_runtime_cleanup_blocker("new", newer)
        self.assertEqual({b.invocation_id for b in containment.cleanup_blockers()},
                         {"b" * 32, "c" * 32})
        self.assertIn("2 containment cleanup object", containment.containment_cleanup_blocker())


if __name__ == "__main__":
    unittest.main()
