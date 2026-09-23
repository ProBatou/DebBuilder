from tests.lifecycle_helpers import cleanup_blockers
"""CP3 startup/runtime convergence and final adversarial regressions."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import debbuilder.command_containment as containment
from debbuilder.build_store import BuildStore
from debbuilder.command_identity import (
    IDENTITY_SCHEMA_VERSION,
    VerificationResult,
    VerificationStatus,
    current_boot_id,
    persist_identity,
)
from debbuilder.execution_recovery import recover_startup
from debbuilder.resource_limits import empty_policy


def recipe(name):
    return {
        "schema_version": 5,
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "CP3 <cp3@example.test>",
            "description": "CP3 containment test",
        },
        "source": {"repository": f"owner/{name}"},
    }


def systemd_identity(run_id, command, invocation="1", *, boot_id=None):
    command_id = command * 32
    unit_name = containment.command_unit_name(run_id, command_id)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "backend": "systemd_cgroup",
        "boot_id": boot_id or current_boot_id(),
        "run_id": run_id,
        "command_id": command_id,
        "unit_name": unit_name,
        "containment_state": "active",
        "invocation_id": invocation * 32,
        "control_group": containment.expected_control_group(unit_name),
        "resource_limits": empty_policy(),
        "resource_io_targets": [],
    }


def recovery(status, reason, *, gone=False):
    return containment.ContainmentRecovery(
        VerificationResult(status, reason), gone=gone,
    )


class CanonicalRecoveryProofTests(unittest.TestCase):
    def test_previous_boot_requires_canonical_absence_proof(self):
        value = systemd_identity(
            "previous-boot", "a", boot_id="00000000-0000-0000-0000-000000000000",
        )
        factory = mock.Mock()
        cases = (
            (containment.AbsenceStatus.PROVEN_GONE, VerificationStatus.NOT_RUNNING, True),
            (containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS, VerificationStatus.MISMATCH, False),
            (containment.AbsenceStatus.PROOF_UNAVAILABLE, VerificationStatus.UNVERIFIABLE, False),
        )
        for proof_status, verification_status, gone in cases:
            with self.subTest(proof_status=proof_status), mock.patch.object(
                containment,
                "prove_containment_absent",
                return_value=containment.AbsenceProof(proof_status, "canonical decision"),
            ) as prove:
                result = containment.recover_systemd_containment(
                    value,
                    expected_run_id=value["run_id"],
                    recorder=mock.Mock(),
                    connection_factory=factory,
                )
            self.assertEqual(result.verification.status, verification_status)
            self.assertEqual(result.gone, gone)
            prove.assert_called_once_with(value, connection_factory=factory)
        factory.assert_not_called()

    def test_absent_current_boot_unit_uses_the_same_canonical_proof(self):
        value = systemd_identity("current-boot", "b")

        class AbsentConnection:
            def __init__(self):
                self.closed = False

            def snapshot(self, _unit_name):
                return None

            def close(self):
                self.closed = True

        connection = AbsentConnection()
        with mock.patch.object(
            containment,
            "prove_containment_absent",
            return_value=containment.AbsenceProof(
                containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS,
                "unit absent but canonical cgroup remains",
            ),
        ) as prove:
            result = containment.recover_systemd_containment(
                value,
                expected_run_id=value["run_id"],
                recorder=mock.Mock(),
                connection_factory=lambda: connection,
            )

        self.assertEqual(result.verification.status, VerificationStatus.MISMATCH)
        self.assertFalse(result.gone)
        self.assertTrue(connection.closed)
        prove.assert_called_once()

    def test_complete_inventory_fails_closed_while_a_live_owner_holds_the_namespace(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            containment, "NAMESPACE_LEASE_PATH", Path(temporary) / "namespace.lock",
        ):
            shared_fd = containment._acquire_namespace_lease(exclusive=False)
            factory = mock.Mock()
            try:
                with self.assertRaisesRegex(containment.ContainmentError, "lease is busy"):
                    containment.loaded_command_units(connection_factory=factory)
            finally:
                os.close(shared_fd)
        factory.assert_not_called()


class RestartConvergenceTests(unittest.TestCase):
    def make_store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return BuildStore(Path(temporary.name) / "builds")

    @staticmethod
    def create_running(store, name, command):
        run = store.create(recipe(name), mode="build", run_id=name)
        run.update({"status": "running", "started_at": run["created_at"]})
        store.save(run)
        identity = systemd_identity(run["id"], command)
        with store.locked_run(run["id"]) as workspace_fd:
            persist_identity(workspace_fd, identity)
        return run, identity

    def test_multiple_bound_objects_converge_independently_across_restart(self):
        combinations = (
            (True, False),
            (False, True),
            (True, True),
            (False, False),
        )
        for a_gone, b_gone in combinations:
            with self.subTest(a_gone=a_gone, b_gone=b_gone):
                store = self.make_store()
                run_a, identity_a = self.create_running(store, "restart-a", "a")
                run_b, identity_b = self.create_running(store, "restart-b", "b")
                states = {run_a["id"]: a_gone, run_b["id"]: b_gone}

                def recover(value, **_kwargs):
                    if states[value["run_id"]]:
                        return recovery(
                            VerificationStatus.NOT_RUNNING,
                            "unit and canonical cgroup are absent",
                            gone=True,
                        )
                    return recovery(VerificationStatus.MISMATCH, "different InvocationID")

                loaded = {
                    identity["unit_name"]
                    for run, identity in ((run_a, identity_a), (run_b, identity_b))
                    if not states[run["id"]]
                }
                with mock.patch(
                    "debbuilder.execution_recovery.recover_systemd_containment",
                    side_effect=recover,
                ), mock.patch(
                    "debbuilder.execution_recovery.loaded_command_units",
                    return_value=loaded,
                ), mock.patch(
                    "debbuilder.execution_recovery.recover_orphan_systemd_containment",
                ) as orphan:
                    result = recover_startup(store)

                expected_blocked = sorted(
                    run["id"] for run in (run_a, run_b) if not states[run["id"]]
                )
                actual_blocked = (
                    result.admission_blocker["details"]["unresolved_run_ids"]
                    if result.admission_blocker else []
                )
                self.assertEqual(actual_blocked, expected_blocked)
                self.assertEqual(sorted(result.recovered_run_ids), sorted(
                    run["id"] for run in (run_a, run_b) if states[run["id"]]
                ))
                orphan.assert_not_called()

    def test_runtime_failure_history_is_unchanged_after_restart_proves_absence(self):
        store = self.make_store()
        run = store.create(recipe("failed-history"), mode="build", run_id="failed-history")
        run.update({
            "status": "failed",
            "finished_at": run["created_at"],
            "duration": 0.0,
            "error": {
                "code": "command_containment_termination_failed",
                "message": "KillMode expected=control-group observed=process",
            },
        })
        store.save(run)
        identity = systemd_identity(run["id"], "c")
        with store.locked_run(run["id"]) as workspace_fd:
            persist_identity(workspace_fd, identity)
        before = store.load(run["id"])

        with mock.patch(
            "debbuilder.execution_recovery.recover_systemd_containment",
            return_value=recovery(
                VerificationStatus.NOT_RUNNING,
                "unit and canonical cgroup are absent",
                gone=True,
            ),
        ), mock.patch(
            "debbuilder.execution_recovery.loaded_command_units", return_value=set(),
        ):
            result = recover_startup(store)

        self.assertIsNone(result.admission_blocker)
        self.assertEqual(store.load(run["id"]), before)

    def test_complete_namespace_inventory_is_restart_saturation_boundary(self):
        clean_store = self.make_store()
        with mock.patch(
            "debbuilder.execution_recovery.loaded_command_units", return_value=set(),
        ):
            clean = recover_startup(clean_store)
        self.assertIsNone(clean.admission_blocker)

        uncertain_store = self.make_store()
        with mock.patch(
            "debbuilder.execution_recovery.loaded_command_units",
            side_effect=containment.ContainmentError("inventory unavailable"),
        ):
            uncertain = recover_startup(uncertain_store)
        self.assertIsNotNone(uncertain.admission_blocker)

        live_store = self.make_store()
        unit_name = "debbuilder-command-aaaaaaaaaaaaaaaa-" + "d" * 32 + ".service"
        with mock.patch(
            "debbuilder.execution_recovery.loaded_command_units", return_value={unit_name},
        ), mock.patch(
            "debbuilder.execution_recovery.recover_orphan_systemd_containment",
            return_value=containment.OrphanContainmentRecovery(
                VerificationResult(VerificationStatus.MISMATCH, "identity ambiguous"),
            ),
        ):
            live = recover_startup(live_store)
        self.assertIsNotNone(live.admission_blocker)
        self.assertEqual(live.stray_units, [unit_name])


class ConcurrentReconciliationTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("_CLEANUP_BLOCKERS", {}),
            ("_CLEANUP_BLOCKER_GENERATIONS", {}),
            ("_CLEANUP_BLOCKER_REVISION", 0),
            ("_CLEANUP_BLOCKERS_SATURATED", False),
        ):
            patch = mock.patch.object(containment, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_two_reconciliations_cannot_double_remove_one_publication(self):
        value = systemd_identity("concurrent-reconcile", "e")
        containment.latch_runtime_cleanup_blocker("uncertain cleanup", value)
        both_proving = threading.Barrier(2)

        def prove(_blocker):
            both_proving.wait(timeout=2)
            return containment.AbsenceProof(
                containment.AbsenceStatus.PROVEN_GONE, "gone",
            )

        results = []
        with mock.patch.object(containment, "prove_containment_absent", side_effect=prove):
            threads = [
                threading.Thread(target=lambda: results.append(
                    containment.reconcile_cleanup_blockers(),
                ))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result.removed for result in results), 1)
        self.assertEqual(sum(result.stale for result in results), 1)
        self.assertEqual(cleanup_blockers(), ())


if __name__ == "__main__":
    unittest.main()
