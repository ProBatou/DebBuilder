import tempfile
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import debbuilder.command_containment as containment
from debbuilder.command_containment import (
    ContainmentError,
    UnitSnapshot,
    _SystemdConnection,
    command_unit_components,
    expected_control_group,
    is_command_namespace_member,
    recover_orphan_systemd_containment,
)
from debbuilder.command_identity import VerificationStatus


UNIT = "debbuilder-command-0123456789abcdef-" + "a" * 32 + ".service"
GROUP = expected_control_group(UNIT)


def snapshot(**changes):
    value = UnitSnapshot(
        unit_name=UNIT,
        description="DebBuilder command 0123456789abcdef/" + "a" * 32,
        transient=True,
        invocation_id="b" * 32,
        control_group=GROUP,
        active_state="active",
        sub_state="running",
        service_type="exec",
        exit_type="cgroup",
        kill_mode="control-group",
        result="success",
        main_pid=123,
        exec_main_code=0,
        exec_main_status=0,
    )
    return replace(value, **changes)


class Connection:
    def __init__(self, snapshots, *, stop_error=None, reset_error=None):
        self.snapshots = list(snapshots)
        self.last = self.snapshots[-1] if self.snapshots else None
        self.stop_error = stop_error
        self.reset_error = reset_error
        self.stop_calls = []
        self.reset_calls = []
        self.closed = False

    def snapshot(self, _unit):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return self.last

    def stop(self, unit):
        self.stop_calls.append(unit)
        if self.stop_error:
            raise self.stop_error

    def reset_failed(self, unit):
        self.reset_calls.append(unit)
        if self.reset_error:
            raise self.reset_error

    def close(self):
        self.closed = True


