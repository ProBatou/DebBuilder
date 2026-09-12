import unittest
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import debbuilder.command_containment as containment
from debbuilder.resource_limits import (
    DBUS_UINT64_MAX,
    JS_SAFE_INTEGER,
    ResourceLimitError,
    admission_contract,
    cpu_quota_usec,
    empty_policy,
    io_target_paths,
    normalize_policy,
    requested_controls,
    resolve_policy,
    validate_contract,
    validate_host_enforceability,
)
from debbuilder.command_containment import (
    ContainmentCapability,
    UnitSnapshot,
    _SystemdConnection,
    _snapshot_matches_resources,
    resource_limit_capability,
)
from debbuilder.command_identity import VerificationStatus


class ResourceLimitPolicyTests(unittest.TestCase):
    def test_all_null_policy_is_canonical_and_round_trips(self):
        self.assertEqual(normalize_policy(None), empty_policy())
        self.assertEqual(normalize_policy({}), empty_policy())
        self.assertEqual(normalize_policy(empty_policy()), empty_policy())
        self.assertEqual(requested_controls(empty_policy()), [])

    def test_finite_fields_require_positive_exact_integers(self):
        for value in (True, False, 1.0, "1", 0, -1):
            with self.subTest(value=value), self.assertRaises(ResourceLimitError) as raised:
                normalize_policy({"tasks_max": value})
            self.assertEqual(raised.exception.path, "$.resource_limits.tasks_max")
        with self.assertRaises(ResourceLimitError) as raised:
            normalize_policy({"future_limit": 1})
        self.assertEqual(raised.exception.code, "unknown_resource_limit")
        self.assertEqual(raised.exception.path, "$.resource_limits.future_limit")

    def test_js_safe_boundary_and_cpu_mapping(self):
        self.assertEqual(normalize_policy({"memory_max_bytes": JS_SAFE_INTEGER})["memory_max_bytes"], JS_SAFE_INTEGER)
        with self.assertRaises(ResourceLimitError):
            normalize_policy({"memory_max_bytes": JS_SAFE_INTEGER + 1})
        self.assertEqual(cpu_quota_usec(250), 2_500_000)
        self.assertEqual(normalize_policy({"cpu_quota_percent": 101})["cpu_quota_percent"], 101)

    def test_sub_page_memory_limit_is_rejected_before_systemd_creation(self):
        with mock.patch("debbuilder.resource_limits.os.sysconf", return_value=4096):
            with self.assertRaises(ResourceLimitError) as raised:
                validate_host_enforceability({"memory_max_bytes": 1})
        self.assertEqual(raised.exception.code, "resource_limits_unavailable")
        self.assertEqual(raised.exception.details["minimum_enforceable_bytes"], 4096)

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "debbuilder.command_containment.containment_capability",
            return_value=ContainmentCapability("systemd_cgroup", True, "verified"),
        ), mock.patch(
            "debbuilder.resource_limits.os.sysconf", return_value=4096,
        ), mock.patch(
            "debbuilder.command_containment._SystemdConnection",
        ) as connection:
            capability = resource_limit_capability(
                {"memory_max_bytes": 1}, workspace=temporary, refresh=True,
            )
        self.assertFalse(capability["available"])
        self.assertIn("page-size", capability["reason"])
        connection.assert_not_called()

        for invalid_page_size in (0, -1):
            with self.subTest(page_size=invalid_page_size), mock.patch(
                "debbuilder.resource_limits.os.sysconf", return_value=invalid_page_size,
            ), self.assertRaises(ResourceLimitError):
                validate_host_enforceability({"memory_max_bytes": 4096})

    def test_memory_safety_properties_must_be_explicitly_observable(self):
        metadata = {"resource_limits": {"memory_max_bytes": 4096}}
        baseline = UnitSnapshot(
            unit_name="example.service", description="example", transient=True,
            invocation_id="a" * 32, control_group="/system.slice/example.service",
            active_state="active", sub_state="running", service_type="exec",
            exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=1, exec_main_code=0, exec_main_status=0,
            memory_max=4096, startup_memory_max=DBUS_UINT64_MAX,
            effective_memory_max=4096, oom_policy="stop", oom_score_adjust=0,
            memory_accounting=True,
        )
        self.assertEqual(
            _snapshot_matches_resources(baseline, metadata).status,
            VerificationStatus.MATCH,
        )
        for missing in (
            {**baseline.__dict__, "startup_memory_max": None},
            {**baseline.__dict__, "oom_score_adjust": None},
        ):
            with self.subTest(missing=missing):
                checked = _snapshot_matches_resources(UnitSnapshot(**missing), metadata)
                self.assertEqual(checked.status, VerificationStatus.MISMATCH)

    def test_global_and_recipe_resolve_to_strictest_with_origins(self):
        effective, origins = resolve_policy(
            {"memory_max_bytes": 100, "tasks_max": 10, "cpu_quota_percent": 200},
            {"memory_max_bytes": 200, "tasks_max": 5, "io_write_bandwidth_max_bytes_per_sec": 50},
        )
        self.assertEqual(effective, {
            "memory_max_bytes": 100,
            "tasks_max": 5,
            "cpu_quota_percent": 200,
            "io_read_bandwidth_max_bytes_per_sec": None,
            "io_write_bandwidth_max_bytes_per_sec": 50,
        })
        self.assertEqual(origins, {
            "memory_max_bytes": "global",
            "tasks_max": "recipe",
            "cpu_quota_percent": "global",
            "io_read_bandwidth_max_bytes_per_sec": "unset",
            "io_write_bandwidth_max_bytes_per_sec": "recipe",
        })

    def test_admission_contract_is_self_consistent_and_tamper_evident(self):
        capability = {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": ["memory"], "reason": "verified",
        }
        contract = admission_contract({"memory_max_bytes": 100}, {}, capability)
        self.assertEqual(validate_contract(contract), contract)
        contract["effective"]["memory_max_bytes"] = 200
        with self.assertRaises(ResourceLimitError):
            validate_contract(contract)

    def test_finite_contract_rejects_non_systemd_admission_backend(self):
        contract = admission_contract({"memory_max_bytes": 100}, {}, {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": ["memory"], "reason": "verified",
        })
        for backend in ("process_group", "historical", "not_evaluated"):
            with self.subTest(backend=backend):
                tampered = {**contract, "admission_capability": {**contract["admission_capability"], "backend": backend}}
                with self.assertRaises(ResourceLimitError):
                    validate_contract(tampered)

    def test_run_contract_requires_canonical_policy_objects(self):
        contract = admission_contract({}, {}, {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": [], "reason": "verified",
        })
        for field in ("global", "recipe", "effective"):
            with self.subTest(field=field):
                tampered = {**contract, field: None}
                with self.assertRaises(ResourceLimitError):
                    validate_contract(tampered)

    def test_io_targets_coalesce_same_device_in_root_then_workspace_order(self):
        workspace = Path("/tmp/workspace").resolve()
        with mock.patch.object(Path, "is_dir", return_value=True), mock.patch(
            "debbuilder.resource_limits.os.stat",
            side_effect=[SimpleNamespace(st_dev=7), SimpleNamespace(st_dev=7)],
        ):
            self.assertEqual(io_target_paths(workspace), ["/"])
        with mock.patch.object(Path, "is_dir", return_value=True), mock.patch(
            "debbuilder.resource_limits.os.stat",
            side_effect=[SimpleNamespace(st_dev=7), SimpleNamespace(st_dev=9)],
        ):
            self.assertEqual(io_target_paths(workspace), ["/", str(workspace)])

    def test_io_resolution_failure_becomes_unavailable_capability(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "debbuilder.command_containment.containment_capability",
            return_value=ContainmentCapability("systemd_cgroup", True, "verified"),
        ), mock.patch(
            "debbuilder.command_containment.io_target_paths",
            side_effect=ResourceLimitError(
                "resource_limits_unavailable", "injected target failure",
                details={"control": "io"},
            ),
        ):
            capability = resource_limit_capability(
                {"io_write_bandwidth_max_bytes_per_sec": 1024},
                workspace=temporary,
                refresh=True,
            )
        self.assertFalse(capability["available"])
        self.assertEqual(capability["backend"], "systemd_cgroup")
        self.assertEqual(capability["requested_controls"], ["io_write"])
        self.assertIn("target failure", capability["reason"])

    def test_systemd_properties_are_atomic_typed_complete_and_deterministic(self):
        class Manager:
            def __init__(self):
                self.calls = []

            def StartTransientUnit(self, *args):
                self.calls.append(args)

        import dbus
        manager = Manager()
        connection = object.__new__(_SystemdConnection)
        connection.manager = manager
        policy = {
            "memory_max_bytes": 512 * 1024 * 1024,
            "tasks_max": 128,
            "cpu_quota_percent": 250,
            "io_read_bandwidth_max_bytes_per_sec": 4 * 1024 * 1024,
            "io_write_bandwidth_max_bytes_per_sec": 2 * 1024 * 1024,
        }
        with tempfile.TemporaryDirectory() as temporary:
            read_fd, write_fd = os.pipe()
            try:
                connection.start_transient(
                    "example.service",
                    arguments=["/usr/bin/true"],
                    executable="/usr/bin/true",
                    cwd=Path(temporary),
                    environment={"PATH": "/usr/bin:/bin"},
                    stdout_fd=write_fd,
                    stderr_fd=write_fd,
                    description="resource test",
                    resource_policy=policy,
                    resource_workspace=Path(temporary),
                )
            finally:
                os.close(read_fd)
                os.close(write_fd)
        properties = dict(manager.calls[0][2])
        self.assertIsInstance(properties["MemoryMax"], dbus.UInt64)
        self.assertIsInstance(properties["StartupMemoryMax"], dbus.UInt64)
        self.assertEqual(int(properties["StartupMemoryMax"]), DBUS_UINT64_MAX)
        self.assertIsInstance(properties["TasksMax"], dbus.UInt64)
        self.assertEqual(int(properties["CPUQuotaPerSecUSec"]), 2_500_000)
        self.assertEqual(bool(properties["MemoryAccounting"]), True)
        self.assertEqual(bool(properties["TasksAccounting"]), True)
        self.assertEqual(bool(properties["CPUAccounting"]), True)
        self.assertEqual(bool(properties["IOAccounting"]), True)
        self.assertIn("ExecStart", properties)
        self.assertEqual([name for name, _value in manager.calls[0][2]][-12:], [
            "MemoryAccounting", "MemoryMax", "StartupMemoryMax", "OOMPolicy", "OOMScoreAdjust",
            "TasksAccounting", "TasksMax", "CPUAccounting", "CPUQuotaPerSecUSec",
            "IOAccounting", "IOReadBandwidthMax", "IOWriteBandwidthMax",
        ])

    def test_base_probe_cleanup_failure_latches_process_wide_blocker(self):
        command_id = "a" * 32
        unit_name = containment.command_unit_name("containment-capability-probe", command_id)
        snapshot = UnitSnapshot(
            unit_name=unit_name,
            description=containment.unit_description("containment-capability-probe", command_id),
            transient=True, invocation_id="b" * 32,
            control_group=containment.expected_control_group(unit_name),
            active_state="active", sub_state="running", service_type="exec",
            exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=123, exec_main_code=0, exec_main_status=0,
        )

        class Connection:
            def start_transient(self, *_args, **_kwargs):
                return None

            def snapshot(self, _unit):
                return snapshot

            def stop(self, _unit):
                raise RuntimeError("injected StopUnit failure")

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cgroup = root / snapshot.control_group.removeprefix("/")
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            with (
                mock.patch.object(containment, "_PROBE_CLEANUP_ERROR", ""),
                mock.patch.object(containment, "_cgroup2_is_unified", return_value=True),
                mock.patch.object(containment, "_SystemdConnection", return_value=Connection()),
                mock.patch.object(containment, "CGROUP_ROOT", root),
                mock.patch.object(containment.os, "urandom", return_value=b"\xaa" * 16),
            ):
                capability = containment._probe_capability()
                blocker = containment.probe_cleanup_blocker()
        self.assertEqual(capability.backend, "unresolved")
        self.assertFalse(capability.available)
        self.assertIn("StopUnit failure", blocker)

    def test_base_probe_cleans_its_bound_unit_while_an_unrelated_owner_is_live(self):
        command_id = "e" * 32
        unit_name = containment.command_unit_name("containment-capability-probe", command_id)
        observed = UnitSnapshot(
            unit_name=unit_name,
            description=containment.unit_description("containment-capability-probe", command_id),
            transient=True, invocation_id="f" * 32,
            control_group=containment.expected_control_group(unit_name),
            active_state="active", sub_state="running", service_type="exec",
            exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=123, exec_main_code=0, exec_main_status=0,
        )

        class Connection:
            def __init__(self):
                self.stopped = False
                self.stop_calls = []

            def start_transient(self, *_args, **_kwargs):
                return None

            def snapshot(self, _unit):
                return None if self.stopped else observed

            def stop(self, unit):
                self.stop_calls.append(unit)
                self.stopped = True

            def close(self):
                return None

        connection = Connection()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cgroup"
            cgroup = root / observed.control_group.removeprefix("/")
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            lease_path = Path(temporary) / "namespace.lock"
            with mock.patch.object(containment, "NAMESPACE_LEASE_PATH", lease_path):
                owner_fd = containment._acquire_namespace_lease(exclusive=False)
                try:
                    with (
                        mock.patch.object(containment, "_PROBE_CLEANUP_ERROR", ""),
                        mock.patch.object(containment, "_cgroup2_is_unified", return_value=True),
                        mock.patch.object(containment, "_SystemdConnection", return_value=connection),
                        mock.patch.object(containment, "CGROUP_ROOT", root),
                        mock.patch.object(containment, "_cgroup_is_absent", side_effect=lambda _group: connection.stopped),
                        mock.patch.object(containment.os, "urandom", return_value=b"\xee" * 16),
                    ):
                        capability = containment._probe_capability()
                        blocker = containment.probe_cleanup_blocker()
                finally:
                    os.close(owner_fd)
        self.assertTrue(capability.available, capability.reason)
        self.assertEqual(connection.stop_calls, [unit_name])
        self.assertEqual(blocker, "")

    def test_resource_probe_cleanup_failure_latches_and_prevents_another_probe(self):
        command_id = "c" * 32
        unit_name = containment.command_unit_name("resource-limit-capability-probe", command_id)
        snapshot = UnitSnapshot(
            unit_name=unit_name,
            description=containment.unit_description("resource-limit-capability-probe", command_id),
            transient=True, invocation_id="d" * 32,
            control_group=containment.expected_control_group(unit_name),
            active_state="active", sub_state="running", service_type="exec",
            exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=123, exec_main_code=0, exec_main_status=0,
            memory_max=64 * 1024 * 1024,
            startup_memory_max=DBUS_UINT64_MAX,
            effective_memory_max=64 * 1024 * 1024,
            memory_accounting=True, oom_policy="stop", oom_score_adjust=0,
        )

        class Connection:
            def start_transient(self, *_args, **_kwargs):
                return None

            def snapshot(self, _unit):
                return snapshot

            def stop(self, _unit):
                raise RuntimeError("injected resource-probe stop failure")

            def close(self):
                return None

        base = ContainmentCapability("systemd_cgroup", True, "verified")
        connection = Connection()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(containment, "_PROBE_CLEANUP_ERROR", ""),
            mock.patch.object(containment, "containment_capability", return_value=base),
            mock.patch.object(containment, "_SystemdConnection", return_value=connection) as factory,
            mock.patch.object(containment.os, "urandom", return_value=b"\xcc" * 16),
        ):
            first = resource_limit_capability(
                {"memory_max_bytes": 64 * 1024 * 1024}, workspace=temporary,
            )
            second = resource_limit_capability(
                {"memory_max_bytes": 64 * 1024 * 1024}, workspace=temporary,
            )
            blocker = containment.probe_cleanup_blocker()
        self.assertFalse(first["available"])
        self.assertEqual(second["reason"], blocker)
        self.assertEqual(factory.call_count, 1)
        self.assertIn("resource-probe stop failure", blocker)

    def test_resource_probe_cleans_its_bound_unit_while_an_unrelated_owner_is_live(self):
        command_id = "d" * 32
        unit_name = containment.command_unit_name("resource-limit-capability-probe", command_id)
        observed = UnitSnapshot(
            unit_name=unit_name,
            description=containment.unit_description("resource-limit-capability-probe", command_id),
            transient=True, invocation_id="a" * 32,
            control_group=containment.expected_control_group(unit_name),
            active_state="active", sub_state="running", service_type="exec",
            exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=123, exec_main_code=0, exec_main_status=0,
            memory_max=64 * 1024 * 1024,
            startup_memory_max=DBUS_UINT64_MAX,
            effective_memory_max=64 * 1024 * 1024,
            memory_accounting=True, oom_policy="stop", oom_score_adjust=0,
        )

        class Connection:
            def __init__(self):
                self.stopped = False
                self.stop_calls = []

            def start_transient(self, *_args, **_kwargs):
                return None

            def snapshot(self, _unit):
                return None if self.stopped else observed

            def stop(self, unit):
                self.stop_calls.append(unit)
                self.stopped = True

            def close(self):
                return None

        connection = Connection()
        base = ContainmentCapability("systemd_cgroup", True, "verified")
        with tempfile.TemporaryDirectory() as temporary:
            lease_path = Path(temporary) / "namespace.lock"
            with mock.patch.object(containment, "NAMESPACE_LEASE_PATH", lease_path):
                owner_fd = containment._acquire_namespace_lease(exclusive=False)
                try:
                    with (
                        mock.patch.object(containment, "_PROBE_CLEANUP_ERROR", ""),
                        mock.patch.object(containment, "containment_capability", return_value=base),
                        mock.patch.object(containment, "_SystemdConnection", return_value=connection),
                        mock.patch.object(containment, "_cgroup_is_absent", side_effect=lambda _group: connection.stopped),
                        mock.patch.object(containment.os, "urandom", return_value=b"\xdd" * 16),
                    ):
                        capability = resource_limit_capability(
                            {"memory_max_bytes": 64 * 1024 * 1024}, workspace=temporary,
                        )
                        blocker = containment.probe_cleanup_blocker()
                finally:
                    os.close(owner_fd)
        self.assertTrue(capability["available"], capability["reason"])
        self.assertEqual(connection.stop_calls, [unit_name])
        self.assertEqual(blocker, "")


if __name__ == "__main__":
    unittest.main()
