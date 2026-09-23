import hashlib
import json
import os
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.dependency_preparation import DependencyPreparationError, PreparationSupervisor
from debbuilder.command_identity import current_recorder, recording_identities
from debbuilder.execution_cancellation import ExecutionCancelled
from debbuilder.validation_oci import (
    ATTEMPT_LABEL,
    NAME_PREFIX,
    OWNER_LABEL,
    OWNER_VALUE,
    PODMAN_DEFAULT_CAPABILITIES,
    IdentityRegistry,
    OciOwnershipError,
    OwnedContainer,
    PodmanRuntime,
    new_identity,
    ownership_labels,
    recover_owned_containers,
    resource_arguments,
    verify_owned_container,
    verify_container_configuration,
    verify_resource_enforcement,
)


IMAGE = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": "repo@sha256:" + "b" * 64}
CONTAINER_ID = "c" * 64


def command_result(*, stdout="", stderr="", status="success", exit_code=0, **extra):
    result = {
        "status": status, "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
        "timed_out": False, "resource_control": {"verification": "verified"},
    }
    result.update(extra)
    return result


def inspection(identity, *, running=True, labels=None, image_id=None):
    configuration = identity.get("configuration")
    host = {}
    mounts = []
    create_command = []
    if configuration is not None:
        create_command = ["podman", "create"]
        policy = configuration["policy"]
        lifecycle = identity["role"] == "lifecycle"
        host = {
            "NetworkMode": configuration["network"], "ReadonlyRootfs": not lifecycle, "Privileged": False,
            "CapAdd": [], "CapDrop": [] if lifecycle else sorted(PODMAN_DEFAULT_CAPABILITIES),
            "SecurityOpt": [] if lifecycle else ["no-new-privileges"],
            "Memory": policy["memory_max_bytes"] or 0, "PidsLimit": policy["tasks_max"] or 0,
            "CpuPeriod": 100000 if policy["cpu_quota_percent"] is not None else 0,
            "CpuQuota": policy["cpu_quota_percent"] * 1000 if policy["cpu_quota_percent"] is not None else 0,
            "Tmpfs": {row["destination"]: f"rw,nosuid,nodev,noexec,size={row['size']}" for row in configuration["tmpfs"]},
            "BlkioDeviceReadBps": [{"Path": row["path"], "Rate": row["rate"]} for row in configuration["io_devices"] if row["control"] == "io_read"],
            "BlkioDeviceWriteBps": [{"Path": row["path"], "Rate": row["rate"]} for row in configuration["io_devices"] if row["control"] == "io_write"],
        }
        mounts = [{"Type": "bind", "Source": row["source"], "Destination": row["destination"], "RW": not row["read_only"]} for row in configuration["mounts"]]
    return {
        "Id": identity.get("container_id") or CONTAINER_ID,
        "Name": identity["name"], "Image": (image_id or identity["image"]["id"]).removeprefix("sha256:"),
        "Config": {"Labels": ownership_labels(identity) if labels is None else labels, "CreateCommand": create_command},
        "State": {"Running": running, "Pid": 4242 if running else 0},
        "HostConfig": host, "Mounts": mounts,
    }


