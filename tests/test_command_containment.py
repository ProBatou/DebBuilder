import json
import os
import selectors
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

import debbuilder.command_containment as containment_module
import debbuilder.command_runner as command_runner_module
from debbuilder.build_store import BuildStore
from debbuilder.command_containment import (
    ContainmentCapability,
    SystemdCommandContainment,
    UnitSnapshot,
    _SystemdConnection,
    command_unit_name,
    containment_capability,
    expected_control_group,
    unit_description,
    verify_unit,
)
from debbuilder.command_identity import (
    IdentityRecorder,
    VerificationStatus,
    clear_identity,
    persist_identity,
    read_persisted_identity,
    recording_identities,
    update_identity,
)
from debbuilder.command_runner import run_command
from debbuilder.execution_cancellation import ExecutionCancelled


def recipe(name):
    return {
        "name": name,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Containment <containment@example.test>",
            "description": "Containment test",
        },
        "source": {"repository": f"owner/{name}"},
    }


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux is required")
class SystemdCommandContainmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capability = containment_capability(refresh=True)
        if not cls.capability.available:
            raise unittest.SkipTest(cls.capability.reason)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.units = set()

    def tearDown(self):
        connection = _SystemdConnection()
        try:
            for unit_name in self.units:
                try:
                    connection.stop(unit_name)
                except Exception:
                    pass
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    snapshot = connection.snapshot(unit_name)
                    if snapshot is None:
                        break
                    if snapshot.active_state == "failed" and not snapshot.control_group:
                        try:
                            connection.reset_failed(unit_name)
                        except Exception:
                            pass
                    time.sleep(0.02)
        finally:
            connection.close()

    def create_run(self, name):
        return self.store.create(recipe(name), mode="build")

    def run_strong(self, run, command, **kwargs):
        transitions = []
        with self.store.locked_run(run["id"]) as fd:
            self.running_fd = fd
            def record(value):
                transitions.append(dict(value))
                self.units.add(value["unit_name"])
                persist_identity(fd, value)

            def update(expected, updated):
                changed = update_identity(fd, expected, updated)
                if changed:
                    transitions.append(dict(updated))
                return changed

            with recording_identities(
                run["id"], record=record, update=update,
                clear=lambda value: clear_identity(fd, value),
            ):
                try:
                    result = run_command(command, workspace=run["workspace"], **kwargs)
                except ExecutionCancelled as exc:
                    result = exc.command_result
            remaining = read_persisted_identity(fd)
            self.running_fd = None
        return result, transitions, remaining

    @staticmethod
    def process_cgroup(pid):
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        return next(row.split("::", 1)[1] for row in rows if row.startswith("0::"))

    def test_simple_command_streams_both_outputs_and_clears_metadata(self):
        run = self.create_run("simple")
        chunks = []
        result, transitions, remaining = self.run_strong(
            run,
            f"{shlex.quote(sys.executable)} -c 'import os,sys; print(os.getcwd(), os.environ[\"API_TOKEN\"]); print(\"error\",file=sys.stderr)'",
            environment={"API_TOKEN": "secret-value"},
            on_output=chunks.append,
        )
        self.assertEqual(result["status"], "success")
        self.assertIn(run["workspace"], result["stdout"])
        self.assertIn("[REDACTED]", result["stdout"])
        self.assertNotIn("secret-value", json.dumps(result) + json.dumps(chunks))
        self.assertIn("error", result["stderr"])
        self.assertTrue(any(row["stream"] == "stdout" for row in chunks))
        self.assertIsNone(remaining)
        self.assertEqual([row["containment_state"] for row in transitions], ["starting", "active"])
        active = transitions[-1]
        self.assertEqual(active["backend"], "systemd_cgroup")
        self.assertRegex(active["invocation_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(active["control_group"], expected_control_group(active["unit_name"]))
        self.assertNotIn("argv", active)
        self.assertNotIn("environment", active)

    def test_all_descendant_shapes_remain_in_the_exact_cgroup(self):
        variants = {
            "child-grandchild": (
                "import os,subprocess,sys,time; "
                "c=subprocess.Popen([sys.executable,'-c',"
                "'import os,subprocess,sys,time; g=subprocess.Popen([\"/usr/bin/sleep\",\"30\"]); print(os.getpid(),g.pid,flush=True); time.sleep(30)']); "
                "print(os.getpid(),c.pid,flush=True); time.sleep(30)"
            ),
            "new-pgid": "import os,subprocess,time; c=subprocess.Popen(['/usr/bin/sleep','30'],process_group=0); print(os.getpid(),c.pid,flush=True); time.sleep(30)",
            "setsid": "import os,subprocess,time; c=subprocess.Popen(['/usr/bin/sleep','30'],start_new_session=True); print(os.getpid(),c.pid,flush=True); time.sleep(30)",
            "cleared-environment": "import os,subprocess,time; c=subprocess.Popen(['/usr/bin/sleep','30'],env={}); print(os.getpid(),c.pid,flush=True); time.sleep(30)",
            "double-fork": (
                "import os,time; p=os.fork(); "
                "(os._exit(0) if p else None); "
                "os.setsid(); q=os.fork(); "
                "(os._exit(0) if q else None); "
                "print(os.getpid(),flush=True); time.sleep(30)"
            ),
        }
        for index, (name, source) in enumerate(variants.items()):
            with self.subTest(name=name):
                run = self.create_run(f"tree-{index}")
                requested = threading.Event()
                observed = []
                observed_groups = []

                def output(item):
                    values = [int(value) for value in item["text"].split() if value.isdigit()]
                    if values:
                        observed.extend(values)
                        for pid in values:
                            try:
                                observed_groups.append((pid, self.process_cgroup(pid)))
                            except FileNotFoundError:
                                pass
                        requested.set()

                result, transitions, remaining = self.run_strong(
                    run,
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}",
                    cancellation_event=requested,
                    on_cancel=lambda: {"stage": "build"},
                    on_output=output,
                )
                active = next(row for row in transitions if row["containment_state"] == "active")
                self.assertTrue(observed)
                self.assertTrue(observed_groups)
                for _pid, control_group in observed_groups:
                    self.assertEqual(control_group, active["control_group"])
                self.assertEqual(result["status"], "cancelled")
                self.assertIsNone(result["termination_error"])
                self.assertIsNone(remaining)

    def test_leader_exit_does_not_complete_before_descendant(self):
        run = self.create_run("leader-first")
        marker = Path(run["workspace"]) / "leader-child"
        temporary_marker = marker.with_suffix(".tmp")
        source = (
            "import os,subprocess,time; "
            f"c=subprocess.Popen(['/usr/bin/sleep','.5']); f=open({str(temporary_marker)!r},'w'); "
            f"f.write(f'{{os.getpid()}} {{c.pid}}'); f.close(); os.replace({str(temporary_marker)!r},{str(marker)!r}); "
            "print('spawned',flush=True)"
        )
        holder = {}

        def execute():
            holder["value"] = self.run_strong(run, f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}")

        thread = threading.Thread(target=execute)
        started = time.monotonic()
        thread.start()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        leader, child = map(int, marker.read_text().split())
        while Path(f"/proc/{leader}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        active = read_persisted_identity(self.running_fd)
        connection = _SystemdConnection()
        try:
            snapshot, verification = verify_unit(connection, active)
            self.assertEqual(verification.status, VerificationStatus.MATCH)
            self.assertEqual(snapshot.active_state, "active")
            self.assertTrue(snapshot.control_group)
            self.assertTrue(Path(f"/proc/{child}").exists())
        finally:
            connection.close()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        result, _, remaining = holder["value"]
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual(result["status"], "success")
        self.assertIsNone(remaining)

    def test_cooperative_and_resistant_cancellation_remove_the_whole_unit(self):
        for resistant in (False, True):
            with self.subTest(resistant=resistant):
                run = self.create_run(f"cancel-{int(resistant)}")
                requested = threading.Event()
                handler = "signal.signal(signal.SIGTERM,signal.SIG_IGN);" if resistant else ""
                source = f"import signal,time; {handler} print('ready',flush=True); time.sleep(30)"
                result, _, remaining = self.run_strong(
                    run, f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}",
                    cancellation_event=requested,
                    on_cancel=lambda: {"stage": "build"},
                    on_output=lambda item: requested.set() if "ready" in item["text"] else None,
                )
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["killed"], resistant)
                self.assertIsNone(result["termination_error"])
                self.assertIsNone(remaining)

    def test_inactivity_and_maximum_runtime_terminate_the_unit(self):
        cases = (
            ("inactivity", {"inactivity_timeout": 0.1, "maximum_runtime": None}),
            ("maximum_runtime", {"inactivity_timeout": None, "maximum_runtime": 0.1}),
        )
        for name, kwargs in cases:
            with self.subTest(name=name):
                run = self.create_run(f"timeout-{name.replace('_', '-')}")
                with mock.patch("debbuilder.command_runner._signal_process_group") as signal_group:
                    result, _, remaining = self.run_strong(
                        run,
                        f"{shlex.quote(sys.executable)} -c 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'",
                        **kwargs,
                    )
                signal_group.assert_not_called()
                self.assertTrue(result["timed_out"])
                self.assertEqual(result["timeout_reason"], name)
                self.assertTrue(result["killed"])
                self.assertEqual(result["termination_error"], "")
                self.assertIsNone(remaining)

    def test_callback_exception_terminates_and_clears_the_unit(self):
        run = self.create_run("callback")
        with self.assertRaisesRegex(RuntimeError, "injected callback failure"):
            self.run_strong(
                run,
                f"{shlex.quote(sys.executable)} -c 'import time; print(\"ready\",flush=True); time.sleep(30)'",
                on_output=lambda _item: (_ for _ in ()).throw(RuntimeError("injected callback failure")),
            )
        with self.store.locked_run(run["id"]) as fd:
            self.assertIsNone(read_persisted_identity(fd))

    def test_output_selector_setup_failure_terminates_and_clears_the_unit(self):
        run = self.create_run("selector-setup")
        real_selector = selectors.DefaultSelector

        class BrokenSelector:
            def __init__(self):
                self.inner = real_selector()

            def register(self, *_args, **_kwargs):
                raise OSError("injected selector setup failure")

            def get_map(self):
                return self.inner.get_map()

            def close(self):
                self.inner.close()

        with mock.patch.object(command_runner_module.selectors, "DefaultSelector", BrokenSelector):
            result, transitions, remaining = self.run_strong(
                run, f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'",
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("selector setup failure", result["stderr"])
        self.assertIsNone(remaining)
        connection = _SystemdConnection()
        try:
            self.assertIsNone(connection.snapshot(transitions[0]["unit_name"]))
        finally:
            connection.close()

    def test_precommitted_starting_intent_precedes_start_transient_unit(self):
        run = self.create_run("precommit")
        observed = []
        real_start = _SystemdConnection.start_transient

        def start(connection, unit_name, **kwargs):
            observed.append(read_persisted_identity(self.running_fd))
            return real_start(connection, unit_name, **kwargs)

        with mock.patch.object(_SystemdConnection, "start_transient", new=start):
            result, _, remaining = self.run_strong(run, "true")
        self.assertEqual(result["status"], "success")
        self.assertEqual(observed[0]["containment_state"], "starting")
        self.assertIsNone(remaining)

    def test_starting_intent_without_unit_is_distinct_from_an_active_unit(self):
        run = self.create_run("never-created")
        command_id = "1" * 32
        from debbuilder.command_containment import starting_metadata

        intent = starting_metadata(run["id"], command_id)
        self.units.add(intent["unit_name"])
        with self.store.locked_run(run["id"]) as fd:
            persist_identity(fd, intent)
            connection = _SystemdConnection()
            try:
                snapshot, verification = verify_unit(connection, intent)
            finally:
                connection.close()
            self.assertIsNone(snapshot)
            self.assertEqual(verification.status, VerificationStatus.NOT_RUNNING)
            self.assertTrue(clear_identity(fd, intent))

    def test_unresolved_metadata_is_never_overwritten_by_a_fresh_command(self):
        run = self.create_run("unresolved-guard")
        from debbuilder.command_containment import starting_metadata

        unresolved = starting_metadata(run["id"], "2" * 32)
        self.units.add(unresolved["unit_name"])
        with self.store.locked_run(run["id"]) as fd:
            persist_identity(fd, unresolved)
            with recording_identities(
                run["id"],
                record=lambda value: persist_identity(fd, value),
                update=lambda expected, updated: update_identity(fd, expected, updated),
                clear=lambda value: clear_identity(fd, value),
            ):
                result = run_command("true", workspace=run["workspace"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("already has an active command", result["stderr"])
            self.assertEqual(read_persisted_identity(fd), unresolved)
            self.assertTrue(clear_identity(fd, unresolved))

    def test_crash_before_start_leaves_only_a_starting_intent(self):
        run = self.create_run("crash-before-start")
        helper = r'''
import json, os, time
from debbuilder.build_store import BuildStore
from debbuilder.command_identity import clear_identity, persist_identity, recording_identities, update_identity
from debbuilder.command_runner import run_command
store=BuildStore(os.environ["BUILDS"]); run_id=os.environ["RUN_ID"]
with store.locked_run(run_id) as fd:
    def record(intent):
        persist_identity(fd, intent)
        print(json.dumps(intent), flush=True)
        while True: time.sleep(1)
    with recording_identities(run_id, record=record, update=lambda a,b:update_identity(fd,a,b), clear=lambda x:clear_identity(fd,x)):
        run_command("sleep 30", workspace=os.environ["WORKSPACE"], inactivity_timeout=None)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "BUILDS": str(self.store.root), "RUN_ID": run["id"], "WORKSPACE": run["workspace"]},
        )
        try:
            intent = json.loads(process.stdout.readline())
            self.units.add(intent["unit_name"])
            process.kill()
            process.wait(timeout=3)
            with self.store.locked_run(run["id"]) as fd:
                persisted = read_persisted_identity(fd)
                self.assertEqual(persisted, intent)
                connection = _SystemdConnection()
                try:
                    snapshot, verification = verify_unit(connection, intent)
                finally:
                    connection.close()
                self.assertIsNone(snapshot)
                self.assertEqual(verification.status, VerificationStatus.NOT_RUNNING)
                self.assertTrue(clear_identity(fd, intent))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_crash_after_start_before_activation_persistence_is_discoverable(self):
        run = self.create_run("crash-after-start")
        helper = r'''
import json, os, time
from debbuilder.build_store import BuildStore
from debbuilder.command_identity import clear_identity, persist_identity, recording_identities, update_identity
from debbuilder.command_runner import run_command
store=BuildStore(os.environ["BUILDS"]); run_id=os.environ["RUN_ID"]
with store.locked_run(run_id) as fd:
    def before_update(expected, active):
        print(json.dumps(active), flush=True)
        while True: time.sleep(1)
    with recording_identities(run_id, record=lambda x:persist_identity(fd,x), update=before_update, clear=lambda x:clear_identity(fd,x)):
        run_command("sleep 30", workspace=os.environ["WORKSPACE"], inactivity_timeout=None)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "BUILDS": str(self.store.root), "RUN_ID": run["id"], "WORKSPACE": run["workspace"]},
        )
        connection = None
        try:
            activated = json.loads(process.stdout.readline())
            self.units.add(activated["unit_name"])
            process.kill()
            process.wait(timeout=3)
            with self.store.locked_run(run["id"]) as fd:
                intent = read_persisted_identity(fd)
                self.assertEqual(intent["containment_state"], "starting")
                self.assertEqual(intent["unit_name"], activated["unit_name"])
                connection = _SystemdConnection()
                snapshot, verification = verify_unit(connection, intent)
                self.assertEqual(verification.status, VerificationStatus.MATCH)
                self.assertEqual(snapshot.invocation_id, activated["invocation_id"])
                recorder = IdentityRecorder(run["id"], lambda _value: None, lambda _value: True)
                command = SystemdCommandContainment(connection, recorder, intent, None, None)
                terminated = command._terminate_record(intent, persist_stopping=False)
                self.assertTrue(terminated.gone, terminated.error)
                self.assertTrue(clear_identity(fd, intent))
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_identity_mismatch_never_authorizes_stop(self):
        run = self.create_run("mismatch")
        requested = threading.Event()
        checked = []

        def output(item):
            if "ready" not in item["text"]:
                return
            active = read_persisted_identity(self.running_fd)
            connection = _SystemdConnection()
            try:
                variants = []
                variants.append({**active, "invocation_id": "0" * 32})
                variants.append({**active, "boot_id": "00000000-0000-0000-0000-000000000000"})
                variants.append({
                    **active,
                    "control_group": "/system.slice/debbuilder-command-0000000000000000-" + "0" * 32 + ".service",
                })
                variants.append({
                    **active,
                    "unit_name": "debbuilder-command-0000000000000000-" + "0" * 32 + ".service",
                })
                for wrong in variants:
                    snapshot, verification = verify_unit(connection, wrong)
                    checked.append((snapshot, verification))
            finally:
                connection.close()
            requested.set()

        result, _, remaining = self.run_strong(
            run,
            f"{shlex.quote(sys.executable)} -c 'import time; print(\"ready\",flush=True); time.sleep(30)'",
            cancellation_event=requested,
            on_cancel=lambda: {"stage": "build"},
            on_output=output,
        )
        self.assertTrue(checked)
        self.assertTrue(all(row[1].status is VerificationStatus.MISMATCH for row in checked))
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(remaining)

    def test_reused_unit_name_with_new_invocation_does_not_match_old_metadata(self):
        run = self.create_run("reuse")
        result, transitions, remaining = self.run_strong(run, "true")
        self.assertEqual(result["status"], "success")
        self.assertIsNone(remaining)
        old = next(row for row in transitions if row["containment_state"] == "active")
        connection = _SystemdConnection()
        read_fd, write_fd = os.pipe()
        try:
            connection.start_transient(
                old["unit_name"], arguments=["/usr/bin/sleep", ".5"], executable="/usr/bin/sleep",
                cwd=Path("/"), environment={"PATH": "/usr/bin:/bin"},
                stdout_fd=write_fd, stderr_fd=write_fd,
                description=unit_description(old["run_id"], old["command_id"]),
            )
            os.close(write_fd)
            write_fd = -1
            snapshot, verification = verify_unit(connection, old)
            self.assertIsNotNone(snapshot)
            self.assertNotEqual(snapshot.invocation_id, old["invocation_id"])
            self.assertEqual(verification.status, VerificationStatus.MISMATCH)
        finally:
            try:
                connection.stop(old["unit_name"])
            except Exception:
                pass
            connection.close()
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_sequential_commands_and_multiple_runs_have_distinct_units(self):
        units = []
        for index in range(2):
            run = self.create_run(f"isolation-{index}")
            for _ in range(2):
                result, transitions, remaining = self.run_strong(run, "true")
                self.assertEqual(result["status"], "success")
                self.assertIsNone(remaining)
                units.append(transitions[0]["unit_name"])
        self.assertEqual(len(set(units)), 4)
        self.assertTrue(all(name.startswith("debbuilder-command-") for name in units))

    def test_unit_name_is_bounded_and_never_contains_the_raw_run_id(self):
        command_id = "a" * 32
        first = command_unit_name("valid-run+with.user-input", command_id)
        second = command_unit_name("valid-run+with.user-input", command_id)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 255)
        self.assertNotIn("valid-run", first)
        self.assertRegex(first, r"^debbuilder-command-[0-9a-f]{16}-[0-9a-f]{32}\.service$")


