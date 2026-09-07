import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from debbuilder import app, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.command_containment import (
    ContainmentError,
    ContainmentRecovery,
    UnitSnapshot,
    _SystemdConnection,
    command_unit_name,
    containment_capability,
    expected_control_group,
    recover_systemd_containment,
    unit_description,
)
from debbuilder.command_identity import (
    ACTIVE_COMMAND_FILE,
    IDENTITY_SCHEMA_VERSION,
    GroupTerminationResult,
    VerificationResult,
    VerificationStatus,
    current_boot_id,
    persist_identity,
)
from debbuilder.execution_manager import ExecutionManager, ExecutionManagerError
from debbuilder.execution_recovery import BLOCKER_CODE, RECOVERY_ERROR_CODE, recover_startup
from debbuilder.execution_recovery import StartupRecoveryResult
from tests.admin_api_case import AdminApiCase


def recipe(name="recovery"):
    return {
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Recovery <recovery@example.test>",
            "description": "Recovery test",
        },
        "source": {"repository": f"owner/{name}"},
    }


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.unit_scan = mock.patch(
            "debbuilder.execution_recovery.loaded_command_units", return_value=set(),
        )
        self.unit_scan.start()
        self.addCleanup(self.unit_scan.stop)

    def create(self, name, status="pending"):
        run = self.store.create(recipe(name), mode="build", run_id=name)
        run["status"] = status
        if status in {"running", "cancelling"}:
            run["started_at"] = run["created_at"]
        self.store.save(run)
        return run

    def save_identity(self, run_id, identity):
        with self.store.locked_run(run_id) as fd:
            persist_identity(fd, identity)

    def systemd_identity(self, run_id, state="starting", *, boot_id=None):
        command_id = (run_id.encode().hex() + "0" * 32)[:32]
        unit = command_unit_name(run_id, command_id)
        value = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "backend": "systemd_cgroup",
            "boot_id": boot_id or current_boot_id(),
            "run_id": run_id,
            "command_id": command_id,
            "unit_name": unit,
            "containment_state": state,
        }
        if state != "starting":
            value.update({"invocation_id": "1" * 32, "control_group": expected_control_group(unit)})
        return value

    def process_identity(self, run_id, *, boot_id=None):
        return {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "backend": "process_group",
            "containment_state": "active",
            "pid": 424242,
            "pgid": 424242,
            "start_time_ticks": 123,
            "boot_id": boot_id or current_boot_id(),
            "run_id": run_id,
            "command_id": "fallback-command",
        }

    def test_pending_and_queued_without_execution_evidence_are_safely_terminalized(self):
        runs = [self.create("safe-pending", "pending"), self.create("safe-queued", "queued")]

        result = recover_startup(self.store)

        self.assertIsNone(result.admission_blocker)
        self.assertEqual(result.recovered_run_ids, [row["id"] for row in runs])
        for original in runs:
            run = self.store.load(original["id"])
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["error"]["code"], RECOVERY_ERROR_CODE)
            self.assertTrue(all(step["status"] == "pending" for step in run["steps"]))

    def test_no_metadata_decision_table_blocks_execution_evidence_and_active_states(self):
        pending = self.create("pending-with-evidence", "pending")
        pending["events"].append({"at": pending["created_at"], "level": "info", "message": "began"})
        self.store.save(pending)
        running = self.create("running-without-identity", "running")
        cancelling = self.create("cancelling-without-identity", "cancelling")

        result = recover_startup(self.store)

        self.assertEqual(
            result.admission_blocker["details"]["unresolved_run_ids"],
            sorted([pending["id"], running["id"], cancelling["id"]]),
        )
        for run_id in (pending["id"], running["id"], cancelling["id"]):
            run = self.store.load(run_id)
            self.assertNotEqual(run["status"], "failed")
            self.assertEqual(run["recovery"]["status"], "blocked")
            with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                workspace_cleanup.clean_workspace(self.store, run_id)

    def test_running_step_is_failed_future_steps_and_cancellation_are_preserved(self):
        run = self.create("old-boot-cancelling", "cancelling")
        run["steps"][0].update({"status": "success", "finished_at": run["created_at"], "duration": 0.1})
        run["steps"][1].update({"status": "running", "started_at": run["created_at"]})
        run["cancellation"] = {
            "code": "execution_cancelled", "reason": "user_requested",
            "phase": "execution", "stage": "detection", "requested_at": run["created_at"],
        }
        self.store.save(run)
        self.save_identity(run["id"], self.process_identity(
            run["id"], boot_id="00000000-0000-0000-0000-000000000000",
        ))

        result = recover_startup(self.store)
        recovered = self.store.load(run["id"])

        self.assertIsNone(result.admission_blocker)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["steps"][0]["status"], "success")
        self.assertEqual(recovered["steps"][1]["status"], "failed")
        self.assertEqual(recovered["steps"][1]["error"]["code"], RECOVERY_ERROR_CODE)
        self.assertTrue(all(step["status"] == "pending" for step in recovered["steps"][2:]))
        self.assertEqual(recovered["cancellation"], run["cancellation"])

    def test_multiple_running_steps_block_ambiguous_lifecycle_mutation(self):
        run = self.create("multiple-running-steps", "running")
        for step in run["steps"][:2]:
            step.update({"status": "running", "started_at": run["created_at"]})
        self.store.save(run)
        self.save_identity(run["id"], self.process_identity(
            run["id"], boot_id="00000000-0000-0000-0000-000000000000",
        ))

        result = recover_startup(self.store)

        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [run["id"]])
        recovered = self.store.load(run["id"])
        self.assertEqual(recovered["status"], "running")
        self.assertEqual([step["status"] for step in recovered["steps"][:2]], ["running", "running"])
        self.assertIn("multiple running steps", recovered["recovery"]["reason"])

    def test_old_boot_systemd_and_fallback_are_absent_without_signalling(self):
        strong = self.create("old-strong", "running")
        weak = self.create("old-fallback", "running")
        old_boot = "00000000-0000-0000-0000-000000000000"
        self.save_identity(strong["id"], self.systemd_identity(strong["id"], "active", boot_id=old_boot))
        self.save_identity(weak["id"], self.process_identity(weak["id"], boot_id=old_boot))

        with mock.patch("debbuilder.execution_recovery.recover_systemd_containment") as systemd, \
                mock.patch("debbuilder.execution_recovery.terminate_verified_process_group") as process:
            result = recover_startup(self.store)

        self.assertIsNone(result.admission_blocker)
        self.assertEqual(sorted(result.recovered_run_ids), sorted([strong["id"], weak["id"]]))
        systemd.assert_not_called()
        process.assert_not_called()

    def test_each_systemd_state_uses_strong_recovery_and_terminalizes_only_absence(self):
        runs = [self.create(f"strong-{state}", "running") for state in ("starting", "active", "stopping")]
        identities = [self.systemd_identity(run["id"], state) for run, state in zip(runs, ("starting", "active", "stopping"))]
        for run, identity in zip(runs, identities):
            self.save_identity(run["id"], identity)
        absent = ContainmentRecovery(
            VerificationResult(VerificationStatus.NOT_RUNNING, "authoritatively absent"), gone=True,
        )

        with mock.patch("debbuilder.execution_recovery.recover_systemd_containment", return_value=absent) as recover:
            result = recover_startup(self.store)

        self.assertIsNone(result.admission_blocker)
        self.assertEqual(recover.call_count, 3)
        self.assertEqual(sorted(result.recovered_run_ids), sorted(run["id"] for run in runs))

    def test_systemd_mismatch_and_unavailable_are_blockers_and_never_fallback(self):
        mismatch = self.create("strong-mismatch", "running")
        unavailable = self.create("strong-unavailable", "running")
        self.save_identity(mismatch["id"], self.systemd_identity(mismatch["id"], "active"))
        self.save_identity(unavailable["id"], self.systemd_identity(unavailable["id"], "starting"))
        outcomes = [
            ContainmentRecovery(VerificationResult(VerificationStatus.MISMATCH, "invocation mismatch")),
            ContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, "systemd unavailable")),
        ]

        with mock.patch("debbuilder.execution_recovery.recover_systemd_containment", side_effect=outcomes), \
                mock.patch("debbuilder.execution_recovery.terminate_verified_process_group") as fallback:
            result = recover_startup(self.store)

        self.assertEqual(len(result.blockers), 2)
        fallback.assert_not_called()
        self.assertTrue(all(self.store.load(run["id"])["status"] == "running" for run in (mismatch, unavailable)))

    def test_current_boot_fallback_match_is_terminated_but_remains_blocked(self):
        run = self.create("fallback-current", "running")
        identity = self.process_identity(run["id"])
        self.save_identity(run["id"], identity)
        verified = VerificationResult(VerificationStatus.MATCH, "matches")
        terminated = GroupTerminationResult(verified, signalled=True, group_gone=True)

        with mock.patch("debbuilder.execution_recovery.verify_identity", return_value=verified), \
                mock.patch("debbuilder.execution_recovery.terminate_verified_process_group", return_value=terminated) as terminate:
            result = recover_startup(self.store)

        terminate.assert_called_once_with(identity, expected_run_id=run["id"])
        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [run["id"]])
        self.assertEqual(self.store.load(run["id"])["status"], "running")

    def test_corrupt_identity_and_malformed_run_block_without_destructive_calls(self):
        corrupt = self.create("corrupt-identity", "running")
        (self.store.run_dir(corrupt["id"]) / ACTIVE_COMMAND_FILE).write_text("{not-json")
        malformed = self.create("malformed-run", "running")
        (self.store.run_dir(malformed["id"]) / "run.json").write_text("{not-json")

        with mock.patch("debbuilder.execution_recovery.terminate_verified_process_group") as terminate, \
                mock.patch("debbuilder.execution_recovery.recover_systemd_containment") as systemd:
            result = recover_startup(self.store)

        self.assertEqual(
            result.admission_blocker["details"]["unresolved_run_ids"],
            [corrupt["id"], malformed["id"]],
        )
        terminate.assert_not_called()
        systemd.assert_not_called()

    def test_unsafe_run_file_and_unavailable_inventories_fail_closed(self):
        unsafe = self.create("unsafe-run-file", "running")
        path = self.store.run_dir(unsafe["id"]) / "run.json"
        path.unlink()
        path.mkdir()
        result = recover_startup(self.store)
        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [unsafe["id"]])

        with mock.patch("debbuilder.execution_recovery._persisted_run_ids", side_effect=PermissionError("denied")):
            result = recover_startup(self.store)
        self.assertIsNotNone(result.admission_blocker)
        self.assertIn("Run inventory", result.blockers[0]["reason"])

        with mock.patch("debbuilder.execution_recovery.loaded_command_units", side_effect=PermissionError("denied")):
            result = recover_startup(BuildStore(Path(self.temporary.name) / "empty-builds"))
        self.assertIsNotNone(result.admission_blocker)
        self.assertIn("cgroup inventory", result.blockers[0]["reason"])

    def test_partial_symlink_and_non_directory_run_inventory_entries_block(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / "partial-run").mkdir()
        (self.store.root / "unexpected-file").write_text("not a Run")
        (self.store.root / "linked-run").symlink_to(self.store.root / "partial-run")

        result = recover_startup(self.store)

        self.assertIsNotNone(result.admission_blocker)
        reasons = "\n".join(row["reason"] for row in result.blockers)
        self.assertIn("run.json is absent", reasons)
        self.assertIn("unexpected non-directory entry", reasons)
        self.assertIn("linked-run", reasons)

    def test_any_command_history_entry_blocks_no_metadata_shortcut(self):
        run = self.create("queued-history-symlink", "queued")
        command_entry = self.store.run_dir(run["id"]) / "logs" / "commands" / "001.json"
        command_entry.symlink_to("missing-target")

        result = recover_startup(self.store)

        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [run["id"]])
        self.assertEqual(self.store.load(run["id"])["status"], "queued")

    def test_terminal_unresolved_identity_is_reconciled_or_blocks_without_lifecycle_rewrite(self):
        resolved = self.create("terminal-strong-resolved", "failed")
        unresolved = self.create("terminal-strong-mismatch", "failed")
        resolved_identity = self.systemd_identity(resolved["id"], "active")
        unresolved_identity = self.systemd_identity(unresolved["id"], "active")
        self.save_identity(resolved["id"], resolved_identity)
        self.save_identity(unresolved["id"], unresolved_identity)
        before = dict(resolved)
        def recover(identity, **_kwargs):
            if identity["run_id"] == resolved["id"]:
                return ContainmentRecovery(
                    VerificationResult(VerificationStatus.NOT_RUNNING, "authoritatively absent"), gone=True,
                )
            return ContainmentRecovery(VerificationResult(VerificationStatus.MISMATCH, "new invocation"))

        with mock.patch(
            "debbuilder.execution_recovery.recover_systemd_containment", side_effect=recover,
        ):
            result = recover_startup(self.store)

        self.assertEqual(self.store.load(resolved["id"]), before)
        self.assertFalse((self.store.run_dir(resolved["id"]) / ACTIVE_COMMAND_FILE).exists())
        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [unresolved["id"]])
        self.assertEqual(self.store.load(unresolved["id"])["status"], "failed")
        self.assertEqual(self.store.load(unresolved["id"])["recovery"]["status"], "blocked")

    def test_terminal_corrupt_identity_blocks_and_prevents_workspace_cleanup(self):
        run = self.create("terminal-corrupt-identity", "failed")
        metadata = self.store.run_dir(run["id"]) / ACTIVE_COMMAND_FILE
        metadata.write_text("{not-json")

        result = recover_startup(self.store)

        self.assertEqual(result.admission_blocker["details"]["unresolved_run_ids"], [run["id"]])
        with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
            workspace_cleanup.clean_workspace(self.store, run["id"])

    def test_removing_terminal_identity_does_not_erase_a_durable_blocker(self):
        run = self.create("terminal-missing-blocked-identity", "failed")
        identity = self.systemd_identity(run["id"], "active")
        self.save_identity(run["id"], identity)
        mismatch = ContainmentRecovery(VerificationResult(VerificationStatus.MISMATCH, "new invocation"))
        with mock.patch("debbuilder.execution_recovery.recover_systemd_containment", return_value=mismatch):
            first = recover_startup(self.store)
        self.assertIsNotNone(first.admission_blocker)
        (self.store.run_dir(run["id"]) / ACTIVE_COMMAND_FILE).unlink()

        second = recover_startup(self.store)

        self.assertEqual(second.admission_blocker["details"]["unresolved_run_ids"], [run["id"]])
        self.assertEqual(self.store.load(run["id"])["recovery"]["status"], "blocked")

    def test_blocker_and_successful_terminalization_are_idempotent(self):
        blocked = self.create("repeat-blocked", "running")
        safe = self.create("repeat-safe", "queued")

        first = recover_startup(self.store)
        first_blocked = self.store.load(blocked["id"])
        first_safe = self.store.load(safe["id"])
        second = recover_startup(self.store)

        self.assertEqual(self.store.load(blocked["id"]), first_blocked)
        self.assertEqual(self.store.load(safe["id"]), first_safe)
        self.assertEqual(len(first_safe["events"]), 1)
        self.assertEqual(first.admission_blocker, second.admission_blocker)

    def test_terminalization_before_identity_cleanup_converges_without_rewriting_run(self):
        run = self.create("terminal-before-clear", "running")
        identity = self.systemd_identity(run["id"], "stopping", boot_id="00000000-0000-0000-0000-000000000000")
        self.save_identity(run["id"], identity)
        with mock.patch("debbuilder.execution_recovery.clear_identity", return_value=False):
            first = recover_startup(self.store)
        terminal = self.store.load(run["id"])
        self.assertIn(run["id"], first.recovered_run_ids)
        self.assertTrue((self.store.run_dir(run["id"]) / ACTIVE_COMMAND_FILE).exists())

        second = recover_startup(self.store)

        self.assertIsNone(second.admission_blocker)
        self.assertEqual(self.store.load(run["id"]), terminal)
        self.assertFalse((self.store.run_dir(run["id"]) / ACTIVE_COMMAND_FILE).exists())

    def test_stray_unit_blocks_without_stop_and_bound_unresolved_unit_is_not_duplicated(self):
        run = self.create("bound-unit", "running")
        identity = self.systemd_identity(run["id"], "active")
        self.save_identity(run["id"], identity)
        stray = "debbuilder-command-aaaaaaaaaaaaaaaa-" + "b" * 32 + ".service"
        mismatch = ContainmentRecovery(VerificationResult(VerificationStatus.MISMATCH, "different invocation"))

        with mock.patch("debbuilder.execution_recovery.recover_systemd_containment", return_value=mismatch), \
                mock.patch("debbuilder.execution_recovery.loaded_command_units", return_value={identity["unit_name"], stray}):
            result = recover_startup(self.store)

        self.assertEqual(result.stray_units, [stray])
        self.assertEqual(len([row for row in result.blockers if row.get("run_id") == run["id"]]), 1)
        self.assertEqual(result.admission_blocker["details"]["stray_unit_count"], 1)

    def test_manager_blocker_closes_reserve_and_submit_without_starting_work(self):
        executed = []
        manager = ExecutionManager(self.store, execute=lambda run_id, **_kwargs: executed.append(run_id))
        blocker = StartupRecoveryBlocker.sample()
        manager.start(admission_blocker=blocker)
        self.addCleanup(manager.stop, 2)
        run = self.create("not-admitted")

        for operation in (lambda: manager.submit(run["id"]), lambda: manager.reserve().__enter__()):
            with self.assertRaises(ExecutionManagerError) as caught:
                operation()
            self.assertEqual(caught.exception.code, BLOCKER_CODE)
        self.assertEqual(executed, [])
        self.assertFalse(manager.accepting)

    def test_app_start_keeps_admission_closed_until_recovery_completes(self):
        class HttpServer:
            pass

        entered = threading.Event()
        release = threading.Event()
        manager = ExecutionManager(self.store, execute=lambda *_args, **_kwargs: None)

        def recover(_store):
            entered.set()
            release.wait(2)
            return StartupRecoveryResult()

        starter = threading.Thread(target=app.start_execution_manager, args=(HttpServer(), manager))
        with mock.patch("debbuilder.app.execution_recovery.recover_startup", side_effect=recover):
            starter.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(manager.accepting)
            self.assertIsNone(manager.worker)
            release.set()
            starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertTrue(manager.accepting)
        self.assertTrue(manager.worker.is_alive())
        manager.stop(timeout=2)

    def test_strong_recovery_starting_absent_and_matching_active(self):
        run = self.create("strong-primitive", "running")
        starting = self.systemd_identity(run["id"], "starting")
        recorder = mock.Mock()

        class AbsentConnection:
            def snapshot(self, _unit):
                return None

            def close(self):
                pass

        absent = recover_systemd_containment(
            starting, expected_run_id=run["id"], recorder=recorder,
            connection_factory=AbsentConnection,
        )
        self.assertEqual(absent.verification.status, VerificationStatus.NOT_RUNNING)
        self.assertTrue(absent.gone)

        active = self.systemd_identity(run["id"], "active")

        class MatchingConnection:
            stopped = False

            def snapshot(self, unit):
                if self.stopped:
                    return None
                return UnitSnapshot(
                    unit, unit_description(run["id"], active["command_id"]), True,
                    active["invocation_id"], active["control_group"], "active", "running",
                    "exec", "cgroup", "control-group", "success", 123, 0, 0,
                )

            def stop(self, _unit):
                self.stopped = True

            def reset_failed(self, _unit):
                pass

            def close(self):
                pass

        matching = MatchingConnection()
        recorder = mock.Mock()
        recorder.update.return_value = True
        recovered = recover_systemd_containment(
            active, expected_run_id=run["id"], recorder=recorder,
            connection_factory=lambda: matching,
        )
        self.assertEqual(recovered.verification.status, VerificationStatus.NOT_RUNNING)
        self.assertTrue(recovered.signalled)
        self.assertTrue(recovered.gone)
        recorder.update.assert_called_once()

    def test_strong_recovery_mismatch_and_unavailable_never_stop(self):
        run = self.create("strong-fail-closed", "running")
        active = self.systemd_identity(run["id"], "active")

        class MismatchConnection:
            def __init__(self):
                self.stop_calls = []

            def snapshot(self, unit):
                return UnitSnapshot(
                    unit, "different binding", True, active["invocation_id"],
                    active["control_group"], "active", "running", "exec", "cgroup",
                    "control-group", "success", 123, 0, 0,
                )

            def stop(self, unit):
                self.stop_calls.append(unit)

            def close(self):
                pass

        mismatch_connection = MismatchConnection()
        mismatch = recover_systemd_containment(
            active, expected_run_id=run["id"], recorder=mock.Mock(),
            connection_factory=lambda: mismatch_connection,
        )
        self.assertEqual(mismatch.verification.status, VerificationStatus.MISMATCH)
        self.assertEqual(mismatch_connection.stop_calls, [])

        unavailable = recover_systemd_containment(
            active, expected_run_id=run["id"], recorder=mock.Mock(),
            connection_factory=lambda: (_ for _ in ()).throw(ContainmentError("unavailable")),
        )
        self.assertEqual(unavailable.verification.status, VerificationStatus.UNVERIFIABLE)

    def test_strong_recovery_revalidates_unit_immediately_before_stop(self):
        run = self.create("strong-transition", "running")
        active = self.systemd_identity(run["id"], "active")
        matching = UnitSnapshot(
            active["unit_name"], unit_description(run["id"], active["command_id"]), True,
            active["invocation_id"], active["control_group"], "active", "running",
            "exec", "cgroup", "control-group", "success", 123, 0, 0,
        )
        reused = UnitSnapshot(
            active["unit_name"], unit_description(run["id"], active["command_id"]), True,
            "f" * 32, active["control_group"], "active", "running",
            "exec", "cgroup", "control-group", "success", 456, 0, 0,
        )

        class TransitionConnection:
            def __init__(self):
                self.snapshots = iter((matching, reused))
                self.stop_calls = []

            def snapshot(self, _unit):
                return next(self.snapshots)

            def stop(self, unit):
                self.stop_calls.append(unit)

            def close(self):
                pass

        connection = TransitionConnection()
        recovered = recover_systemd_containment(
            active, expected_run_id=run["id"], recorder=mock.Mock(),
            connection_factory=lambda: connection,
        )

        self.assertEqual(recovered.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertIn("not authorized", recovered.verification.reason)
        self.assertEqual(connection.stop_calls, [])

        class PostPersistenceTransitionConnection:
            def __init__(self):
                self.snapshots = iter((matching, matching, reused))
                self.stop_calls = []

            def snapshot(self, _unit):
                return next(self.snapshots)

            def stop(self, unit):
                self.stop_calls.append(unit)

            def close(self):
                pass

        connection = PostPersistenceTransitionConnection()
        recorder = mock.Mock()
        recorder.update.return_value = True
        recovered = recover_systemd_containment(
            active, expected_run_id=run["id"], recorder=recorder,
            connection_factory=lambda: connection,
        )

        self.assertEqual(recovered.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertIn("after persisting stop intent", recovered.verification.reason)
        self.assertEqual(connection.stop_calls, [])


class StartupRecoveryBlocker:
    @staticmethod
    def sample():
        return {
            "code": BLOCKER_CODE,
            "message": "blocked by recovery",
            "details": {"unresolved_run_ids": ["old-run"], "unresolved_count": 1, "stray_unit_count": 0},
        }


@unittest.skipUnless(containment_capability().available, "systemd/cgroup containment is unavailable")
class RealSystemdStartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.units = set()

    def tearDown(self):
        connection = _SystemdConnection()
        try:
            for unit in self.units:
                try:
                    connection.stop(unit)
                except Exception:
                    pass
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    snapshot = connection.snapshot(unit)
                    if snapshot is None:
                        break
                    if snapshot.active_state == "failed" and not snapshot.control_group:
                        try:
                            connection.reset_failed(unit)
                        except Exception:
                            pass
                    time.sleep(0.02)
        finally:
            connection.close()

    def create_running(self, name):
        run = self.store.create(recipe(name), mode="build")
        run.update({"status": "running", "started_at": run["created_at"]})
        self.store.save(run)
        return run

    def start_unit(self, run, source, *, durable_state="active", persist=True):
        command_id = secrets.token_hex(16)
        unit = command_unit_name(run["id"], command_id)
        self.units.add(unit)
        starting = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "backend": "systemd_cgroup",
            "boot_id": current_boot_id(),
            "run_id": run["id"],
            "command_id": command_id,
            "unit_name": unit,
            "containment_state": "starting",
        }
        if persist and durable_state == "starting":
            with self.store.locked_run(run["id"]) as fd:
                persist_identity(fd, starting)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        connection = _SystemdConnection()
        try:
            connection.start_transient(
                unit,
                arguments=[sys.executable, "-c", source],
                executable=sys.executable,
                cwd=Path(run["workspace"]),
                environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                stdout_fd=null_fd,
                stderr_fd=null_fd,
                description=unit_description(run["id"], command_id),
            )
            deadline = time.monotonic() + 3
            snapshot = None
            while time.monotonic() < deadline:
                snapshot = connection.snapshot(unit)
                if snapshot and snapshot.invocation_id and snapshot.control_group:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.invocation_id)
            self.assertEqual(snapshot.control_group, expected_control_group(unit))
            if persist and durable_state != "starting":
                active = {
                    **starting,
                    "containment_state": durable_state,
                    "invocation_id": snapshot.invocation_id,
                    "control_group": snapshot.control_group,
                }
                with self.store.locked_run(run["id"]) as fd:
                    persist_identity(fd, active)
                return active
            return starting
        finally:
            connection.close()
            os.close(null_fd)

    def assert_unit_absent(self, unit):
        connection = _SystemdConnection()
        try:
            self.assertIsNone(connection.snapshot(unit))
        finally:
            connection.close()
        self.assertFalse((Path("/sys/fs/cgroup/system.slice") / unit).exists())

    def test_recovery_removes_real_starting_active_stopping_and_descendant_shapes(self):
        variants = [
            ("starting-window", "import time; time.sleep(30)", "starting"),
            ("active-simple", "import time; time.sleep(30)", "active"),
            (
                "leader-exit-child",
                "import subprocess; subprocess.Popen(['/usr/bin/sleep','30'])",
                "active",
            ),
            (
                "setsid-child",
                "import subprocess,time; subprocess.Popen(['/usr/bin/sleep','30'],start_new_session=True); time.sleep(30)",
                "active",
            ),
            (
                "double-fork",
                "import os,time; p=os.fork(); (os._exit(0) if p else None); os.setsid(); q=os.fork(); (os._exit(0) if q else None); time.sleep(30)",
                "active",
            ),
            (
                "term-resistant",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)",
                "stopping",
            ),
        ]
        for name, source, state in variants:
            with self.subTest(name=name):
                run = self.create_running(name)
                identity = self.start_unit(run, source, durable_state=state)
                time.sleep(0.1)

                result = recover_startup(self.store)

                self.assertIsNone(result.admission_blocker)
                self.assertEqual(self.store.load(run["id"])["error"]["code"], RECOVERY_ERROR_CODE)
                self.assertFalse((self.store.run_dir(run["id"]) / ACTIVE_COMMAND_FILE).exists())
                self.assert_unit_absent(identity["unit_name"])

    def test_real_invocation_mismatch_and_stray_unit_are_not_signalled(self):
        mismatch_run = self.create_running("real-mismatch")
        mismatch_identity = self.start_unit(
            mismatch_run, "import time; time.sleep(30)", durable_state="active",
        )
        with self.store.locked_run(mismatch_run["id"]) as fd:
            path = Path(os.readlink(f"/proc/self/fd/{fd}")) / ACTIVE_COMMAND_FILE
            value = json.loads(path.read_text())
            value["invocation_id"] = "f" * 32
            path.write_text(json.dumps(value))

        result = recover_startup(self.store)
        self.assertIsNotNone(result.admission_blocker)
        connection = _SystemdConnection()
        try:
            live = connection.snapshot(mismatch_identity["unit_name"])
            self.assertIsNotNone(live)
            self.assertTrue(live.control_group)
        finally:
            connection.close()

        stray_run = self.create_running("real-stray-owner")
        stray_identity = self.start_unit(
            stray_run, "import time; time.sleep(30)", persist=False,
        )
        # The owner Run is terminal solely to exclude it from non-terminal
        # recovery; no trustworthy metadata binds the unit to that history.
        stray_run["status"] = "failed"
        stray_run["finished_at"] = stray_run["created_at"]
        self.store.save(stray_run)
        result = recover_startup(self.store)
        self.assertIn(stray_identity["unit_name"], result.stray_units)
        connection = _SystemdConnection()
        try:
            self.assertIsNotNone(connection.snapshot(stray_identity["unit_name"]))
        finally:
            connection.close()