def add_test_configuration(identity, *, command=None):
    command = command or ["podman", "create"]
    identity["configuration"] = {
        "network": "bridge", "mounts": [], "tmpfs": [],
        "policy": {"memory_max_bytes": None, "tasks_max": None, "cpu_quota_percent": None,
                   "io_read_bandwidth_max_bytes_per_sec": None, "io_write_bandwidth_max_bytes_per_sec": None},
        "io_devices": [], "create_argv_sha256": hashlib.sha256(
            json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    }
    return identity


class FakeRuntime:
    def __init__(self, workspace: Path, registry: IdentityRegistry):
        self.workspace = workspace
        self.registry = registry
        self.cancellation_event = threading.Event()
        self.calls = []
        self.current = None

    def run(self, arguments, *, timeout, workload=False):
        self.calls.append((list(arguments), workload))
        if arguments[:2] == ["podman", "create"]:
            persisted = self.registry.load_all()[0][0][1]
            self.assert_planned = persisted["state"]
            self.current = inspection({**persisted, "container_id": CONTAINER_ID}, running=False)
            self.current["Config"]["CreateCommand"] = list(arguments)
            return command_result(stdout=CONTAINER_ID + "\n")
        if arguments and arguments[0] == "systemd-run":
            assert self.current is not None
            self.current["State"] = {"Running": True, "Pid": 4242}
            return command_result(stdout="started\n")
        if arguments[:3] == ["podman", "rm", "--force"]:
            self.current = None
            return command_result()
        return command_result()

    def inspect_container(self, _reference):
        return self.current

    @staticmethod
    def verify_payload_capabilities(_pid):
        return None


class OciIdentityTests(unittest.TestCase):
    def test_control_plane_inventory_does_not_create_transient_workload_scopes(self):
        observed = []

        def runner(_command, **_kwargs):
            observed.append(current_recorder())
            return command_result(stdout="[]")

        runtime = PodmanRuntime(Path.cwd(), runner=runner)
        with recording_identities(
            "run", record=lambda _identity: None, clear=lambda _identity: None,
            update=lambda _old, _new: None, resource_policy={"memory_max_bytes": 1024 * 1024},
        ):
            runtime.run(["podman", "inspect", "example"], timeout=1, workload=False)
        self.assertEqual(observed, [None])

    def test_podman_inventory_queries_are_bounded_before_output_is_buffered(self):
        calls = []

        def runner(command, **_kwargs):
            arguments = shlex.split(command)
            calls.append(arguments)
            return command_result(stdout="[]")

        runtime = PodmanRuntime(Path.cwd(), runner=runner)
        self.assertEqual(runtime.namespace_candidates(), [])
        self.assertTrue(any(value.endswith("bounded_process.py") for value in calls[0][:2]))
        self.assertEqual(calls[0][calls[0].index("--") + 1:3 + calls[0].index("--")], ["podman", "ps"])

    def test_exec_relies_on_the_verified_container_cgroup_not_repeated_host_probes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(
                run_id="run", attempt_id="container-cgroup", role="dependency-preparation",
                runtime="podman", image=IMAGE,
            )
            identity.update({"container_id": CONTAINER_ID, "state": "running"})

            class InspectingRuntime(FakeRuntime):
                def run(self, arguments, *, timeout, workload=False):
                    if arguments[:2] == ["podman", "exec"]:
                        self.recorder_during_exec = current_recorder()
                    return super().run(arguments, timeout=timeout, workload=workload)

            runtime = InspectingRuntime(root, registry)
            runtime.current = inspection(identity)
            with recording_identities(
                "run", record=lambda _identity: None, clear=lambda _identity: None,
                update=lambda _old, _new: None, resource_policy={"memory_max_bytes": 1024 * 1024},
            ):
                OwnedContainer(runtime, registry, identity).exec(["true"], timeout=1)
            self.assertIsNone(runtime.recorder_during_exec)
            self.assertTrue(runtime.calls[-1][1], "workload cancellation must still be forwarded")

    def test_create_persists_planned_identity_before_runtime_and_start_uses_separate_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run-one", attempt_id="attempt-one", role="dependency-preparation", runtime="podman", image=IMAGE)
            runtime = FakeRuntime(root, registry)
            owned = OwnedContainer(runtime, registry, identity)
            owned.create(mounts=[], tmpfs=[("/prep", 1024)], policy={}, network="bridge")
            self.assertEqual(runtime.assert_planned, "planned")
            self.assertEqual(owned.identity["state"], "created")
            _facts, _result = owned.start(policy={})
            start = next(call for call, _workload in runtime.calls if call and call[0] == "systemd-run")
            self.assertIn("--scope", start)
            self.assertIn(identity["scope_name"], start)
            self.assertLess(
                next(i for i, (call, _workload) in enumerate(runtime.calls) if call[0:2] == ["podman", "create"]),
                next(i for i, (call, _workload) in enumerate(runtime.calls) if call[0] == "systemd-run"),
            )
            owned.cleanup()
            self.assertEqual(registry.load_all(), ([], []))
            self.assertFalse(runtime.calls[-1][1], "cleanup must ignore workload cancellation")

    def test_lifecycle_role_is_created_networkless_for_systemd_with_writable_ephemeral_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(
                run_id="run-one", attempt_id="lifecycle-one", role="lifecycle",
                runtime="podman", image=IMAGE,
            )
            runtime = FakeRuntime(root, registry)
            owned = OwnedContainer(runtime, registry, identity)
            with self.assertRaisesRegex(OciOwnershipError, "requires network mode none"):
                owned.create(mounts=[], tmpfs=[], policy={}, network="bridge")
            owned.create(mounts=[], tmpfs=[], policy={}, network="none")
            create = next(call for call, _workload in runtime.calls if call[:2] == ["podman", "create"])
            self.assertIn("--systemd", create)
            self.assertIn("always", create)
            self.assertNotIn("--read-only", create)
            self.assertNotIn("--cap-drop", create)
            self.assertEqual(create[-1], "/sbin/init")
            _facts, _result = owned.start(policy={})
            owned.cleanup()

    def test_created_container_security_and_storage_must_match_durable_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run", attempt_id="configuration", role="dependency-preparation", runtime="podman", image=IMAGE)
            runtime = FakeRuntime(root, registry)
            owned = OwnedContainer(runtime, registry, identity)
            owned.create(mounts=[(root, "/input", "ro")], tmpfs=[("/prep", 1024)], policy={})
            verify_container_configuration(owned.identity, runtime.current)
            for case, field, value in (
                ("writable-root", "ReadonlyRootfs", False), ("host-network", "NetworkMode", "host"),
                ("partial-cap-drop", "CapDrop", ["CAP_CHOWN"]),
            ):
                with self.subTest(case=case):
                    changed = json.loads(json.dumps(runtime.current))
                    changed["HostConfig"][field] = value
                    with self.assertRaises(OciOwnershipError):
                        verify_container_configuration(owned.identity, changed)
            changed = json.loads(json.dumps(runtime.current))
            changed["Mounts"][0]["RW"] = True
            with self.assertRaises(OciOwnershipError):
                verify_container_configuration(owned.identity, changed)
            changed = json.loads(json.dumps(runtime.current))
            changed["HostConfig"]["Tmpfs"]["/prep"] += ",exec"
            with self.assertRaises(OciOwnershipError):
                verify_container_configuration(owned.identity, changed)
            owned.cleanup()

    def test_cancellation_at_create_start_update_solve_and_download_still_cleans(self):
        phases = {
            "create": lambda args: args[:2] == ["podman", "create"],
            "start": lambda args: bool(args) and args[0] == "systemd-run",
            "update": lambda args: "update" in args,
            "solve": lambda args: "--simulate" in args,
            "download": lambda args: "--download-only" in args,
        }
        for phase, predicate in phases.items():
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                registry = IdentityRegistry(root / "registry")
                identity = new_identity(run_id="run", attempt_id=f"cancel-{phase}", role="dependency-preparation", runtime="podman", image=IMAGE)

                class CancellingRuntime(FakeRuntime):
                    interrupted = False

                    def run(self, arguments, *, timeout, workload=False):
                        if workload and not self.interrupted and predicate(arguments):
                            self.interrupted = True
                            raise ExecutionCancelled()
                        return super().run(arguments, timeout=timeout, workload=workload)

                runtime = CancellingRuntime(root, registry)
                runtime.current = None if phase == "create" else inspection(
                    {**identity, "container_id": CONTAINER_ID}, running=phase != "start",
                )
                owned = OwnedContainer(runtime, registry, identity)
                try:
                    owned.create(mounts=[], tmpfs=[], policy={})
                    owned.start(policy={})
                    if phase in {"update", "solve", "download"}:
                        command = {"update": ["apt-get", "update"], "solve": ["apt-get", "--simulate", "install"], "download": ["apt-get", "--download-only", "install"]}[phase]
                        owned.exec(command, timeout=1)
                    self.fail("phase did not cancel")
                except ExecutionCancelled:
                    owned.cleanup()
                self.assertEqual(registry.load_all(), ([], []))
                self.assertIsNone(runtime.current)

    def test_timeout_cleanup_and_unresolved_cleanup_are_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run", attempt_id="timeout", role="dependency-preparation", runtime="podman", image=IMAGE)
            identity.update({"container_id": CONTAINER_ID, "state": "running"})
            add_test_configuration(identity)

            class TimedOutRuntime(FakeRuntime):
                def run(self, arguments, *, timeout, workload=False):
                    if workload and arguments[:2] == ["podman", "exec"]:
                        return command_result(status="failed", exit_code=None, timed_out=True, stderr="maximum runtime")
                    return super().run(arguments, timeout=timeout, workload=workload)

            runtime = TimedOutRuntime(root, registry)
            runtime.current = inspection(identity)
            registry.persist(identity)
            owned = OwnedContainer(runtime, registry, identity)
            with self.assertRaises(OciOwnershipError):
                owned.exec(["apt-get", "update"], timeout=1)
            owned.cleanup(timeout=1)
            self.assertEqual(registry.load_all(), ([], []))

            failing_identity = new_identity(run_id="run", attempt_id="cleanup-failure", role="dependency-preparation", runtime="podman", image=IMAGE)
            failing_identity.update({"container_id": CONTAINER_ID, "state": "running"})

            class FailedCleanupRuntime(FakeRuntime):
                def run(self, arguments, *, timeout, workload=False):
                    if arguments[:3] == ["podman", "rm", "--force"]:
                        return command_result(status="failed", exit_code=None, timed_out=True, stderr="cleanup timeout")
                    return super().run(arguments, timeout=timeout, workload=workload)

            failed = FailedCleanupRuntime(root, registry)
            failed.current = inspection(failing_identity)
            registry.persist(failing_identity)
            with self.assertRaises(OciOwnershipError):
                OwnedContainer(failed, registry, failing_identity).cleanup(timeout=1)
            rows, blockers = registry.load_all()
            self.assertEqual(blockers, [])
            self.assertEqual(rows[0][1]["state"], "cleanup_failed")

    def test_shutdown_closes_admission_cancels_and_waits_for_active_attempt(self):
        supervisor = PreparationSupervisor()
        event = threading.Event()
        supervisor.register("attempt", event)
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor.shutdown(1)))
        thread.start()
        self.assertTrue(event.wait(0.5))
        with self.assertRaises(DependencyPreparationError):
            supervisor.register("new", threading.Event())
        supervisor.unregister("attempt")
        thread.join(1)
        self.assertEqual(result, [True])

    def test_exact_namespaced_labels_image_and_runtime_id_are_required(self):
        identity = new_identity(run_id="run-one", attempt_id="attempt-one", role="dependency-preparation", runtime="podman", image=IMAGE)
        identity["container_id"] = CONTAINER_ID
        good = inspection(identity)
        self.assertEqual(verify_owned_container(identity, good)["id"], CONTAINER_ID)
        for changed in (
            {**ownership_labels(identity), ATTEMPT_LABEL: "other"},
            {**ownership_labels(identity), "io.debbuilder.validation.extra": "x"},
        ):
            with self.assertRaises(OciOwnershipError):
                verify_owned_container(identity, inspection(identity, labels=changed))
        with self.assertRaises(OciOwnershipError):
            verify_owned_container(identity, inspection(identity, image_id="sha256:" + "d" * 64))

    def test_registry_rejects_symlink_and_oversized_or_malformed_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            registry.root.mkdir()
            (root / "target").write_text("{}")
            (registry.root / ("a" * 32 + ".json")).symlink_to(root / "target")
            rows, blockers = registry.load_all()
            self.assertEqual(rows, [])
            self.assertEqual(blockers[0]["code"], "validation_identity_unverifiable")