class ContainmentFallbackTests(unittest.TestCase):
    def test_completed_systemd_command_promotes_without_a_live_cgroup_snapshot(self):
        command_id = "a" * 32
        run_id = "completed-before-snapshot"
        unit_name = command_unit_name(run_id, command_id)
        snapshot = UnitSnapshot(
            unit_name=unit_name,
            description=unit_description(run_id, command_id),
            transient=True,
            invocation_id="b" * 32,
            control_group="",
            active_state="active",
            sub_state="exited",
            service_type="exec",
            exit_type="cgroup",
            kill_mode="control-group",
            result="success",
            main_pid=0,
            exec_main_code=1,
            exec_main_status=0,
        )

        class CompletedConnection:
            def __init__(self):
                self.stopped = False

            def start_transient(self, *_args, **_kwargs):
                return None

            def snapshot(self, requested_unit):
                if requested_unit != unit_name:
                    raise AssertionError(f"unexpected unit: {requested_unit}")
                return None if self.stopped else snapshot

            def stop(self, requested_unit):
                if requested_unit != unit_name:
                    raise AssertionError(f"unexpected unit: {requested_unit}")
                self.stopped = True

            def close(self):
                return None

        transitions = []
        cleared = []
        recorder = IdentityRecorder(
            run_id,
            transitions.append,
            lambda value: cleared.append(value) or True,
            lambda expected, active: transitions.append(active) or True,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(containment_module, "_SystemdConnection", CompletedConnection),
        ):
            command = SystemdCommandContainment.start(
                ["/usr/bin/true"],
                cwd=Path(temporary),
                environment={"PATH": "/usr/bin:/bin"},
                recorder=recorder,
                command_id=command_id,
            )
            try:
                self.assertEqual(command.poll(), 0)
                self.assertEqual(command.metadata["invocation_id"], snapshot.invocation_id)
                self.assertEqual(command.metadata["control_group"], expected_control_group(unit_name))
                finished = command.finish()
                self.assertTrue(finished.gone, finished.error)
                self.assertEqual(cleared, [command.metadata])
            finally:
                command.close()

    def test_cgroup_absence_permission_error_is_not_absence(self):
        unit = "debbuilder-command-" + "a" * 16 + "-" + "b" * 32 + ".service"
        with mock.patch("debbuilder.command_containment.os.stat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PermissionError, "denied"):
                containment_module._cgroup_is_absent(expected_control_group(unit))

    def test_unexpected_command_cgroup_type_fails_inventory_closed(self):
        unit = "debbuilder-command-" + "a" * 16 + "-" + "b" * 32 + ".service"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "system.slice").mkdir()
            (root / "system.slice" / unit).write_text("unexpected")
            with mock.patch.object(containment_module, "CGROUP_ROOT", root):
                with self.assertRaisesRegex(containment_module.ContainmentError, "unexpected filesystem type"):
                    containment_module.loaded_command_units(connection_factory=mock.Mock())

    def test_start_failure_before_dbus_attempt_clears_precommitted_intent(self):
        recorded = []
        cleared = []
        recorder = IdentityRecorder(
            "pre-dbus-failure", recorded.append, lambda value: cleared.append(value) or True,
            lambda _expected, _updated: True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                containment_module, "_SystemdConnection",
                side_effect=containment_module.ContainmentError("injected connection failure"),
            ):
                with self.assertRaisesRegex(containment_module.ContainmentError, "connection failure"):
                    SystemdCommandContainment.start(
                        ["/usr/bin/true"], cwd=Path(temporary),
                        environment={"PATH": "/usr/bin:/bin"}, recorder=recorder,
                        command_id="a" * 32,
                    )
        self.assertEqual(len(recorded), 1)
        self.assertEqual(cleared, recorded)

    def test_process_group_selector_setup_failure_reaps_spawned_child(self):
        real_selector = selectors.DefaultSelector
        real_popen = command_runner_module.subprocess.Popen
        spawned = []

        class BrokenSelector:
            def __init__(self):
                self.inner = real_selector()

            def register(self, *_args, **_kwargs):
                raise OSError("injected selector setup failure")

            def get_map(self):
                return self.inner.get_map()

            def select(self, wait):
                return self.inner.select(wait)

            def unregister(self, fileobj):
                return self.inner.unregister(fileobj)

            def close(self):
                self.inner.close()

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(command_runner_module.selectors, "DefaultSelector", BrokenSelector),
                mock.patch.object(command_runner_module.subprocess, "Popen", side_effect=popen),
            ):
                result = run_command("sleep 30", workspace=temporary)

        self.assertEqual(result["status"], "failed")
        self.assertIn("selector setup failure", result["stderr"])
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())

    def test_fast_fallback_completion_is_not_misreported_as_identity_failure(self):
        recorder = IdentityRecorder("fast-fallback", lambda _value: None, lambda _value: True, lambda _a, _b: True)
        unavailable = ContainmentCapability("process_group", False, "injected unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch("debbuilder.command_runner.current_recorder", return_value=recorder),
                mock.patch("debbuilder.command_runner.containment_capability", return_value=unavailable),
            ):
                for _index in range(25):
                    self.assertEqual(run_command("true", workspace=temporary)["status"], "success")

    def test_fallback_collects_result_when_process_disappears_before_stat_capture(self):
        recorder = IdentityRecorder("vanished-fallback", lambda _value: None, lambda _value: True)
        unavailable = ContainmentCapability("process_group", False, "injected unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch("debbuilder.command_runner.current_recorder", return_value=recorder),
                mock.patch("debbuilder.command_runner.containment_capability", return_value=unavailable),
                mock.patch(
                    "debbuilder.command_runner.capture_identity",
                    side_effect=ProcessLookupError(3, "process exited before stat capture"),
                ),
            ):
                result = run_command("true", workspace=temporary)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)

    def test_capability_failure_keeps_process_group_backend_explicit(self):
        recorded = []
        recorder = IdentityRecorder("fallback-run", recorded.append, lambda _value: True, lambda _a, _b: True)
        unavailable = ContainmentCapability("process_group", False, "injected unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch("debbuilder.command_runner.current_recorder", return_value=recorder),
                mock.patch("debbuilder.command_runner.containment_capability", return_value=unavailable),
            ):
                result = run_command("sleep 0.1", workspace=temporary)
        self.assertEqual(result["status"], "success")
        self.assertEqual(recorded[0]["backend"], "process_group")
        self.assertEqual(recorded[0]["containment_state"], "active")

    def test_capability_detection_fails_closed_when_probe_fails(self):
        with mock.patch.object(containment_module, "_cgroup2_is_unified", return_value=False):
            capability = containment_module.containment_capability(refresh=True)
        self.assertFalse(capability.available)
        self.assertEqual(capability.backend, "process_group")


if __name__ == "__main__":
    unittest.main()