class RecoveryAdmissionApiTests(AdminApiCase):
    def error_response(self, method, path, body):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(method, path, body)
        return captured.exception.code, json.loads(captured.exception.read())

    def test_unresolved_recovery_blocks_build_and_test_but_keeps_history_readable(self):
        app.stop_execution_manager(self.httpd, timeout=5)
        store = BuildStore(app.DATA / "builds")
        stale = store.create(recipe("api-stale"), mode="build", run_id="api-stale")
        stale["status"] = "running"
        stale["started_at"] = stale["created_at"]
        store.save(stale)
        before = set(path.name for path in store.root.iterdir())
        with mock.patch("debbuilder.execution_recovery.loaded_command_units", return_value=set()):
            self.execution_manager = app.start_execution_manager(self.httpd)

        for dry_run in (True, False):
            status, response = self.error_response(
                "POST", "/api/run", {"workflow": recipe(f"blocked-{dry_run}"), "dry_run": dry_run},
            )
            self.assertEqual(status, 503)
            self.assertEqual(response["error"]["code"], BLOCKER_CODE)
            self.assertEqual(response["error"]["details"]["unresolved_run_ids"], [stale["id"]])
        self.assertEqual(set(path.name for path in store.root.iterdir()), before)
        status, response = self.request("GET", f"/api/executions/{stale['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(response["execution"]["recovery"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