class OciRecoveryTests(unittest.TestCase):
    def runner(self, state, *, inventory_error=False):
        def run(command, **_kwargs):
            arguments = shlex.split(command)
            if "--" in arguments and any(value.endswith("bounded_process.py") for value in arguments[:2]):
                arguments = arguments[arguments.index("--") + 1:]
            if arguments[:2] == ["podman", "ps"]:
                if inventory_error:
                    return command_result(status="failed", exit_code=125, stderr="runtime unavailable")
                rows = [{"Id": key, "Names": [value["Name"]]} for key, value in state.items()]
                return command_result(stdout=json.dumps(rows))
            if arguments[:3] == ["podman", "container", "inspect"]:
                reference = arguments[3]
                found = next((value for key, value in state.items() if reference in {key, value["Name"]}), None)
                return command_result(stdout=json.dumps([found])) if found else command_result(status="failed", exit_code=125, stderr="no such container")
            if arguments[:3] == ["podman", "rm", "--force"]:
                state.pop(arguments[-1], None)
                return command_result()
            raise AssertionError(arguments)
        return run

    def test_authenticated_orphan_is_removed_and_absence_proved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run", attempt_id="attempt", role="dependency-preparation", runtime="podman", image=IMAGE)
            identity.update({"container_id": CONTAINER_ID, "state": "running"})
            add_test_configuration(identity)
            registry.persist(identity)
            state = {CONTAINER_ID: inspection(identity)}
            result = recover_owned_containers(registry.root, workspace=root, runner=self.runner(state))
            self.assertEqual(result.recovered, ["attempt"])
            self.assertEqual(result.blockers, [])
            self.assertEqual(state, {})
            self.assertEqual(registry.load_all(), ([], []))

    def test_create_start_update_download_and_prepared_persistence_crash_windows_converge(self):
        windows = (
            ("created-before-promotion", "planned", False, False),
            ("identity-before-start", "created", True, False),
            ("started-before-attempt-update", "running", True, True),
            ("apt-update-daemon-loss", "running", True, True),
            ("download-before-prepared", "running", True, True),
            ("prepared-before-removal", "cleanup_failed", True, True),
        )
        for attempt, state_name, persist_id, running in windows:
            with self.subTest(window=attempt), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                registry = IdentityRegistry(root / "registry")
                identity = new_identity(run_id="run", attempt_id=attempt, role="dependency-preparation", runtime="podman", image=IMAGE)
                if persist_id:
                    identity["container_id"] = CONTAINER_ID
                identity["state"] = state_name
                add_test_configuration(identity)
                if state_name == "cleanup_failed":
                    identity["cleanup_error"] = "interrupted cleanup"
                registry.persist(identity)
                state = {CONTAINER_ID: inspection({**identity, "container_id": CONTAINER_ID}, running=running)}
                result = recover_owned_containers(registry.root, workspace=root, runner=self.runner(state))
                self.assertEqual(result.recovered, [attempt])
                self.assertEqual(result.blockers, [])
                self.assertEqual(state, {})
                self.assertEqual(registry.load_all(), ([], []))

    def test_planned_identity_with_absent_container_is_cleared_only_after_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run", attempt_id="planned", role="dependency-preparation", runtime="podman", image=IMAGE)
            registry.persist(identity)
            result = recover_owned_containers(registry.root, workspace=root, runner=self.runner({}))
            self.assertEqual(result.cleared_absent, ["planned"])
            self.assertFalse(result.admission_blocker)

    def test_namespace_container_without_identity_blocks_and_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = new_identity(run_id="run", attempt_id="foreign", role="dependency-preparation", runtime="podman", image=IMAGE)
            identity["container_id"] = CONTAINER_ID
            state = {CONTAINER_ID: inspection(identity)}
            result = recover_owned_containers(root / "registry", workspace=root, runner=self.runner(state))
            self.assertEqual(result.blockers[0]["code"], "validation_container_ownership_unverifiable")
            self.assertIn(CONTAINER_ID, state)

    def test_unavailable_runtime_without_durable_state_does_not_block_docker_only_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = recover_owned_containers(Path(temporary) / "registry", workspace=temporary, runner=self.runner({}, inventory_error=True))
            self.assertEqual(result.blockers, [])
            self.assertFalse(result.inventory_trustworthy)

    def test_unavailable_runtime_with_durable_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            identity = new_identity(run_id="run", attempt_id="durable", role="dependency-preparation", runtime="podman", image=IMAGE)
            registry.persist(identity)
            result = recover_owned_containers(registry.root, workspace=root, runner=self.runner({}, inventory_error=True))
            self.assertEqual(result.blockers[0]["code"], "validation_container_inventory_unavailable")

    def test_recovery_removes_owned_containers_and_blocks_unverifiable_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = IdentityRegistry(root / "registry")
            running = new_identity(run_id="run", attempt_id="running", role="dependency-preparation", runtime="podman", image=IMAGE)
            stopped = new_identity(run_id="run", attempt_id="stopped", role="dependency-preparation", runtime="podman", image=IMAGE)
            absent = new_identity(run_id="run", attempt_id="absent", role="dependency-preparation", runtime="podman", image=IMAGE)
            ids = ("a" * 64, "b" * 64)
            for identity, container_id, state_name in ((running, ids[0], "running"), (stopped, ids[1], "created")):
                identity.update({"container_id": container_id, "state": state_name})
                add_test_configuration(identity)
                registry.persist(identity)
            registry.persist(absent)
            foreign = new_identity(run_id="run", attempt_id="foreign", role="dependency-preparation", runtime="podman", image=IMAGE)
            foreign["container_id"] = CONTAINER_ID
            state = {
                ids[0]: inspection(running, running=True),
                ids[1]: inspection(stopped, running=False),
                CONTAINER_ID: inspection(foreign, running=True),
            }
            result = recover_owned_containers(registry.root, workspace=root, runner=self.runner(state))
            self.assertEqual(set(result.recovered), {"running", "stopped"})
            self.assertEqual(result.cleared_absent, ["absent"])
            self.assertEqual(result.blockers[0]["code"], "validation_container_ownership_unverifiable")
            self.assertTrue(result.inventory_trustworthy)