class OrphanContainmentTests(unittest.TestCase):
    def test_unit_name_components_are_strict(self):
        self.assertEqual(command_unit_components(UNIT), ("0123456789abcdef", "a" * 32))
        for invalid in ("debbuilder-command.service", UNIT + ".extra", UNIT.upper()):
            with self.subTest(invalid=invalid), self.assertRaises(ContainmentError):
                command_unit_components(invalid)

    def test_malformed_reserved_namespace_member_remains_visible_but_unauthorized(self):
        malformed = "debbuilder-command-not-a-canonical-identity.service"
        self.assertTrue(is_command_namespace_member(malformed))
        connection_factory = mock.Mock()
        recovered = recover_orphan_systemd_containment(
            malformed, connection_factory=connection_factory,
        )
        self.assertEqual(recovered.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertFalse(recovered.signalled)
        connection_factory.assert_not_called()

    def test_snapshot_uses_systemd_unit_id_instead_of_echoing_requested_alias(self):
        actual = "debbuilder-command-ffffffffffffffff-" + "e" * 32 + ".service"
        connection = object.__new__(_SystemdConnection)
        connection.manager = mock.Mock()
        connection.manager.GetUnit.return_value = "/unit/path"
        connection.bus = mock.Mock()
        properties = mock.Mock()
        properties.GetAll.side_effect = [
            {
                "Id": actual,
                "Description": "DebBuilder command 0123456789abcdef/" + "a" * 32,
                "Transient": True,
                "InvocationID": bytes.fromhex("b" * 32),
                "ActiveState": "active",
                "SubState": "running",
            },
            {
                "ControlGroup": GROUP,
                "Type": "exec",
                "ExitType": "cgroup",
                "KillMode": "control-group",
                "Result": "success",
                "MainPID": 123,
                "ExecMainCode": 0,
                "ExecMainStatus": 0,
            },
        ]
        with mock.patch("dbus.Interface", return_value=properties):
            observed = connection.snapshot(UNIT)
        self.assertEqual(observed.unit_name, actual)
        recovered = recover_orphan_systemd_containment(
            UNIT, connection_factory=lambda: Connection([observed]),
        )
        self.assertFalse(recovered.signalled)

    def test_valid_unbound_transient_provenance_is_stopped_and_proved_absent(self):
        connection = Connection([snapshot(), snapshot(), None])
        recovered = recover_orphan_systemd_containment(UNIT, connection_factory=lambda: connection)
        self.assertEqual(recovered.verification.status, VerificationStatus.NOT_RUNNING)
        self.assertTrue(recovered.signalled)
        self.assertTrue(recovered.gone)
        self.assertEqual(connection.stop_calls, [UNIT])

    def test_live_owner_in_another_process_blocks_provenance_only_stop(self):
        connection = Connection([snapshot(), snapshot(), None])
        with tempfile.TemporaryDirectory() as temporary:
            lease_path = Path(temporary) / "namespace.lock"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, os, sys; "
                        "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
                        "fcntl.flock(fd, fcntl.LOCK_SH); print('ready', flush=True); "
                        "sys.stdin.read()"
                    ),
                    str(lease_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with mock.patch.object(containment, "NAMESPACE_LEASE_PATH", lease_path):
                    blocked = recover_orphan_systemd_containment(
                        UNIT, connection_factory=lambda: connection,
                    )
                self.assertEqual(blocked.verification.status, VerificationStatus.UNVERIFIABLE)
                self.assertIn("lease is busy", blocked.verification.reason)
                self.assertFalse(blocked.signalled)
                self.assertEqual(connection.stop_calls, [])
            finally:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()

            with mock.patch.object(containment, "NAMESPACE_LEASE_PATH", lease_path):
                recovered = recover_orphan_systemd_containment(
                    UNIT, connection_factory=lambda: connection,
                )
            self.assertTrue(recovered.gone)
            self.assertEqual(connection.stop_calls, [UNIT])

    def test_untrusted_namespace_lease_file_never_authorizes_stop(self):
        connection = Connection([snapshot(), snapshot(), None])
        with tempfile.TemporaryDirectory() as temporary:
            lease_path = Path(temporary) / "namespace.lock"
            lease_path.write_text("", encoding="ascii")
            lease_path.chmod(0o644)
            with mock.patch.object(containment, "NAMESPACE_LEASE_PATH", lease_path):
                recovered = recover_orphan_systemd_containment(
                    UNIT, connection_factory=lambda: connection,
                )
        self.assertEqual(recovered.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertIn("unsafe ownership or permissions", recovered.verification.reason)
        self.assertFalse(recovered.signalled)
        self.assertEqual(connection.stop_calls, [])

    def test_name_only_or_invalid_provenance_never_authorizes_stop(self):
        variants = (
            snapshot(description="different"),
            snapshot(transient=False),
            snapshot(service_type="simple"),
            snapshot(exit_type="main"),
            snapshot(kill_mode="process"),
            snapshot(control_group="/system.slice/different.service"),
            snapshot(control_group="", active_state="active", sub_state="running"),
            snapshot(invocation_id=""),
            snapshot(unit_name="debbuilder-command-ffffffffffffffff-" + "e" * 32 + ".service"),
        )
        for value in variants:
            with self.subTest(value=value):
                connection = Connection([value])
                recovered = recover_orphan_systemd_containment(UNIT, connection_factory=lambda: connection)
                self.assertFalse(recovered.gone)
                self.assertEqual(connection.stop_calls, [])

    def test_cgroup_without_observable_unit_remains_unresolved(self):
        connection = Connection([None])
        with mock.patch.object(containment, "_cgroup_is_absent", return_value=False):
            recovered = recover_orphan_systemd_containment(UNIT, connection_factory=lambda: connection)
        self.assertEqual(recovered.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertFalse(recovered.signalled)

    def test_stop_reset_and_surviving_cgroup_fail_closed(self):
        stop = Connection([snapshot(), snapshot()], stop_error=RuntimeError("stop denied"))
        stopped = recover_orphan_systemd_containment(UNIT, connection_factory=lambda: stop)
        self.assertFalse(stopped.gone)
        self.assertIn("StopUnit failed", stopped.verification.reason)

        failed = snapshot(
            control_group="", active_state="failed", sub_state="failed",
            result="exit-code", exec_main_code=1, exec_main_status=1,
        )
        reset = Connection([snapshot(), snapshot(), failed], reset_error=RuntimeError("reset denied"))
        reset_result = recover_orphan_systemd_containment(UNIT, connection_factory=lambda: reset)
        self.assertFalse(reset_result.gone)
        self.assertIn("could not be reset", reset_result.verification.reason)

        surviving = Connection([snapshot(), snapshot()])
        with (
            mock.patch.object(containment, "OPERATION_TIMEOUT", 0.01),
            mock.patch.object(containment, "POLL_INTERVAL", 0.001),
            mock.patch.object(containment, "_cgroup_is_absent", return_value=False),
        ):
            surviving_result = recover_orphan_systemd_containment(
                UNIT, connection_factory=lambda: surviving,
            )
        self.assertFalse(surviving_result.gone)
        self.assertTrue(surviving_result.signalled)

    def test_same_name_replacement_is_never_reset_after_stop(self):
        replacement = snapshot(
            invocation_id="c" * 32,
            control_group="",
            active_state="failed",
            sub_state="failed",
        )
        connection = Connection([snapshot(), snapshot(), replacement])
        recovered = recover_orphan_systemd_containment(
            UNIT, connection_factory=lambda: connection,
        )
        self.assertFalse(recovered.gone)
        self.assertTrue(recovered.signalled)
        self.assertEqual(connection.reset_calls, [])
        self.assertIn("invocation changed", recovered.verification.reason)

    def test_dbus_inaccessible_never_falls_back_to_process_signalling(self):
        with mock.patch("debbuilder.command_runner._signal_process_group") as signal_group:
            recovered = recover_orphan_systemd_containment(
                UNIT,
                connection_factory=lambda: (_ for _ in ()).throw(ContainmentError("D-Bus unavailable")),
            )
        self.assertFalse(recovered.gone)
        self.assertFalse(recovered.signalled)
        signal_group.assert_not_called()

    def test_loaded_inventory_failure_is_not_hidden_by_a_cgroup_entry(self):
        with mock.patch.object(containment, "_command_cgroup_units", return_value={UNIT}):
            with self.assertRaisesRegex(ContainmentError, "inventory is unavailable"):
                containment.loaded_command_units(
                    connection_factory=lambda: (_ for _ in ()).throw(RuntimeError("D-Bus denied")),
                )


if __name__ == "__main__":
    unittest.main()
