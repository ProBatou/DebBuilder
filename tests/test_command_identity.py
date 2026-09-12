import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import debbuilder.command_containment as containment
import debbuilder.command_runner as command_runner
from debbuilder.command_identity import (
    ACTIVE_COMMAND_FILE,
    CommandIdentityError,
    MAX_IDENTITY_BYTES,
    VerificationStatus,
    _read_proc_stat,
    capture_identity,
    clear_identity,
    persist_identity,
    read_persisted_identity,
    recording_identities,
    terminate_verified_process_group,
    verify_identity,
    verify_persisted_identity,
    validated_identity,
)
from debbuilder.command_runner import run_command
from debbuilder.command_containment import command_unit_name, expected_control_group
from debbuilder.execution_cancellation import ExecutionCancelled
from debbuilder.build_store import BuildStore


def recipe(name="identity-run"):
    return {
        "name": name,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Identity <identity@example.test>",
            "description": "Identity test",
        },
        "source": {"repository": f"owner/{name}"},
    }


@unittest.skipUnless(sys.platform.startswith("linux") and Path("/proc/self/stat").is_file(), "Linux /proc is required")
class CommandIdentityTests(unittest.TestCase):
    def setUp(self):
        runtime_blocker = mock.patch.object(containment, "_RUNTIME_CLEANUP_ERROR", "")
        runtime_blocker.start()
        self.addCleanup(runtime_blocker.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def spawn(self, source: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    @staticmethod
    def reaping_wait(process):
        def wait(delay):
            process.poll()
            if delay:
                time.sleep(delay)
        return wait

    @staticmethod
    def assert_pids_gone(test, pids):
        deadline = time.monotonic() + 2
        remaining = set(pids)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    remaining.remove(pid)
            if remaining:
                time.sleep(0.01)
        test.assertEqual(remaining, set(), f"processes still exist: {sorted(remaining)}")

    def live_identity(self, source="import time; print('ready', flush=True); time.sleep(30)"):
        from debbuilder.command_identity import COMMAND_ID_ENV, RUN_ID_ENV
        run_id, command_id = "run-one", "command-one"
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, RUN_ID_ENV: run_id, COMMAND_ID_ENV: command_id},
        )
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process, capture_identity(process.pid, run_id=run_id, command_id=command_id)

    def test_v3_binds_policy_and_v1_v2_process_identities_remain_readable(self):
        process, current = self.live_identity()
        self.assertEqual(current["schema_version"], 3)
        self.assertIn("resource_limits", current)
        self.assertEqual(current["resource_io_targets"], [])
        previous = {key: value for key, value in current.items() if key not in {"resource_limits", "resource_io_targets"}}
        previous["schema_version"] = 2
        self.assertEqual(validated_identity(previous), previous)
        legacy = {key: value for key, value in previous.items() if key not in {"backend", "containment_state"}}
        legacy["schema_version"] = 1
        self.assertEqual(validated_identity(legacy), legacy)

    def test_v2_systemd_starting_and_active_identities_remain_readable(self):
        run_id = "v2-systemd"
        command_id = "a" * 32
        unit_name = command_unit_name(run_id, command_id)
        starting = {
            "schema_version": 2,
            "backend": "systemd_cgroup",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "run_id": run_id,
            "command_id": command_id,
            "unit_name": unit_name,
            "containment_state": "starting",
        }
        self.assertEqual(validated_identity(starting), starting)
        active = {
            **starting,
            "containment_state": "active",
            "invocation_id": "b" * 32,
            "control_group": expected_control_group(unit_name),
        }
        self.assertEqual(validated_identity(active), active)

    def test_v3_resource_policy_must_be_an_object(self):
        _process, identity = self.live_identity()
        identity["resource_limits"] = None
        with self.assertRaises(CommandIdentityError):
            validated_identity(identity)

    def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm(self):
        proc = self.root / "proc"
        stat_path = proc / "42" / "stat"
        stat_path.parent.mkdir(parents=True)
        suffix = ["S", "1", "42"] + ["0"] * 16 + ["987654"] + ["0"] * 5
        stat_path.write_text(f"42 (name with ) parens) {' '.join(suffix)}\n")
        self.assertEqual(_read_proc_stat(42, proc), (42, 987654))

    def test_matching_live_process_and_natural_exit(self):
        process, identity = self.live_identity("import time; print('ready', flush=True); time.sleep(.1)")
        self.assertEqual(verify_identity(identity).status, VerificationStatus.MATCH)
        process.wait(timeout=2)
        self.assertEqual(verify_identity(identity).status, VerificationStatus.NOT_RUNNING)

    def test_start_time_pgid_and_boot_mismatches_never_signal(self):
        process, identity = self.live_identity()
        variants = []
        changed = dict(identity)
        changed["start_time_ticks"] += 1
        variants.append(changed)
        changed = dict(identity)
        changed["pgid"] += 1
        variants.append(changed)
        changed = dict(identity)
        changed["boot_id"] = "00000000-0000-0000-0000-000000000000"
        variants.append(changed)
        with mock.patch("debbuilder.command_runner._signal_process_group") as send:
            for value in variants:
                with self.subTest(identity=value):
                    self.assertEqual(verify_identity(value).status, VerificationStatus.MISMATCH)
                    result = terminate_verified_process_group(value, expected_run_id=identity["run_id"])
                    self.assertEqual(result.verification.status, VerificationStatus.MISMATCH)
                    self.assertFalse(result.signalled)
            send.assert_not_called()
        self.assertIsNone(process.poll())

    def test_wrong_pgid_is_mismatch_even_when_pid_is_live(self):
        process, identity = self.live_identity()
        identity["pgid"] += 1
        self.assertEqual(verify_identity(identity).status, VerificationStatus.MISMATCH)
        self.assertIsNone(process.poll())

    def test_malformed_and_oversized_persisted_metadata_are_unverifiable(self):
        self.assertEqual(verify_identity({"pid": os.getpid()}).status, VerificationStatus.UNVERIFIABLE)
        store = BuildStore(self.root / "builds")
        for name, payload in (("malformed", b"{not-json"), ("oversized", b"x" * (MAX_IDENTITY_BYTES + 1))):
            with self.subTest(name=name):
                run = store.create(recipe(name), mode="build")
                with store.locked_run(run["id"]) as fd:
                    file_fd = os.open(ACTIVE_COMMAND_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                    with os.fdopen(file_fd, "wb") as handle:
                        handle.write(payload)
                    identity, result = verify_persisted_identity(fd, expected_run_id=run["id"])
                self.assertIsNone(identity)
                self.assertEqual(result.status, VerificationStatus.UNVERIFIABLE)

        with mock.patch("debbuilder.command_runner._signal_process_group") as send:
            result = terminate_verified_process_group({"pid": os.getpid()}, expected_run_id="malformed")
        self.assertEqual(result.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertFalse(result.signalled)
        send.assert_not_called()

    def test_symlink_metadata_and_wrong_run_are_unverifiable_and_never_signalled(self):
        store = BuildStore(self.root / "untrusted-builds")
        run = store.create(recipe("expected-run"), mode="build")
        target = self.root / "outside.json"
        target.write_text("{}")
        with store.locked_run(run["id"]) as fd:
            os.symlink(target, ACTIVE_COMMAND_FILE, dir_fd=fd)
            persisted, result = verify_persisted_identity(fd, expected_run_id=run["id"])
        self.assertIsNone(persisted)
        self.assertEqual(result.status, VerificationStatus.UNVERIFIABLE)

        process, identity = self.live_identity()
        with mock.patch("debbuilder.command_runner._signal_process_group") as send:
            result = terminate_verified_process_group(identity, expected_run_id=run["id"])
        self.assertEqual(result.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertFalse(result.signalled)
        send.assert_not_called()
        self.assertIsNone(process.poll())

    def test_malformed_proc_identity_is_unverifiable_and_never_signalled(self):
        process, identity = self.live_identity()
        with (
            mock.patch(
                "debbuilder.command_identity._read_proc_stat",
                side_effect=CommandIdentityError("Linux process stat is malformed"),
            ),
            mock.patch("debbuilder.command_runner._signal_process_group") as send,
        ):
            result = terminate_verified_process_group(identity, expected_run_id=identity["run_id"])
        self.assertEqual(result.verification.status, VerificationStatus.UNVERIFIABLE)
        self.assertFalse(result.signalled)
        send.assert_not_called()
        self.assertIsNone(process.poll())

    def test_cooperative_verified_group_uses_sigterm_without_sigkill(self):
        process, identity = self.live_identity()
        result = terminate_verified_process_group(
            identity, expected_run_id=identity["run_id"], on_wait=self.reaping_wait(process),
        )
        self.assertTrue(result.signalled)
        self.assertFalse(result.killed)
        self.assertTrue(result.group_gone)
        self.assertEqual(result.error, "")
        self.assertEqual(process.wait(timeout=2), -signal.SIGTERM)

    def test_sigterm_resistant_verified_group_escalates_to_sigkill(self):
        process, identity = self.live_identity(
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
        )
        result = terminate_verified_process_group(
            identity, expected_run_id=identity["run_id"], on_wait=self.reaping_wait(process),
        )
        self.assertTrue(result.signalled)
        self.assertTrue(result.killed)
        self.assertTrue(result.group_gone)
        self.assertEqual(result.error, "")
        self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)

    def test_verified_leader_termination_removes_parent_child_and_grandchild(self):
        script = self.root / "tree.py"
        marker = self.root / "pids"
        script.write_text(
            "import os,subprocess,sys,time\n"
            "level=int(sys.argv[1]); marker=sys.argv[2]\n"
            "with open(marker, 'a') as out: out.write(str(os.getpid()) + '\\n')\n"
            "if level: subprocess.Popen([sys.executable, __file__, str(level-1), marker])\n"
            "print('ready', flush=True)\n"
            "while True: time.sleep(.05)\n"
        )
        from debbuilder.command_identity import COMMAND_ID_ENV, RUN_ID_ENV
        process = subprocess.Popen(
            [sys.executable, str(script), "2", str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
            env={**os.environ, RUN_ID_ENV: "tree-run", COMMAND_ID_ENV: "tree-command"},
        )
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if marker.exists() and len(marker.read_text().splitlines()) == 3:
                break
            time.sleep(0.01)
        pids = [int(value) for value in marker.read_text().splitlines()]
        self.assertEqual(len(pids), 3)
        identity = capture_identity(process.pid, run_id="tree-run", command_id="tree-command")
        result = terminate_verified_process_group(
            identity, expected_run_id=identity["run_id"], on_wait=self.reaping_wait(process),
        )
        self.assertTrue(result.group_gone)
        self.assertFalse(result.killed)
        self.assert_pids_gone(self, pids)

    def test_runner_persists_identity_before_output_and_clears_after_cancellation(self):
        store = BuildStore(self.root / "builds")
        run = store.create(recipe(), mode="build")
        requested = threading.Event()
        observed = []
        command = f"{shlex.quote(sys.executable)} -c 'import time; print(\"ready\", flush=True); time.sleep(30)'"
        with store.locked_run(run["id"]) as fd:
            def output(item):
                if "ready" in item["text"]:
                    identity = read_persisted_identity(fd)
                    observed.append((identity, verify_identity(identity, expected_run_id=run["id"])))
                    requested.set()

            with recording_identities(
                run["id"],
                record=lambda identity: persist_identity(fd, identity),
                clear=lambda identity: clear_identity(fd, identity),
            ):
                with self.assertRaises(ExecutionCancelled):
                    run_command(
                        command,
                        workspace=run["workspace"],
                        cancellation_event=requested,
                        on_cancel=lambda: {"stage": "build"},
                        on_output=output,
                    )
            self.assertIsNone(read_persisted_identity(fd))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][1].status, VerificationStatus.MATCH)
        self.assertEqual(observed[0][0]["run_id"], run["id"])
        self.assertNotIn("command", observed[0][0])

    def test_sequential_commands_use_distinct_ids_and_leave_no_identity(self):
        store = BuildStore(self.root / "builds")
        run = store.create(recipe(), mode="build")
        recorded = []
        with store.locked_run(run["id"]) as fd:
            def record(identity):
                recorded.append(identity)
                persist_identity(fd, identity)

            with recording_identities(
                run["id"], record=record,
                clear=lambda identity: clear_identity(fd, identity),
            ):
                self.assertEqual(run_command("true", workspace=run["workspace"])["status"], "success")
                self.assertEqual(run_command("true", workspace=run["workspace"])["status"], "success")
            self.assertIsNone(read_persisted_identity(fd))
        self.assertEqual(len({row["command_id"] for row in recorded}), 2)

    def test_capture_accepts_missing_zombie_markers_only_after_owned_child_exit(self):
        command_id = "a" * 32
        with (
            mock.patch("debbuilder.command_identity._read_proc_stat", return_value=(123, 456)),
            mock.patch("debbuilder.command_identity._read_proc_identity_environment", return_value=("", "")),
            mock.patch("debbuilder.command_identity._read_boot_id", return_value="b" * 32),
        ):
            with self.assertRaises(CommandIdentityError):
                capture_identity(123, run_id="zombie-run", command_id=command_id)
            identity = capture_identity(
                123,
                run_id="zombie-run",
                command_id=command_id,
                process_exited=lambda: True,
            )
        self.assertEqual(identity["pid"], 123)
        self.assertEqual(identity["start_time_ticks"], 456)
        self.assertEqual(identity["command_id"], command_id)

    def test_capture_preserves_owned_identity_when_environment_vanishes_after_stat(self):
        command_id = "a" * 32
        with (
            mock.patch("debbuilder.command_identity._read_proc_stat", return_value=(123, 456)),
            mock.patch(
                "debbuilder.command_identity._read_proc_identity_environment",
                side_effect=ProcessLookupError(3, "process exited"),
            ),
            mock.patch("debbuilder.command_identity._read_boot_id", return_value="b" * 32),
        ):
            with self.assertRaises(ProcessLookupError):
                capture_identity(123, run_id="short-run", command_id=command_id)
            identity = capture_identity(
                123,
                run_id="short-run",
                command_id=command_id,
                allow_vanished_environment=True,
            )
        self.assertEqual(identity["pid"], 123)
        self.assertEqual(identity["pgid"], 123)
        self.assertEqual(identity["start_time_ticks"], 456)

    def test_capture_accepts_empty_markers_only_for_a_terminal_owned_child(self):
        command_id = "a" * 32
        with (
            mock.patch("debbuilder.command_identity._read_proc_stat", return_value=(123, 456)),
            mock.patch("debbuilder.command_identity._read_proc_identity_environment", return_value=("", "")),
            mock.patch("debbuilder.command_identity._read_boot_id", return_value="b" * 32),
            mock.patch("debbuilder.command_identity._process_is_terminal", return_value=False),
        ):
            with self.assertRaises(CommandIdentityError):
                capture_identity(
                    123,
                    run_id="short-run",
                    command_id=command_id,
                    process_exited=lambda: False,
                    allow_vanished_environment=True,
                )
        with (
            mock.patch("debbuilder.command_identity._read_proc_stat", return_value=(123, 456)),
            mock.patch("debbuilder.command_identity._read_proc_identity_environment", return_value=("", "")),
            mock.patch("debbuilder.command_identity._read_boot_id", return_value="b" * 32),
            mock.patch("debbuilder.command_identity._process_is_terminal", return_value=True),
        ):
            identity = capture_identity(
                123,
                run_id="short-run",
                command_id=command_id,
                process_exited=lambda: False,
                allow_vanished_environment=True,
            )
        self.assertEqual(identity["pid"], 123)
        self.assertEqual(identity["start_time_ticks"], 456)

    def test_spawn_failure_does_not_create_active_identity(self):
        store = BuildStore(self.root / "spawn-failure-builds")
        run = store.create(recipe("spawn-failure"), mode="build")
        with store.locked_run(run["id"]) as fd:
            with recording_identities(
                run["id"],
                record=lambda identity: persist_identity(fd, identity),
                clear=lambda identity: clear_identity(fd, identity),
            ):
                with mock.patch("debbuilder.command_runner.subprocess.Popen", side_effect=OSError("injected spawn failure")):
                    result = run_command("true", workspace=run["workspace"])
            self.assertIsNone(read_persisted_identity(fd))
        self.assertEqual(result["status"], "failed")
        self.assertIn("injected spawn failure", result["stderr"])

    def test_timeout_and_unexpected_callback_exception_clear_verified_identity(self):
        store = BuildStore(self.root / "cleanup-builds")
        for name in ("timeout", "exception"):
            with self.subTest(name=name):
                run = store.create(recipe(name), mode="build")
                with store.locked_run(run["id"]) as fd:
                    with recording_identities(
                        run["id"],
                        record=lambda identity: persist_identity(fd, identity),
                        clear=lambda identity: clear_identity(fd, identity),
                    ):
                        command = f"{shlex.quote(sys.executable)} -c 'import time; print(\"ready\", flush=True); time.sleep(30)'"
                        if name == "timeout":
                            result = run_command(command, workspace=run["workspace"], inactivity_timeout=0.05)
                            self.assertTrue(result["timed_out"])
                        else:
                            with self.assertRaisesRegex(RuntimeError, "injected output failure"):
                                run_command(
                                    command,
                                    workspace=run["workspace"],
                                    on_output=lambda _item: (_ for _ in ()).throw(RuntimeError("injected output failure")),
                                )
                    self.assertIsNone(read_persisted_identity(fd))

    def test_unverified_group_disappearance_retains_identity_for_future_recovery(self):
        store = BuildStore(self.root / "uncertain-cleanup-builds")
        run = store.create(recipe("uncertain-cleanup"), mode="build")
        real_group_exists = command_runner._process_group_exists

        def unverifiable_after_exit(process_group):
            if real_group_exists(process_group):
                return True
            raise PermissionError("injected group verification failure")

        command = f"{shlex.quote(sys.executable)} -c 'print(\"ready\", flush=True)'"
        with store.locked_run(run["id"]) as fd:
            with recording_identities(
                run["id"],
                record=lambda identity: persist_identity(fd, identity),
                clear=lambda identity: clear_identity(fd, identity),
            ):
                with mock.patch(
                    "debbuilder.command_runner._process_group_exists",
                    side_effect=unverifiable_after_exit,
                ):
                    result = run_command(command, workspace=run["workspace"])
            identity = read_persisted_identity(fd)
            self.assertIsNotNone(identity)
            self.assertEqual(verify_identity(identity, expected_run_id=run["id"]).status, VerificationStatus.NOT_RUNNING)
        self.assertEqual(result["status"], "failed")
        self.assertIn("injected group verification failure", result["stderr"])

    def test_crash_after_spawn_before_persistence_leaves_unrecognized_live_process(self):
        store = BuildStore(self.root / "window-a-builds")
        run = store.create(recipe("window-a"), mode="build")
        helper = r'''
import json, os, sys, time
from debbuilder.command_identity import recording_identities
from debbuilder.command_runner import run_command
run_id, workspace = os.environ["RUN_ID"], os.environ["WORKSPACE"]
def before_persist(identity):
    print(json.dumps(identity), flush=True)
    while True: time.sleep(1)
with recording_identities(run_id, record=before_persist, clear=lambda identity: True):
    run_command("sleep 30", workspace=workspace, inactivity_timeout=None)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", helper],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "RUN_ID": run["id"], "WORKSPACE": run["workspace"]},
        )
        self.processes.append(process)
        identity = json.loads(process.stdout.readline())
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        with store.locked_run(run["id"]) as fd:
            persisted, result = verify_persisted_identity(fd, expected_run_id=run["id"])
        self.assertIsNone(persisted)
        self.assertEqual(result.status, VerificationStatus.UNVERIFIABLE)
        self.assertEqual(verify_identity(identity).status, VerificationStatus.MATCH)
        os.killpg(identity["pgid"], signal.SIGKILL)
        self.assert_pids_gone(self, [identity["pid"]])

    def test_crash_after_exit_before_clear_leaves_safe_not_running_identity(self):
        store = BuildStore(self.root / "window-b-builds")
        run = store.create(recipe("window-b"), mode="build")
        helper = r'''
import os, time
from debbuilder.build_store import BuildStore
from debbuilder.command_identity import clear_identity, persist_identity, recording_identities
store=BuildStore(os.environ["BUILDS"]); run_id=os.environ["RUN_ID"]
with store.locked_run(run_id) as fd:
    def before_clear(identity):
        print("clear-window", flush=True)
        while True: time.sleep(1)
    with recording_identities(run_id, record=lambda identity: persist_identity(fd, identity), clear=before_clear):
        run_command = __import__("debbuilder.command_runner", fromlist=["run_command"]).run_command
        run_command("true", workspace=os.environ["WORKSPACE"])
'''
        process = subprocess.Popen(
            [sys.executable, "-c", helper],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={
                **os.environ,
                "BUILDS": str(store.root),
                "RUN_ID": run["id"],
                "WORKSPACE": run["workspace"],
            },
        )
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), "clear-window")
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        with store.locked_run(run["id"]) as fd:
            identity, result = verify_persisted_identity(fd, expected_run_id=run["id"])
            self.assertEqual(result.status, VerificationStatus.NOT_RUNNING)
            self.assertTrue(clear_identity(fd, identity))
            self.assertIsNone(read_persisted_identity(fd))


if __name__ == "__main__":
    unittest.main()