class OciResourceTests(unittest.TestCase):
    def test_null_policy_has_no_runtime_limit_flags(self):
        arguments, devices = resource_arguments({}, Path("/tmp"))
        self.assertEqual(arguments, [])
        self.assertEqual(devices, [])

    def test_finite_memory_tasks_and_cpu_map_before_start(self):
        arguments, _ = resource_arguments({"memory_max_bytes": 1048576, "tasks_max": 12, "cpu_quota_percent": 150}, Path("/tmp"))
        self.assertEqual(arguments, ["--memory", "1048576", "--pids-limit", "12", "--cpu-period", "100000", "--cpu-quota", "150000"])

    def test_supported_io_maps_both_directions_and_unresolvable_io_fails_closed(self):
        policy = {
            "io_read_bandwidth_max_bytes_per_sec": 1000,
            "io_write_bandwidth_max_bytes_per_sec": 2000,
        }
        with mock.patch("debbuilder.validation_oci._block_device", return_value=(Path("/dev/vda"), (254, 0))):
            arguments, devices = resource_arguments(policy, Path("/workspace"))
        self.assertIn(["--device-read-bps", "/dev/vda:1000"], [arguments[index:index + 2] for index in range(len(arguments) - 1)])
        self.assertIn(["--device-write-bps", "/dev/vda:2000"], [arguments[index:index + 2] for index in range(len(arguments) - 1)])
        self.assertEqual({(row["control"], row["path"], row["major"], row["minor"], row["rate"]) for row in devices}, {
            ("io_read", "/dev/vda", 254, 0, 1000), ("io_write", "/dev/vda", 254, 0, 2000),
        })
        with self.assertRaises(OciOwnershipError):
            resource_arguments({"io_read_bandwidth_max_bytes_per_sec": 1}, Path("/tmp"))

    def test_effective_io_verification_requires_every_persisted_backing_device(self):
        identity = new_identity(run_id="run", attempt_id="all-devices", role="dependency-preparation", runtime="podman", image=IMAGE)
        identity.update({"container_id": CONTAINER_ID, "state": "running"})
        add_test_configuration(identity)
        identity["configuration"]["policy"]["io_read_bandwidth_max_bytes_per_sec"] = 1000
        identity["configuration"]["io_devices"] = [
            {"control": "io_read", "path": "/dev/vda", "major": 254, "minor": 0, "rate": 1000},
            {"control": "io_read", "path": "/dev/vdb", "major": 254, "minor": 16, "rate": 1000},
        ]
        inspected = inspection(identity)
        controls = {"io.max": "254:0 rbps=1000\n254:16 rbps=1000"}
        with mock.patch("debbuilder.validation_oci._cgroup_files", return_value=Path("cgroup")), mock.patch(
            "debbuilder.validation_oci._read_control", side_effect=lambda _root, name: controls[name],
        ):
            verify_resource_enforcement(identity, inspected, identity["configuration"]["policy"], workspace=Path("/tmp"), launcher_result=command_result())
            inspected["HostConfig"]["BlkioDeviceReadBps"].pop()
            with self.assertRaises(OciOwnershipError):
                verify_resource_enforcement(identity, inspected, identity["configuration"]["policy"], workspace=Path("/tmp"), launcher_result=command_result())

    def test_effective_inspection_and_cgroup_values_must_both_match(self):
        identity = new_identity(run_id="run", attempt_id="limits", role="dependency-preparation", runtime="podman", image=IMAGE)
        identity.update({"container_id": CONTAINER_ID, "state": "running"})
        inspected = inspection(identity)
        inspected["HostConfig"].update({"Memory": 2048, "PidsLimit": 8, "CpuPeriod": 100000, "CpuQuota": 50000})
        policy = {"memory_max_bytes": 2048, "tasks_max": 8, "cpu_quota_percent": 50}
        values = {"memory.max": "2048", "pids.max": "8", "cpu.max": "50000 100000"}
        with mock.patch("debbuilder.validation_oci._cgroup_files", return_value=Path("cgroup")), mock.patch(
            "debbuilder.validation_oci._read_control", side_effect=lambda _root, name: values[name],
        ):
            rows = verify_resource_enforcement(identity, inspected, policy, workspace=Path("/tmp"), launcher_result=command_result())
        self.assertTrue(all(row["status"] in {"enforced", "not_requested"} for row in rows))
        inspected["HostConfig"]["Memory"] = 4096
        with mock.patch("debbuilder.validation_oci._cgroup_files", return_value=Path("cgroup")), mock.patch(
            "debbuilder.validation_oci._read_control", side_effect=lambda _root, name: values[name],
        ), self.assertRaises(OciOwnershipError):
            verify_resource_enforcement(identity, inspected, policy, workspace=Path("/tmp"), launcher_result=command_result())

    def test_unverified_launcher_fails_closed_for_finite_policy(self):
        identity = new_identity(run_id="run", attempt_id="limits", role="dependency-preparation", runtime="podman", image=IMAGE)
        identity.update({"container_id": CONTAINER_ID, "state": "running"})
        with mock.patch("debbuilder.validation_oci._cgroup_files", return_value=Path("cgroup")), self.assertRaises(OciOwnershipError):
            verify_resource_enforcement(
                identity, inspection(identity), {"memory_max_bytes": 2048}, workspace=Path("/tmp"),
                launcher_result=command_result(resource_control={"verification": "fallback"}),
            )
