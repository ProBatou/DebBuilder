"""Strong per-command containment through systemd transient services.

The cgroup owned by PID 1 is the process-tree ownership boundary.  Process
identity fields and environment markers are retained only as correlation and
fallback data; they never authorize destruction of a systemd containment.

This protects against accidental orphan and resource leakage.  It is not a
security sandbox for build code running as root, which can ask systemd to move
or create workloads of its own.
"""
from __future__ import annotations

import hashlib
import fcntl
import os
import re
import select
import shutil
import signal
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable

from .command_identity import (
    COMMAND_ID_ENV,
    IDENTITY_SCHEMA_VERSION,
    RUN_ID_ENV,
    CommandIdentityError,
    IdentityRecorder,
    VerificationResult,
    VerificationStatus,
    _read_boot_id,
    _validated_identity,
)
from .resource_limits import (
    DBUS_UINT64_MAX,
    cpu_quota_usec,
    empty_policy,
    enforcement_required,
    io_target_paths,
    normalize_policy,
    requested_controls,
    validate_host_enforceability,
)


SYSTEMD_DESTINATION = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
UNIT_INTERFACE = "org.freedesktop.systemd1.Unit"
SERVICE_INTERFACE = "org.freedesktop.systemd1.Service"
CGROUP_ROOT = Path("/sys/fs/cgroup")
COMMAND_LAUNCHER = Path(__file__).with_name("command_launcher.py").resolve()
UNIT_PREFIX = "debbuilder-command-"
UNIT_NAME_PATTERN = re.compile(
    r"debbuilder-command-(?P<run_digest>[0-9a-f]{16})-(?P<command_id>[0-9a-f]{32})\.service"
)
STOP_TIMEOUT_USEC = 200_000
OPERATION_TIMEOUT = 3.0
POLL_INTERVAL = 0.02
NAMESPACE_LEASE_PATH = Path("/run/lock/debbuilder-command-namespace.lock")


class ContainmentError(RuntimeError):
    """Strong containment could not be established or proved empty."""


class ContainmentCleanupError(ContainmentError):
    """An owned transient workload could not be proved gone."""


def _acquire_namespace_lease(*, exclusive: bool) -> int:
    """Coordinate live owners and provenance-only recovery across processes."""
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(NAMESPACE_LEASE_PATH, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContainmentError("command namespace lease has an unsafe filesystem type")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ContainmentError("command namespace lease has unsafe ownership or permissions")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return fd
    except BlockingIOError as exc:
        if "fd" in locals():
            os.close(fd)
        kind = "exclusive recovery" if exclusive else "command ownership"
        raise ContainmentError(f"command namespace lease is busy for {kind}") from exc
    except Exception:
        if "fd" in locals():
            os.close(fd)
        raise


@dataclass(frozen=True)
class ContainmentCapability:
    backend: str
    available: bool
    reason: str


@dataclass(frozen=True)
class UnitSnapshot:
    unit_name: str
    description: str
    transient: bool
    invocation_id: str
    control_group: str
    active_state: str
    sub_state: str
    service_type: str
    exit_type: str
    kill_mode: str
    result: str
    main_pid: int
    exec_main_code: int
    exec_main_status: int
    memory_max: int = DBUS_UINT64_MAX
    startup_memory_max: int | None = None
    effective_memory_max: int = DBUS_UINT64_MAX
    tasks_max: int = DBUS_UINT64_MAX
    effective_tasks_max: int = DBUS_UINT64_MAX
    cpu_quota_usec: int = DBUS_UINT64_MAX
    io_read_bandwidth_max: tuple[tuple[str, int], ...] = ()
    io_write_bandwidth_max: tuple[tuple[str, int], ...] = ()
    memory_peak: int = 0
    cpu_usage_nsec: int = 0
    io_read_bytes: int = 0
    io_write_bytes: int = 0
    oom_policy: str = ""
    oom_score_adjust: int | None = None
    tasks_current: int = 0
    memory_accounting: bool = False
    tasks_accounting: bool = False
    cpu_accounting: bool = False
    io_accounting: bool = False

    @property
    def command_complete(self) -> bool:
        return not self.control_group and (
            self.sub_state in {"exited", "dead", "failed"}
            or self.active_state in {"inactive", "failed"}
        )

    @property
    def process_exit_code(self) -> int | None:
        if self.exec_main_code == 1:
            return self.exec_main_status
        if self.exec_main_code in {2, 3} and self.exec_main_status:
            return -self.exec_main_status
        return None


@dataclass(frozen=True)
class ContainmentTermination:
    process_exit_code: int | None
    killed: bool
    gone: bool
    error: str = ""
    enforcement_error: str = ""


@dataclass(frozen=True)
class ContainmentRecovery:
    """Authoritative result of reconciling one historical systemd command."""

    verification: VerificationResult
    signalled: bool = False
    killed: bool = False
    gone: bool = False
    resource_control: dict | None = None


@dataclass(frozen=True)
class OrphanContainmentRecovery:
    """Result of provenance-only reconciliation for an unbound namespace member."""

    verification: VerificationResult
    signalled: bool = False
    killed: bool = False
    gone: bool = False


_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY: ContainmentCapability | None = None
_CLEANUP_GATE_LOCK = threading.RLock()
_PROBE_CLEANUP_LOCK = threading.Lock()
_PROBE_CLEANUP_ERROR = ""
_RUNTIME_CLEANUP_LOCK = threading.Lock()
_RUNTIME_CLEANUP_ERROR = ""


def _latch_probe_cleanup_blocker(reason: str) -> str:
    global _PROBE_CLEANUP_ERROR
    bounded = str(reason or "transient capability probe cleanup is unresolved")[:500]
    with _CLEANUP_GATE_LOCK:
        with _PROBE_CLEANUP_LOCK:
            if not _PROBE_CLEANUP_ERROR:
                _PROBE_CLEANUP_ERROR = bounded
            return _PROBE_CLEANUP_ERROR


def probe_cleanup_blocker() -> str:
    """Return the process-lifetime blocker created by an unproved probe teardown."""
    with _PROBE_CLEANUP_LOCK:
        return _PROBE_CLEANUP_ERROR


def latch_runtime_cleanup_blocker(reason: str) -> str:
    """Latch an unproved runtime command teardown for the process lifetime."""
    global _RUNTIME_CLEANUP_ERROR
    bounded = str(reason or "transient runtime command cleanup is unresolved")[:500]
    with _CLEANUP_GATE_LOCK:
        with _RUNTIME_CLEANUP_LOCK:
            if not _RUNTIME_CLEANUP_ERROR:
                _RUNTIME_CLEANUP_ERROR = bounded
            return _RUNTIME_CLEANUP_ERROR


def runtime_cleanup_blocker() -> str:
    """Return the process-lifetime blocker created by an unproved command teardown."""
    with _RUNTIME_CLEANUP_LOCK:
        return _RUNTIME_CLEANUP_ERROR


def containment_cleanup_blocker() -> str:
    """Return any process-lifetime blocker that makes further mutation unsafe."""
    with _CLEANUP_GATE_LOCK:
        return probe_cleanup_blocker() or runtime_cleanup_blocker()


@contextmanager
def containment_safety_gate():
    """Serialize blocker publication with admission and destructive cleanup."""
    with _CLEANUP_GATE_LOCK:
        yield


def containment_safety_serialized(function):
    """Hold the containment safety gate for one complete mutation boundary."""
    @wraps(function)
    def guarded(*args, **kwargs):
        with containment_safety_gate():
            return function(*args, **kwargs)
    return guarded


def command_unit_name(run_id: str, command_id: str) -> str:
    """Derive a bounded unit name without embedding the user-controlled Run ID."""
    if not isinstance(run_id, str) or not run_id:
        raise ContainmentError("Run ID is unavailable for systemd containment")
    if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f]{32}", command_id):
        raise ContainmentError("command ID is invalid for systemd containment")
    run_digest = command_run_digest(run_id)
    return f"{UNIT_PREFIX}{run_digest}-{command_id}.service"


def command_run_digest(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ContainmentError("Run ID is unavailable for systemd containment")
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


def is_command_namespace_member(unit_name: str) -> bool:
    return (
        isinstance(unit_name, str)
        and unit_name.startswith(UNIT_PREFIX)
        and unit_name.endswith(".service")
    )


def command_unit_components(unit_name: str) -> tuple[str, str]:
    match = UNIT_NAME_PATTERN.fullmatch(str(unit_name or ""))
    if match is None:
        raise ContainmentError("transient unit name is invalid")
    return match.group("run_digest"), match.group("command_id")


def expected_control_group(unit_name: str) -> str:
    command_unit_components(unit_name)
    return f"/system.slice/{unit_name}"


def unit_description(run_id: str, command_id: str) -> str:
    digest = command_run_digest(run_id)
    return f"DebBuilder command {digest}/{command_id}"


def starting_metadata(run_id: str, command_id: str, resource_policy: dict | None = None, *, workspace: Path | None = None) -> dict:
    policy = normalize_policy(resource_policy if resource_policy is not None else empty_policy())
    io_requested = policy["io_read_bandwidth_max_bytes_per_sec"] is not None or policy["io_write_bandwidth_max_bytes_per_sec"] is not None
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "backend": "systemd_cgroup",
        "boot_id": _read_boot_id(),
        "run_id": run_id,
        "command_id": command_id,
        "unit_name": command_unit_name(run_id, command_id),
        "containment_state": "starting",
        "resource_limits": policy,
        "resource_io_targets": io_target_paths(workspace) if io_requested and workspace is not None else [],
    }


def _cgroup2_is_unified() -> bool:
    try:
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            if separator and len(fields) >= 5 and fields[4] == str(CGROUP_ROOT) and after.split()[0] == "cgroup2":
                return (CGROUP_ROOT / "cgroup.controllers").is_file()
    except (OSError, IndexError):
        return False
    return False


def _private_system_bus():
    try:
        import dbus
    except ImportError as exc:
        raise ContainmentError("python3-dbus is unavailable") from exc
    try:
        return dbus.SystemBus(private=True)
    except Exception as exc:
        raise ContainmentError(f"system systemd manager is unreachable: {exc}") from exc


class _SystemdConnection:
    def __init__(self):
        self.bus = _private_system_bus()
        try:
            import dbus

            manager_object = self.bus.get_object(SYSTEMD_DESTINATION, SYSTEMD_PATH)
            self.manager = dbus.Interface(manager_object, MANAGER_INTERFACE)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        bus, self.bus = getattr(self, "bus", None), None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def start_transient(
        self,
        unit_name: str,
        *,
        arguments: list[str],
        executable: str,
        cwd: Path,
        environment: dict[str, str],
        stdout_fd: int,
        stderr_fd: int,
        description: str,
        resource_policy: dict | None = None,
        resource_workspace: Path | None = None,
        stdin_fd: int | None = None,
    ) -> None:
        import dbus
        from dbus.types import UnixFd

        exec_start = dbus.Array([
            dbus.Struct((
                dbus.String(executable),
                dbus.Array([dbus.String(value) for value in arguments], signature="s"),
                dbus.Boolean(False),
            ), signature="sasb"),
        ], signature="(sasb)")
        policy = validate_host_enforceability(resource_policy if resource_policy is not None else empty_policy())
        resource_properties = []
        if policy["memory_max_bytes"] is not None:
            resource_properties += [
                ("MemoryAccounting", dbus.Boolean(True)),
                ("MemoryMax", dbus.UInt64(policy["memory_max_bytes"])),
                # Keep PID 1's pre-exec preparation out of an arbitrarily low
                # runtime ceiling. Type=exec reaches active state only after
                # the gated launcher execs; MemoryMax is verified before the
                # target command is released.
                ("StartupMemoryMax", dbus.UInt64(DBUS_UINT64_MAX)),
                ("OOMPolicy", dbus.String("stop")),
                ("OOMScoreAdjust", dbus.Int32(0)),
            ]
        if policy["tasks_max"] is not None:
            resource_properties += [
                ("TasksAccounting", dbus.Boolean(True)),
                ("TasksMax", dbus.UInt64(policy["tasks_max"])),
            ]
        if policy["cpu_quota_percent"] is not None:
            resource_properties += [
                ("CPUAccounting", dbus.Boolean(True)),
                ("CPUQuotaPerSecUSec", dbus.UInt64(cpu_quota_usec(policy["cpu_quota_percent"]))),
            ]
        if policy["io_read_bandwidth_max_bytes_per_sec"] is not None or policy["io_write_bandwidth_max_bytes_per_sec"] is not None:
            targets = io_target_paths(resource_workspace or cwd)
            resource_properties.append(("IOAccounting", dbus.Boolean(True)))
            for field, prop in (
                ("io_read_bandwidth_max_bytes_per_sec", "IOReadBandwidthMax"),
                ("io_write_bandwidth_max_bytes_per_sec", "IOWriteBandwidthMax"),
            ):
                if policy[field] is not None:
                    rows = dbus.Array([
                        dbus.Struct((dbus.String(path), dbus.UInt64(policy[field])), signature="st")
                        for path in targets
                    ], signature="(st)")
                    resource_properties.append((prop, rows))
        properties = dbus.Array([
            ("Description", dbus.String(description)),
            ("Type", dbus.String("exec")),
            ("ExitType", dbus.String("cgroup")),
            # Retaining terminal state closes the fast-command result race.  We
            # explicitly stop/reset and prove disappearance after reading it.
            ("RemainAfterExit", dbus.Boolean(True)),
            ("ExecStart", exec_start),
            ("WorkingDirectory", dbus.String(str(cwd))),
            ("Environment", dbus.Array([dbus.String(f"{key}={value}") for key, value in environment.items()], signature="s")),
            ("StandardOutputFileDescriptor", UnixFd(stdout_fd)),
            ("StandardErrorFileDescriptor", UnixFd(stderr_fd)),
            *([("StandardInputFileDescriptor", UnixFd(stdin_fd))] if stdin_fd is not None else []),
            ("KillMode", dbus.String("control-group")),
            ("KillSignal", dbus.Int32(signal.SIGTERM)),
            ("SendSIGKILL", dbus.Boolean(True)),
            ("FinalKillSignal", dbus.Int32(signal.SIGKILL)),
            ("TimeoutStopUSec", dbus.UInt64(STOP_TIMEOUT_USEC)),
            # Failed units must remain inspectable until their exit status is
            # consumed; ResetFailedUnit then permits collection.
            ("CollectMode", dbus.String("inactive")),
            *resource_properties,
        ], signature="(sv)")
        self.manager.StartTransientUnit(
            unit_name,
            "fail",
            properties,
            dbus.Array([], signature="(sa(sv))"),
        )

    def snapshot(self, unit_name: str) -> UnitSnapshot | None:
        import dbus

        try:
            path = self.manager.GetUnit(unit_name)
            properties = dbus.Interface(
                self.bus.get_object(SYSTEMD_DESTINATION, path), PROPERTIES_INTERFACE,
            )
            unit = properties.GetAll(UNIT_INTERFACE)
            service = properties.GetAll(SERVICE_INTERFACE)
        except dbus.DBusException as exc:
            if exc.get_dbus_name() in {
                "org.freedesktop.systemd1.NoSuchUnit",
                "org.freedesktop.DBus.Error.UnknownObject",
            }:
                return None
            raise ContainmentError(f"transient unit could not be inspected: {exc}") from exc
        try:
            invocation_id = bytes(unit["InvocationID"]).hex()
            return UnitSnapshot(
                unit_name=str(unit["Id"]),
                description=str(unit["Description"]),
                transient=bool(unit["Transient"]),
                invocation_id=invocation_id,
                control_group=str(service["ControlGroup"]),
                active_state=str(unit["ActiveState"]),
                sub_state=str(unit["SubState"]),
                service_type=str(service["Type"]),
                exit_type=str(service["ExitType"]),
                kill_mode=str(service["KillMode"]),
                result=str(service["Result"]),
                main_pid=int(service["MainPID"]),
                exec_main_code=int(service["ExecMainCode"]),
                exec_main_status=int(service["ExecMainStatus"]),
                memory_max=int(service.get("MemoryMax", (1 << 64) - 1)),
                startup_memory_max=(
                    int(service["StartupMemoryMax"])
                    if "StartupMemoryMax" in service else None
                ),
                effective_memory_max=int(service.get("EffectiveMemoryMax", (1 << 64) - 1)),
                tasks_max=int(service.get("TasksMax", (1 << 64) - 1)),
                effective_tasks_max=int(service.get("EffectiveTasksMax", (1 << 64) - 1)),
                cpu_quota_usec=int(service.get("CPUQuotaPerSecUSec", (1 << 64) - 1)),
                io_read_bandwidth_max=tuple((str(path), int(rate)) for path, rate in service.get("IOReadBandwidthMax", [])),
                io_write_bandwidth_max=tuple((str(path), int(rate)) for path, rate in service.get("IOWriteBandwidthMax", [])),
                memory_peak=int(service.get("MemoryPeak", 0)),
                cpu_usage_nsec=int(service.get("CPUUsageNSec", 0)),
                io_read_bytes=int(service.get("IOReadBytes", 0)),
                io_write_bytes=int(service.get("IOWriteBytes", 0)),
                oom_policy=str(service.get("OOMPolicy", "")),
                oom_score_adjust=(
                    int(service["OOMScoreAdjust"])
                    if "OOMScoreAdjust" in service else None
                ),
                tasks_current=int(service.get("TasksCurrent", 0)),
                memory_accounting=bool(service.get("MemoryAccounting", False)),
                tasks_accounting=bool(service.get("TasksAccounting", False)),
                cpu_accounting=bool(service.get("CPUAccounting", False)),
                io_accounting=bool(service.get("IOAccounting", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContainmentError("transient unit returned incomplete properties") from exc

    def stop(self, unit_name: str) -> None:
        self.manager.StopUnit(unit_name, "replace")

    def reset_failed(self, unit_name: str) -> None:
        self.manager.ResetFailedUnit(unit_name)

    def command_unit_names(self) -> set[str]:
        """List every loaded unit in our namespace, including retained terminal units."""
        import dbus

        try:
            rows = self.manager.ListUnitsByPatterns(
                dbus.Array([], signature="s"),
                dbus.Array([f"{UNIT_PREFIX}*.service"], signature="s"),
            )
        except Exception as exc:
            raise ContainmentError(f"DebBuilder transient units could not be listed: {exc}") from exc
        # Inventory is deliberately broader than destructive authorization.
        # A malformed member of our reserved prefix must remain visible as a
        # blocker; only command_unit_components() may authorize reconciliation.
        return {
            str(row[0]) for row in rows
            if row
            and is_command_namespace_member(str(row[0]))
        }

    def command_units(self) -> set[str]:
        """List live loaded units in our namespace for compatibility diagnostics."""
        names = self.command_unit_names()
        live = set()
        for name in names:
            try:
                snapshot = self.snapshot(name)
            except ContainmentError:
                # Inspection uncertainty is intentionally surfaced as a
                # potentially live namespace member.
                live.add(name)
                continue
            if snapshot is not None and (
                snapshot.control_group
                or snapshot.active_state in {"activating", "active", "deactivating", "reloading"}
            ):
                live.add(name)
        return live


def _snapshot_matches_ownership(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
    expected_unit = command_unit_name(metadata.get("run_id", ""), metadata.get("command_id", ""))
    expected_group = expected_control_group(expected_unit)
    expected_description = unit_description(metadata["run_id"], metadata["command_id"])
    if metadata.get("unit_name") != expected_unit or snapshot.unit_name != expected_unit:
        return VerificationResult(VerificationStatus.MISMATCH, "transient unit name does not match command identity")
    if not snapshot.transient:
        return VerificationResult(VerificationStatus.MISMATCH, "unit is not transient")
    if snapshot.description != expected_description:
        return VerificationResult(VerificationStatus.MISMATCH, "transient unit description binding does not match")
    if (snapshot.service_type, snapshot.exit_type, snapshot.kill_mode) != ("exec", "cgroup", "control-group"):
        return VerificationResult(VerificationStatus.MISMATCH, "transient service containment properties do not match")
    if snapshot.control_group and snapshot.control_group != expected_group:
        return VerificationResult(VerificationStatus.MISMATCH, "transient unit control group is outside its expected boundary")
    return VerificationResult(VerificationStatus.MATCH, "precommitted transient unit ownership matches")


def _snapshot_matches_orphan_provenance(snapshot: UnitSnapshot, unit_name: str) -> VerificationResult:
    """Authenticate immutable systemd provenance without inventing a Run binding."""
    try:
        run_digest, command_id = command_unit_components(unit_name)
        expected_group = expected_control_group(unit_name)
    except ContainmentError as exc:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if snapshot.unit_name != unit_name:
        return VerificationResult(VerificationStatus.MISMATCH, "orphan unit name changed during inspection")
    if not snapshot.transient:
        return VerificationResult(VerificationStatus.MISMATCH, "orphan unit is not transient")
    if snapshot.description != f"DebBuilder command {run_digest}/{command_id}":
        return VerificationResult(VerificationStatus.MISMATCH, "orphan unit Description does not match its canonical name")
    if (snapshot.service_type, snapshot.exit_type, snapshot.kill_mode) != ("exec", "cgroup", "control-group"):
        return VerificationResult(VerificationStatus.MISMATCH, "orphan unit containment properties do not match")
    if not re.fullmatch(r"[0-9a-f]{32}", snapshot.invocation_id or ""):
        return VerificationResult(VerificationStatus.UNVERIFIABLE, "orphan unit invocation identity is unavailable")
    if snapshot.control_group:
        if snapshot.control_group != expected_group:
            return VerificationResult(VerificationStatus.MISMATCH, "orphan unit control group is outside its canonical boundary")
    elif not snapshot.command_complete:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, "orphan unit has no inspectable control group while active")
    return VerificationResult(VerificationStatus.MATCH, "orphan transient-unit provenance matches")


def _snapshot_matches_resources(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
    policy = normalize_policy(metadata.get("resource_limits"))
    if policy["memory_max_bytes"] is not None:
        requested = policy["memory_max_bytes"]
        if (
            not snapshot.memory_accounting
            or snapshot.memory_max != requested
            or snapshot.startup_memory_max != DBUS_UINT64_MAX
            or snapshot.effective_memory_max > requested
            or snapshot.oom_policy != "stop"
            or snapshot.oom_score_adjust != 0
        ):
            return VerificationResult(VerificationStatus.MISMATCH, "transient unit memory resource properties do not match")
    if policy["tasks_max"] is not None:
        requested = policy["tasks_max"]
        if not snapshot.tasks_accounting or snapshot.tasks_max != requested or snapshot.effective_tasks_max > requested:
            return VerificationResult(VerificationStatus.MISMATCH, "transient unit task resource properties do not match")
    if policy["cpu_quota_percent"] is not None and (
        not snapshot.cpu_accounting or snapshot.cpu_quota_usec != cpu_quota_usec(policy["cpu_quota_percent"])
    ):
        return VerificationResult(VerificationStatus.MISMATCH, "transient unit CPU quota does not match")
    targets = tuple(metadata.get("resource_io_targets") or ())
    if any(policy[field] is not None for field in (
        "io_read_bandwidth_max_bytes_per_sec", "io_write_bandwidth_max_bytes_per_sec",
    )) and not snapshot.io_accounting:
        return VerificationResult(VerificationStatus.MISMATCH, "transient unit I/O accounting is not enabled")
    for field, actual in (
        ("io_read_bandwidth_max_bytes_per_sec", snapshot.io_read_bandwidth_max),
        ("io_write_bandwidth_max_bytes_per_sec", snapshot.io_write_bandwidth_max),
    ):
        rate = policy[field]
        if rate is not None and tuple(actual) != tuple((path, rate) for path in targets):
            return VerificationResult(VerificationStatus.MISMATCH, "transient unit I/O bandwidth properties do not match")
    return VerificationResult(VerificationStatus.MATCH, "transient unit resource properties match")


def _snapshot_matches_intent(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
    matched = _snapshot_matches_ownership(snapshot, metadata)
    if matched.status is not VerificationStatus.MATCH:
        return matched
    return _snapshot_matches_resources(snapshot, metadata)


def _snapshot_matches_metadata(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
    matched = _snapshot_matches_intent(snapshot, metadata)
    if matched.status is not VerificationStatus.MATCH:
        return matched
    state = metadata.get("containment_state")
    if state in {"active", "stopping"}:
        if snapshot.invocation_id != metadata["invocation_id"]:
            return VerificationResult(VerificationStatus.MISMATCH, "transient unit invocation ID does not match")
        if metadata["control_group"] != expected_control_group(metadata["unit_name"]):
            return VerificationResult(VerificationStatus.MISMATCH, "recorded control group does not match")
    return VerificationResult(VerificationStatus.MATCH, "canonical transient unit identity matches")


def _snapshot_matches_ownership_metadata(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
    matched = _snapshot_matches_ownership(snapshot, metadata)
    if matched.status is not VerificationStatus.MATCH:
        return matched
    state = metadata.get("containment_state")
    if state in {"active", "stopping"}:
        if snapshot.invocation_id != metadata["invocation_id"]:
            return VerificationResult(VerificationStatus.MISMATCH, "transient unit invocation ID does not match")
        if metadata["control_group"] != expected_control_group(metadata["unit_name"]):
            return VerificationResult(VerificationStatus.MISMATCH, "recorded control group does not match")
    return VerificationResult(VerificationStatus.MATCH, "canonical transient unit ownership matches")


def verify_unit(connection: _SystemdConnection, metadata: dict, *, verify_resources: bool = True) -> tuple[UnitSnapshot | None, VerificationResult]:
    """Verify durable metadata before any destructive systemd operation."""
    try:
        metadata = _validated_identity(metadata)
        if metadata.get("backend") != "systemd_cgroup":
            raise ContainmentError("active command does not use systemd containment")
        if metadata.get("boot_id") != _read_boot_id():
            return None, VerificationResult(VerificationStatus.MISMATCH, "recorded boot ID does not match current boot")
        expected_unit = command_unit_name(metadata.get("run_id", ""), metadata.get("command_id", ""))
        if metadata.get("unit_name") != expected_unit:
            return None, VerificationResult(VerificationStatus.MISMATCH, "recorded unit name does not match command identity")
        snapshot = connection.snapshot(expected_unit)
        if snapshot is None:
            return None, VerificationResult(VerificationStatus.NOT_RUNNING, "precommitted transient unit does not exist")
        matched = _snapshot_matches_metadata(snapshot, metadata) if verify_resources else _snapshot_matches_ownership_metadata(snapshot, metadata)
        return snapshot, matched
    except (CommandIdentityError, ContainmentError, OSError, TypeError, ValueError) as exc:
        return None, VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))


def recover_systemd_containment(
    metadata: dict,
    *,
    expected_run_id: str,
    recorder: IdentityRecorder,
    connection_factory=_SystemdConnection,
    on_wait: Callable[[float], None] | None = None,
) -> ContainmentRecovery:
    """Reconcile one durable systemd identity without ever downgrading to PGID."""
    try:
        value = _validated_identity(metadata, expected_run_id=expected_run_id)
        if value.get("backend") != "systemd_cgroup":
            raise ContainmentError("active command does not use systemd containment")
        if value["boot_id"] != _read_boot_id():
            return ContainmentRecovery(VerificationResult(
                VerificationStatus.NOT_RUNNING,
                "recorded transient service belongs to a previous boot",
            ), gone=True)
    except (CommandIdentityError, ContainmentError, OSError, TypeError, ValueError) as exc:
        return ContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc)))

    connection = None
    try:
        connection = connection_factory()
        snapshot, verification = verify_unit(connection, value, verify_resources=False)
        if verification.status is VerificationStatus.NOT_RUNNING:
            control_group = value.get("control_group") or expected_control_group(value["unit_name"])
            try:
                gone = _cgroup_is_absent(control_group)
            except (ContainmentError, OSError) as exc:
                return ContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc)))
            if gone:
                return ContainmentRecovery(verification, gone=True)
            return ContainmentRecovery(VerificationResult(
                VerificationStatus.UNVERIFIABLE,
                "transient service is absent but its canonical cgroup remains",
            ))
        if verification.status is not VerificationStatus.MATCH or snapshot is None:
            return ContainmentRecovery(verification)

        resource_verification = _snapshot_matches_resources(snapshot, value)
        command = SystemdCommandContainment(connection, recorder, value, None, None)
        command.last_snapshot = snapshot
        terminated = command._terminate_record(
            value,
            persist_stopping=value.get("containment_state") == "active",
            on_wait=on_wait,
        )
        resource_control = command.resource_control(
            verification="verified" if resource_verification.status is VerificationStatus.MATCH else "failed",
        )
        if terminated.gone:
            suffix = "" if resource_verification.status is VerificationStatus.MATCH else f"; resource verification failed: {resource_verification.reason}"
            return ContainmentRecovery(
                VerificationResult(VerificationStatus.NOT_RUNNING, "transient service and cgroup are absent" + suffix),
                signalled=True,
                killed=terminated.killed,
                gone=True,
                resource_control=resource_control,
            )
        return ContainmentRecovery(VerificationResult(
            VerificationStatus.UNVERIFIABLE,
            terminated.error or "transient service disappearance could not be proven",
        ), signalled=not bool(terminated.error), killed=terminated.killed)
    except (CommandIdentityError, ContainmentError, OSError, TypeError, ValueError) as exc:
        return ContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc)))
    finally:
        if connection is not None:
            connection.close()


def recover_orphan_systemd_containment(
    unit_name: str,
    *,
    connection_factory=_SystemdConnection,
    on_wait: Callable[[float], None] | None = None,
) -> OrphanContainmentRecovery:
    """Reconcile an unbound unit only from strict, revalidated systemd provenance."""
    try:
        command_unit_components(unit_name)
        control_group = expected_control_group(unit_name)
    except ContainmentError as exc:
        return OrphanContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc)))

    connection = None
    lease_fd = -1
    try:
        # Provenance alone cannot distinguish a crashed owner from a command
        # that belongs to another live DebBuilder process. Every live owner
        # holds a shared lease, so provenance-only destruction requires the
        # exclusive lease and fails closed while any owner is alive.
        lease_fd = _acquire_namespace_lease(exclusive=True)
        connection = connection_factory()
        snapshot = connection.snapshot(unit_name)
        if snapshot is None:
            if _cgroup_is_absent(control_group):
                return OrphanContainmentRecovery(
                    VerificationResult(VerificationStatus.NOT_RUNNING, "orphan unit and cgroup are absent"),
                    gone=True,
                )
            return OrphanContainmentRecovery(VerificationResult(
                VerificationStatus.UNVERIFIABLE,
                "orphan cgroup exists but its transient unit provenance is not observable",
            ))
        verification = _snapshot_matches_orphan_provenance(snapshot, unit_name)
        if verification.status is not VerificationStatus.MATCH:
            return OrphanContainmentRecovery(verification)

        # Revalidate all provenance plus the observed invocation immediately
        # before the first destructive operation to close the unit-reuse race.
        current = connection.snapshot(unit_name)
        if current is None:
            if _cgroup_is_absent(control_group):
                return OrphanContainmentRecovery(
                    VerificationResult(VerificationStatus.NOT_RUNNING, "orphan unit and cgroup disappeared before reconciliation"),
                    gone=True,
                )
            return OrphanContainmentRecovery(VerificationResult(
                VerificationStatus.UNVERIFIABLE,
                "orphan unit disappeared but its canonical cgroup remains",
            ))
        revalidated = _snapshot_matches_orphan_provenance(current, unit_name)
        if revalidated.status is not VerificationStatus.MATCH:
            return OrphanContainmentRecovery(revalidated)
        if current.invocation_id != snapshot.invocation_id:
            return OrphanContainmentRecovery(VerificationResult(
                VerificationStatus.MISMATCH, "orphan unit invocation changed before termination",
            ))
        try:
            connection.stop(unit_name)
        except Exception as exc:
            return OrphanContainmentRecovery(VerificationResult(
                VerificationStatus.UNVERIFIABLE, f"orphan StopUnit failed: {exc}",
            ))
        pinned_invocation = current.invocation_id

        def authorize_stopping_snapshot(value: UnitSnapshot) -> VerificationResult:
            checked = _snapshot_matches_orphan_provenance(value, unit_name)
            if checked.status is VerificationStatus.MATCH and value.invocation_id != pinned_invocation:
                return VerificationResult(
                    VerificationStatus.MISMATCH,
                    "orphan unit invocation changed while stopping",
                )
            return checked

        terminated = _wait_for_unit_disappearance(
            connection,
            unit_name=unit_name,
            control_group=control_group,
            authorize_snapshot=authorize_stopping_snapshot,
            last_snapshot=current,
            on_wait=on_wait,
        )
        if terminated.gone:
            return OrphanContainmentRecovery(
                VerificationResult(VerificationStatus.NOT_RUNNING, "orphan transient unit and cgroup are absent"),
                signalled=True,
                killed=terminated.killed,
                gone=True,
            )
        return OrphanContainmentRecovery(
            VerificationResult(
                VerificationStatus.UNVERIFIABLE,
                terminated.error or "orphan transient-unit disappearance could not be proved",
            ),
            signalled=True,
            killed=terminated.killed,
        )
    except (ContainmentError, OSError, TypeError, ValueError) as exc:
        return OrphanContainmentRecovery(VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc)))
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)


def _command_cgroup_units() -> set[str]:
    """Return canonical command cgroup directory names or fail closed."""
    cgroup_units = set()
    system_slice = CGROUP_ROOT / "system.slice"
    try:
        try:
            root_info = os.stat(system_slice, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(root_info.st_mode):
                raise ContainmentError("system.slice cgroup root has an unexpected filesystem type")
            with os.scandir(system_slice) as entries:
                for entry in entries:
                    if not is_command_namespace_member(entry.name):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        cgroup_units.add(entry.name)
                    else:
                        raise ContainmentError(
                            f"DebBuilder command cgroup has an unexpected filesystem type: {entry.name}"
                        )
    except OSError as exc:
        raise ContainmentError(f"DebBuilder command cgroups could not be listed: {exc}") from exc

    return cgroup_units


def loaded_command_units(*, connection_factory=_SystemdConnection) -> set[str]:
    """Return all loaded/cgroup namespace members, failing if either inventory is uncertain."""
    cgroup_units = _command_cgroup_units()
    connection = None
    try:
        connection = connection_factory()
        loaded = (
            connection.command_unit_names()
            if hasattr(connection, "command_unit_names")
            else connection.command_units()
        )
        return cgroup_units | loaded
    except Exception as exc:
        raise ContainmentError(f"DebBuilder transient-unit inventory is unavailable: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def matching_run_command_units(
    run_id: str, *, inventory: Callable[[], set[str]] = loaded_command_units,
) -> set[str]:
    """Conservatively resolve current-boot namespace members for one Run digest."""
    digest = command_run_digest(run_id)
    try:
        names = inventory()
    except Exception as exc:
        raise ContainmentError(f"DebBuilder command namespace cannot be verified for Run cleanup: {exc}") from exc
    matches = set()
    for name in names:
        try:
            run_digest, _command_id = command_unit_components(name)
        except ContainmentError as exc:
            raise ContainmentError(f"DebBuilder command namespace contains an invalid member: {name!r}") from exc
        if run_digest == digest:
            matches.add(name)
    return matches


def _cgroup_is_absent(control_group: str) -> bool:
    if control_group != expected_control_group(Path(control_group).name):
        raise ContainmentError("control group path is not confined to a DebBuilder command unit")
    try:
        os.stat(CGROUP_ROOT / control_group.removeprefix("/"), follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _resolve_executable(argument: str, *, cwd: Path, environment: dict[str, str]) -> str:
    if "/" in argument:
        candidate = Path(argument) if Path(argument).is_absolute() else cwd / argument
        return str(candidate.resolve(strict=False))
    executable = shutil.which(argument, path=environment.get("PATH"))
    if not executable:
        raise FileNotFoundError(2, "command executable was not found", argument)
    return executable


def _reconcile_owned_probe(connection, metadata: dict) -> ContainmentTermination:
    """Terminate a probe through its exact current-boot identity, not orphan provenance."""
    command = SystemdCommandContainment(connection, None, metadata, None, None)
    return command._terminate_record(metadata, persist_stopping=False)


def _probe_capability() -> ContainmentCapability:
    if not sys.platform.startswith("linux"):
        return ContainmentCapability("process_group", False, "Linux is required")
    if not _cgroup2_is_unified():
        return ContainmentCapability("process_group", False, "a unified cgroup v2 mount is required")
    connection = None
    read_fd = write_fd = -1
    probe_run = "containment-capability-probe"
    command_id = os.urandom(16).hex()
    unit_name = command_unit_name(probe_run, command_id)
    control_group = expected_control_group(unit_name)
    metadata = starting_metadata(probe_run, command_id, workspace=Path("/"))
    start_attempted = False
    cleanup_gone = False
    lease_fd = -1

    def reconcile_probe() -> ContainmentTermination:
        if connection is None:
            return ContainmentTermination(
                None, False, False, "capability probe systemd connection is unavailable",
            )
        return _reconcile_owned_probe(connection, metadata)

    try:
        lease_fd = _acquire_namespace_lease(exclusive=False)
        connection = _SystemdConnection()
        read_fd, write_fd = os.pipe()
        # Use the same required property set and FD transport as real commands.
        start_attempted = True
        connection.start_transient(
            unit_name,
            arguments=["/usr/bin/sleep", "0.5"],
            executable="/usr/bin/sleep",
            cwd=Path("/"),
            environment={"PATH": "/usr/bin:/bin"},
            stdout_fd=write_fd,
            stderr_fd=write_fd,
            description=unit_description(probe_run, command_id),
        )
        deadline = time.monotonic() + OPERATION_TIMEOUT
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = connection.snapshot(unit_name)
            if snapshot is not None:
                break
            time.sleep(POLL_INTERVAL)
        if snapshot is None:
            raise ContainmentError("transient-service probe unit was not observable")
        matched = _snapshot_matches_intent(snapshot, metadata)
        if matched.status is not VerificationStatus.MATCH:
            raise ContainmentError(matched.reason)
        if snapshot.control_group != control_group:
            raise ContainmentError("transient-service probe did not enter the expected cgroup")
        events = CGROUP_ROOT / control_group.removeprefix("/") / "cgroup.events"
        if not events.is_file() or "populated " not in events.read_text(encoding="ascii"):
            raise ContainmentError("transient-service cgroup lifecycle is not inspectable")
        recovered = reconcile_probe()
        cleanup_gone = recovered.gone
        if not cleanup_gone:
            raise ContainmentCleanupError(
                f"transient-service probe could not prove cgroup disappearance: "
                f"{recovered.error or 'absence could not be proved'}"
            )
        return ContainmentCapability("systemd_cgroup", True, "systemd transient-service cgroup probe succeeded")
    except Exception as exc:
        cleanup_reason = ""
        if start_attempted and not cleanup_gone and connection is not None:
            recovered = reconcile_probe()
            cleanup_gone = recovered.gone
            cleanup_reason = recovered.error
        if start_attempted and not cleanup_gone:
            reason = _latch_probe_cleanup_blocker(
                f"{exc}; capability probe cleanup unresolved: {cleanup_reason or 'absence could not be proved'}"
            )
            return ContainmentCapability("unresolved", False, reason)
        return ContainmentCapability("process_group", False, str(exc))
    finally:
        for fd in (write_fd, read_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            if connection is not None:
                connection.close()
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)


@containment_safety_serialized
def containment_capability(*, refresh: bool = False) -> ContainmentCapability:
    global _CAPABILITY
    unresolved_cleanup = containment_cleanup_blocker()
    if unresolved_cleanup:
        return ContainmentCapability("unresolved", False, unresolved_cleanup)
    with _CAPABILITY_LOCK:
        if refresh or _CAPABILITY is None:
            _CAPABILITY = _probe_capability()
        unresolved_cleanup = containment_cleanup_blocker()
        if unresolved_cleanup:
            return ContainmentCapability("unresolved", False, unresolved_cleanup)
        return _CAPABILITY


@containment_safety_serialized
def cached_containment_capability() -> ContainmentCapability:
    """Return the last probe result without causing a transient-unit probe."""
    unresolved_cleanup = containment_cleanup_blocker()
    if unresolved_cleanup:
        return ContainmentCapability("unresolved", False, unresolved_cleanup)
    with _CAPABILITY_LOCK:
        return _CAPABILITY or ContainmentCapability(
            "unknown", False, "containment capability has not been evaluated",
        )


@containment_safety_serialized
def resource_limit_capability(policy: dict, *, workspace: str | Path, refresh: bool = False) -> dict:
    """Prove the exact finite property subset with a disposable transient unit."""
    canonical = normalize_policy(policy)
    controls = requested_controls(canonical)
    unresolved_cleanup = containment_cleanup_blocker()
    if unresolved_cleanup:
        return {
            "backend": "systemd_cgroup",
            "available": False,
            "requested_controls": controls,
            "reason": unresolved_cleanup,
        }
    base = containment_capability(refresh=refresh)
    if not controls:
        return {
            "backend": base.backend,
            "available": True,
            "requested_controls": [],
            "reason": base.reason[:500],
        }
    if not base.available:
        return {
            "backend": base.backend,
            "available": False,
            "requested_controls": controls,
            "reason": base.reason[:500],
        }
    connection = None
    read_fd = write_fd = -1
    probe_run = "resource-limit-capability-probe"
    command_id = os.urandom(16).hex()
    unit_name = command_unit_name(probe_run, command_id)
    start_attempted = False
    cleanup_gone = False
    lease_fd = -1

    def reconcile_probe() -> ContainmentTermination:
        if connection is None:
            return ContainmentTermination(
                None, False, False, "resource probe systemd connection is unavailable",
            )
        return _reconcile_owned_probe(connection, metadata)

    try:
        canonical = validate_host_enforceability(canonical)
        workspace = Path(workspace).resolve()
        metadata = starting_metadata(probe_run, command_id, canonical, workspace=workspace)
        lease_fd = _acquire_namespace_lease(exclusive=False)
        connection = _SystemdConnection()
        read_fd, write_fd = os.pipe()
        start_attempted = True
        connection.start_transient(
            unit_name,
            arguments=["/usr/bin/sleep", "0.3"],
            executable="/usr/bin/sleep",
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin"},
            stdout_fd=write_fd,
            stderr_fd=write_fd,
            description=unit_description(probe_run, command_id),
            resource_policy=canonical,
            resource_workspace=workspace,
        )
        deadline = time.monotonic() + OPERATION_TIMEOUT
        verification = VerificationResult(VerificationStatus.NOT_RUNNING, "resource probe was not observable")
        while time.monotonic() < deadline:
            snapshot = connection.snapshot(unit_name)
            if snapshot is not None:
                verification = _snapshot_matches_intent(snapshot, metadata)
                if verification.status is VerificationStatus.MATCH:
                    break
                if snapshot.command_complete:
                    break
            time.sleep(POLL_INTERVAL)
        if verification.status is not VerificationStatus.MATCH:
            raise ContainmentError(verification.reason)
        recovered = reconcile_probe()
        cleanup_gone = recovered.gone
        if not cleanup_gone:
            raise ContainmentCleanupError(
                f"resource probe cleanup could not be proved: "
                f"{recovered.error or 'absence could not be proved'}"
            )
        return {
            "backend": "systemd_cgroup",
            "available": True,
            "requested_controls": controls,
            "reason": "requested transient resource properties were verified",
        }
    except Exception as exc:
        cleanup_reason = ""
        if start_attempted and not cleanup_gone and connection is not None:
            recovered = reconcile_probe()
            cleanup_gone = recovered.gone
            cleanup_reason = recovered.error
        if start_attempted and not cleanup_gone:
            reason = _latch_probe_cleanup_blocker(
                f"{exc}; resource probe cleanup unresolved: {cleanup_reason or 'absence could not be proved'}"
            )
        else:
            reason = str(exc)
        return {
            "backend": "systemd_cgroup",
            "available": False,
            "requested_controls": controls,
            "reason": reason[:500],
        }
    finally:
        for fd in (write_fd, read_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            if connection is not None:
                connection.close()
        finally:
            if lease_fd >= 0:
                os.close(lease_fd)


def _wait_for_unit_disappearance(
    connection,
    *,
    unit_name: str,
    control_group: str,
    authorize_snapshot: Callable[[UnitSnapshot], VerificationResult],
    last_snapshot: UnitSnapshot | None,
    on_wait: Callable[[float], None] | None = None,
    on_snapshot: Callable[[UnitSnapshot], None] | None = None,
) -> ContainmentTermination:
    """Shared StopUnit completion proof for bound and provenance-only ownership."""
    deadline = time.monotonic() + OPERATION_TIMEOUT
    last = last_snapshot
    reset_attempted = False
    while time.monotonic() < deadline:
        try:
            snapshot = connection.snapshot(unit_name)
        except ContainmentError as exc:
            return ContainmentTermination(last.process_exit_code if last else None, False, False, str(exc))
        if snapshot is None:
            verification = VerificationResult(VerificationStatus.NOT_RUNNING, "transient unit does not exist")
        else:
            verification = authorize_snapshot(snapshot)
        # During unload systemd can briefly expose an inactive, empty unit
        # object with Transient=no. No further signal is sent in this phase.
        unloading_stub = bool(
            snapshot is not None
            and snapshot.active_state == "inactive"
            and not snapshot.control_group
            and not snapshot.transient
        )
        if (
            verification.status in {VerificationStatus.MISMATCH, VerificationStatus.UNVERIFIABLE}
            and not unloading_stub
        ):
            return ContainmentTermination(
                last.process_exit_code if last else None, False, False,
                f"containment identity changed while stopping: {verification.reason}",
            )
        if snapshot is None:
            try:
                gone = _cgroup_is_absent(control_group)
            except (ContainmentError, OSError) as exc:
                return ContainmentTermination(last.process_exit_code if last else None, False, False, str(exc))
            if gone:
                killed = bool(last and last.process_exit_code == -signal.SIGKILL)
                return ContainmentTermination(last.process_exit_code if last else None, killed, True)
        elif not unloading_stub:
            last = snapshot
            if callable(on_snapshot):
                on_snapshot(snapshot)
            if snapshot.active_state == "failed" and not snapshot.control_group and not reset_attempted:
                try:
                    connection.reset_failed(snapshot.unit_name)
                    reset_attempted = True
                except Exception as exc:
                    return ContainmentTermination(
                        last.process_exit_code, False, False,
                        f"failed unit could not be reset: {exc}",
                    )
        delay = min(POLL_INTERVAL, max(0.0, deadline - time.monotonic()))
        if callable(on_wait):
            on_wait(delay)
        elif delay:
            time.sleep(delay)
    return ContainmentTermination(
        last.process_exit_code if last else None,
        bool(last and last.process_exit_code == -signal.SIGKILL),
        False,
        "transient unit or cgroup remained after systemd termination",
    )


class SystemdCommandContainment:
    """One active command whose canonical owner is a transient service cgroup."""

    def __init__(self, connection, recorder, metadata, stdout, stderr):
        self.connection = connection
        self.recorder = recorder
        self.metadata = metadata
        self.stdout = stdout
        self.stderr = stderr
        self.last_snapshot: UnitSnapshot | None = None
        self._namespace_lease_fd = -1
        self._identity_clear_candidates = [metadata]
        self._resource_lock = threading.RLock()
        self._resource_event_fds: dict[int, str] = {}
        self._resource_event_stop = threading.Event()
        self._resource_event_thread: threading.Thread | None = None
        self.resource_observations = {
            "memory_peak_bytes": 0,
            "memory_events": {},
            "tasks_peak": 0,
            "tasks_denied": 0,
            "cpu_usage_usec": 0,
            "cpu_throttled_periods": 0,
            "cpu_throttled_usec": 0,
            "io_read_bytes": 0,
            "io_write_bytes": 0,
            "io_device_count": 0,
        }

    @staticmethod
    def _fd_key_values(fd: int) -> dict[str, int]:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            rows = os.read(fd, 4096).decode("ascii").splitlines()
            return {parts[0]: int(parts[1]) for row in rows if len(parts := row.split()) == 2}
        except (OSError, UnicodeDecodeError, ValueError):
            return {}

    def _record_event_values(self, kind: str, values: dict[str, int]) -> None:
        with self._resource_lock:
            if kind == "memory":
                previous = self.resource_observations["memory_events"]
                self.resource_observations["memory_events"] = {
                    key: max(previous.get(key, 0), value)
                    for key, value in values.items() if value
                }
            elif kind == "tasks":
                self.resource_observations["tasks_denied"] = max(
                    self.resource_observations["tasks_denied"], values.get("max", 0),
                )

    def _capture_resource_event_fd(self, fd: int, kind: str) -> None:
        with self._resource_lock:
            self._record_event_values(kind, self._fd_key_values(fd))

    def _start_resource_event_watchers(self, snapshot: UnitSnapshot) -> None:
        policy = normalize_policy(self.metadata.get("resource_limits"))
        requested = []
        if policy["memory_max_bytes"] is not None:
            requested.append(("memory.events", "memory"))
        if policy["tasks_max"] is not None:
            requested.append(("pids.events", "tasks"))
        if not requested:
            return
        if not snapshot.control_group:
            if snapshot.command_complete:
                return
            raise ContainmentError("required resource event cgroup is not inspectable")
        root = CGROUP_ROOT / snapshot.control_group.removeprefix("/")
        poller = select.poll()
        try:
            for name, kind in requested:
                fd = os.open(root / name, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                self._resource_event_fds[fd] = kind
                self._capture_resource_event_fd(fd, kind)
                poller.register(fd, select.POLLPRI | select.POLLERR)
        except BaseException:
            for fd in self._resource_event_fds:
                os.close(fd)
            self._resource_event_fds.clear()
            raise

        ready = threading.Event()

        def watch() -> None:
            ready.set()
            while not self._resource_event_stop.is_set():
                for fd, _event in poller.poll(100):
                    kind = self._resource_event_fds.get(fd)
                    if kind:
                        self._capture_resource_event_fd(fd, kind)

        thread = threading.Thread(
            target=watch,
            name=f"resource-events-{self.metadata['command_id'][:8]}",
            daemon=True,
        )
        thread.start()
        self._resource_event_thread = thread
        if not ready.wait(1.0):
            raise ContainmentError("required resource event watcher did not start")

    def _stop_resource_event_watchers(self) -> None:
        self._resource_event_stop.set()
        if self._resource_event_thread is not None:
            self._resource_event_thread.join(timeout=1.0)
        self._capture_resource_event_fds()
        for fd in self._resource_event_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._resource_event_fds.clear()

    def _capture_resource_event_fds(self) -> None:
        """Synchronously retain terminal counters from already-authorized cgroup files."""
        for fd, kind in list(self._resource_event_fds.items()):
            self._capture_resource_event_fd(fd, kind)

    @staticmethod
    def _key_values(path: Path) -> dict[str, int]:
        try:
            rows = path.read_text(encoding="ascii").splitlines()
            return {parts[0]: int(parts[1]) for row in rows if len(parts := row.split()) == 2}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _sample_resources(self, snapshot: UnitSnapshot | None) -> None:
        with self._resource_lock:
            self._sample_resources_unlocked(snapshot)

    def _sample_resources_unlocked(self, snapshot: UnitSnapshot | None) -> None:
        if snapshot is not None:
            counters = {
                "memory_peak_bytes": snapshot.memory_peak,
                "tasks_peak": snapshot.tasks_current,
                "cpu_usage_usec": snapshot.cpu_usage_nsec // 1000,
                "io_read_bytes": snapshot.io_read_bytes,
                "io_write_bytes": snapshot.io_write_bytes,
            }
            for key, value in counters.items():
                # systemd represents an unavailable accounting counter as
                # UINT64_MAX; it is not an observed amount.
                if 0 <= value < (1 << 64) - 1:
                    self.resource_observations[key] = max(self.resource_observations[key], value)
        control_group = str((snapshot.control_group if snapshot else "") or self.metadata.get("control_group") or "")
        if not control_group:
            return
        root = CGROUP_ROOT / control_group.removeprefix("/")
        memory = self._key_values(root / "memory.events")
        if memory:
            previous = self.resource_observations["memory_events"]
            self.resource_observations["memory_events"] = {
                key: max(previous.get(key, 0), value) for key, value in memory.items() if value
            }
        pids = self._key_values(root / "pids.events")
        self.resource_observations["tasks_denied"] = max(self.resource_observations["tasks_denied"], pids.get("max", 0))
        cpu = self._key_values(root / "cpu.stat")
        self.resource_observations["cpu_usage_usec"] = max(self.resource_observations["cpu_usage_usec"], cpu.get("usage_usec", 0))
        self.resource_observations["cpu_throttled_periods"] = max(self.resource_observations["cpu_throttled_periods"], cpu.get("nr_throttled", 0))
        self.resource_observations["cpu_throttled_usec"] = max(self.resource_observations["cpu_throttled_usec"], cpu.get("throttled_usec", 0))
        try:
            io_rows = (root / "io.stat").read_text(encoding="ascii").splitlines()
            totals = {"rbytes": 0, "wbytes": 0}
            for row in io_rows:
                fields = row.split()
                for item in fields[1:]:
                    key, separator, raw = item.partition("=")
                    if separator and key in totals:
                        totals[key] += int(raw)
            self.resource_observations["io_read_bytes"] = max(self.resource_observations["io_read_bytes"], totals["rbytes"])
            self.resource_observations["io_write_bytes"] = max(self.resource_observations["io_write_bytes"], totals["wbytes"])
            self.resource_observations["io_device_count"] = max(self.resource_observations["io_device_count"], len(io_rows))
        except (FileNotFoundError, OSError, ValueError):
            pass

    def resource_control(self, *, verification: str = "verified") -> dict:
        # Poll notifications are advisory; synchronously consume the pinned
        # descriptors before reporting so a fast cgroup removal cannot race
        # the last resource-limit evidence into oblivion.
        self._capture_resource_event_fds()
        self._sample_resources(self.last_snapshot)
        policy = normalize_policy(self.metadata.get("resource_limits"))
        with self._resource_lock:
            observations = {
                **self.resource_observations,
                "memory_events": dict(self.resource_observations["memory_events"]),
            }
        outcome = None
        memory_events = observations.get("memory_events") or {}
        if any(memory_events.get(key, 0) for key in ("max", "oom", "oom_kill", "oom_group_kill")) or (self.last_snapshot and self.last_snapshot.result == "oom-kill"):
            outcome = {"code": "command_resource_limit_exceeded", "control": "memory_max_bytes"}
        elif observations.get("tasks_denied", 0):
            outcome = {"code": "command_resource_limit_exceeded", "control": "tasks_max"}
        if verification != "verified":
            outcome = {"code": "resource_limit_enforcement_failed", "control": None}
        return {
            "backend": "systemd_cgroup",
            "enforcement_required": enforcement_required(policy),
            "verification": verification,
            "requested": policy,
            "unit_result": self.last_snapshot.result if self.last_snapshot else "",
            "observations": observations,
            "outcome": outcome,
        }

    @classmethod
    def start(
        cls,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        recorder: IdentityRecorder,
        command_id: str,
        resource_policy: dict | None = None,
        resource_workspace: Path | None = None,
    ) -> "SystemdCommandContainment":
        if recorder.update is None:
            raise ContainmentError("durable containment metadata updates are unavailable")
        executable = _resolve_executable(arguments[0], cwd=cwd, environment=environment)
        policy = normalize_policy(resource_policy if resource_policy is not None else recorder.resource_policy)
        finite_policy = enforcement_required(policy)
        intent = starting_metadata(recorder.run_id, command_id, policy, workspace=resource_workspace or cwd)
        connection = None
        stdout_read = stdout_write = stderr_read = stderr_write = gate_read = gate_write = -1
        lease_fd = _acquire_namespace_lease(exclusive=False)
        try:
            recorder.record(intent)
        except BaseException:
            os.close(lease_fd)
            raise
        try:
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            launch_arguments = arguments
            launch_executable = executable
            if finite_policy:
                gate_read, gate_write = os.pipe()
                launch_executable = "/usr/bin/python3"
                launch_arguments = [
                    "/usr/bin/python3", str(COMMAND_LAUNCHER), "--", *arguments,
                ]
            connection = _SystemdConnection()
            process_environment = {
                **environment,
                RUN_ID_ENV: recorder.run_id,
                COMMAND_ID_ENV: command_id,
            }
            connection.start_transient(
                intent["unit_name"], arguments=launch_arguments, executable=launch_executable,
                cwd=cwd, environment=process_environment,
                stdout_fd=stdout_write, stderr_fd=stderr_write,
                description=unit_description(recorder.run_id, command_id),
                resource_policy=policy,
                resource_workspace=resource_workspace or cwd,
                stdin_fd=gate_read if finite_policy else None,
            )
            if gate_read >= 0:
                os.close(gate_read)
                gate_read = -1
            os.close(stdout_write)
            stdout_write = -1
            os.close(stderr_write)
            stderr_write = -1
            deadline = time.monotonic() + OPERATION_TIMEOUT
            snapshot = None
            verification = None
            while time.monotonic() < deadline:
                snapshot, verification = verify_unit(connection, intent)
                if (
                    snapshot is not None
                    and verification.status is VerificationStatus.MATCH
                    and snapshot.invocation_id
                    and (snapshot.control_group or snapshot.command_complete)
                ):
                    break
                if verification.status is not VerificationStatus.NOT_RUNNING:
                    if verification.status is not VerificationStatus.MATCH or (snapshot and snapshot.command_complete):
                        break
                time.sleep(POLL_INTERVAL)
            if snapshot is None or verification is None or verification.status is not VerificationStatus.MATCH:
                reason = verification.reason if verification else "transient unit activation was not observable"
                raise ContainmentError(reason)
            if not snapshot.invocation_id or not (snapshot.control_group or snapshot.command_complete):
                raise ContainmentError("transient unit activation identity is incomplete")
            active = {
                **intent,
                "containment_state": "active",
                "invocation_id": snapshot.invocation_id,
                # A fast command can empty its cgroup before the first
                # snapshot.  The canonical path is deterministic from the
                # already-authenticated exact unit name and is still required
                # for disappearance proof and persisted recovery.
                "control_group": snapshot.control_group or expected_control_group(intent["unit_name"]),
            }
            if recorder.update(intent, active) is not True:
                raise ContainmentError("activated containment metadata could not replace its exact starting intent")
            stdout = os.fdopen(stdout_read, "rb", buffering=0)
            stdout_read = -1
            stderr = os.fdopen(stderr_read, "rb", buffering=0)
            stderr_read = -1
            command = cls(connection, recorder, active, stdout, stderr)
            command.last_snapshot = snapshot
            command._sample_resources(snapshot)
            command._start_resource_event_watchers(snapshot)
            if finite_policy and not snapshot.command_complete:
                if os.write(gate_write, b"1") != 1:
                    raise ContainmentError("finite-policy command launch gate could not be released")
                os.close(gate_write)
                gate_write = -1
            command._namespace_lease_fd = lease_fd
            lease_fd = -1
            return command
        except BaseException as original:
            # A failed D-Bus call is ambiguous: the precommitted intent remains
            # for Checkpoint 4 unless this process can prove and clear cleanup.
            if "command" in locals():
                command._stop_resource_event_watchers()
            cleanup_error = ""
            if connection is not None:
                try:
                    cleanup_candidates = [active, intent] if "active" in locals() else [intent]
                    cleanup_record = cleanup_candidates[0]
                    temporary = command if "command" in locals() else cls(connection, recorder, cleanup_record, None, None)
                    terminated = temporary._terminate_record(cleanup_record, persist_stopping=False)
                    if terminated.gone:
                        cleared = False
                        clear_errors = []
                        for candidate in cleanup_candidates:
                            try:
                                if recorder.clear(candidate) is True:
                                    cleared = True
                                    break
                            except Exception as exc:
                                clear_errors.append(str(exc) or type(exc).__name__)
                        if not cleared:
                            cleanup_error = "active containment metadata could not be cleared exactly"
                            if clear_errors:
                                cleanup_error += f": {'; '.join(clear_errors)}"
                    else:
                        cleanup_error = terminated.error or "transient containment disappearance was not proved"
                except Exception as exc:
                    cleanup_error = str(exc) or type(exc).__name__
                if "command" in locals():
                    for stream in (command.stdout, command.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception:
                                pass
                connection.close()
            else:
                # No systemd connection means StartTransientUnit was never
                # attempted, so the precommitted intent cannot own a workload.
                try:
                    recorder.clear(intent)
                except Exception:
                    pass
            if cleanup_error:
                raise ContainmentCleanupError(
                    f"{original}; containment cleanup failed: {cleanup_error}"
                ) from original
            raise original
        finally:
            for fd in (stdout_write, stderr_write, stdout_read, stderr_read, gate_read, gate_write):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if lease_fd >= 0:
                os.close(lease_fd)

    def poll(self) -> int | None:
        snapshot, verification = verify_unit(self.connection, self.metadata)
        if verification.status is not VerificationStatus.MATCH:
            raise ContainmentError(f"active containment verification failed: {verification.reason}")
        self.last_snapshot = snapshot
        self._sample_resources(snapshot)
        if snapshot is not None and snapshot.command_complete:
            return snapshot.process_exit_code
        return None

    def _wait_for_disappearance(self, *, on_wait: Callable[[float], None] | None = None) -> ContainmentTermination:
        control_group = self.metadata.get("control_group") or expected_control_group(self.metadata["unit_name"])
        pinned_invocation = self.last_snapshot.invocation_id if self.last_snapshot is not None else ""

        def observe(snapshot: UnitSnapshot) -> None:
            self.last_snapshot = snapshot
            self._sample_resources(snapshot)

        def authorize(snapshot: UnitSnapshot) -> VerificationResult:
            checked = _snapshot_matches_ownership_metadata(snapshot, self.metadata)
            if checked.status is VerificationStatus.MATCH and pinned_invocation and snapshot.invocation_id != pinned_invocation:
                return VerificationResult(
                    VerificationStatus.MISMATCH,
                    "transient unit invocation changed while stopping",
                )
            return checked

        return _wait_for_unit_disappearance(
            self.connection,
            unit_name=self.metadata["unit_name"],
            control_group=control_group,
            authorize_snapshot=authorize,
            last_snapshot=self.last_snapshot,
            on_wait=on_wait,
            on_snapshot=observe,
        )

    def _terminate_record(self, record: dict, *, persist_stopping: bool, on_wait=None) -> ContainmentTermination:
        self.metadata = record
        snapshot, verification = verify_unit(self.connection, record, verify_resources=False)
        if verification.status is VerificationStatus.NOT_RUNNING:
            control_group = record.get("control_group") or expected_control_group(record["unit_name"])
            try:
                gone = _cgroup_is_absent(control_group)
            except (ContainmentError, OSError) as exc:
                return ContainmentTermination(None, False, False, str(exc))
            return ContainmentTermination(None, False, gone, "" if gone else "transient cgroup remains without its unit")
        if verification.status is not VerificationStatus.MATCH or snapshot is None:
            return ContainmentTermination(None, False, False, f"containment termination was not authorized: {verification.reason}")
        self.last_snapshot = snapshot
        self._sample_resources(snapshot)
        transition_error = ""
        if persist_stopping and record.get("containment_state") == "active":
            stopping = {**record, "containment_state": "stopping"}
            update_raised = False
            try:
                updated = self.recorder.update(record, stopping)
            except Exception as exc:
                # The atomic replace may have committed before a durability
                # error was reported. Both exact records remain candidates,
                # but immutable unit/invocation ownership still authorizes
                # termination of this one cgroup.
                updated = None
                update_raised = True
                transition_error = f"stopping identity update was ambiguous: {exc}"
                self._identity_clear_candidates = [stopping, record]
            if not update_raised and updated is not True:
                return ContainmentTermination(snapshot.process_exit_code, False, False, "active containment metadata changed before termination")
            self.metadata = stopping
            if updated is True:
                self._identity_clear_candidates = [stopping]
            snapshot, verification = verify_unit(self.connection, stopping, verify_resources=False)
            if verification.status is not VerificationStatus.MATCH or snapshot is None:
                return ContainmentTermination(
                    self.last_snapshot.process_exit_code if self.last_snapshot else None,
                    False,
                    False,
                    f"containment identity changed after persisting stop intent: {verification.reason}",
                )
            self.last_snapshot = snapshot
        try:
            self.connection.stop(record["unit_name"])
        except Exception as exc:
            error = f"systemd StopUnit failed: {exc}"
            if transition_error:
                error = f"{transition_error}; {error}"
            return ContainmentTermination(snapshot.process_exit_code, False, False, error)
        terminated = self._wait_for_disappearance(on_wait=on_wait)
        if transition_error:
            return ContainmentTermination(
                terminated.process_exit_code,
                terminated.killed,
                terminated.gone,
                "; ".join(filter(None, (transition_error, terminated.error))),
                terminated.enforcement_error,
            )
        return terminated

    def _clear_owned_identity_after_disappearance(self) -> str:
        errors = []
        for candidate in self._identity_clear_candidates:
            try:
                if self.recorder.clear(candidate) is True:
                    return ""
            except Exception as exc:
                errors.append(str(exc) or type(exc).__name__)
        detail = f": {'; '.join(errors)}" if errors else ""
        return f"active containment metadata could not be cleared exactly{detail}"

    def terminate(self, *, on_wait=None) -> ContainmentTermination:
        return self._terminate_record(self.metadata, persist_stopping=True, on_wait=on_wait)

    def finish(self, *, on_wait=None) -> ContainmentTermination:
        """Remove a completed retained unit, prove cgroup disappearance, then clear metadata."""
        snapshot, verification = verify_unit(self.connection, self.metadata)
        if verification.status is not VerificationStatus.MATCH or snapshot is None:
            owned_snapshot, ownership = verify_unit(self.connection, self.metadata, verify_resources=False)
            if ownership.status is VerificationStatus.MATCH and owned_snapshot is not None:
                self.last_snapshot = owned_snapshot
                cleaned = self._terminate_record(self.metadata, persist_stopping=False, on_wait=on_wait)
                clear_error = self._clear_owned_identity_after_disappearance() if cleaned.gone else ""
                if clear_error:
                    return ContainmentTermination(
                        cleaned.process_exit_code,
                        cleaned.killed,
                        False,
                        clear_error,
                    )
                return ContainmentTermination(
                    cleaned.process_exit_code, cleaned.killed, cleaned.gone,
                    cleaned.error,
                    f"completed resource enforcement could not be verified: {verification.reason}",
                )
            return ContainmentTermination(None, False, False, f"completed containment could not be verified: {verification.reason}")
        if not snapshot.command_complete:
            return ContainmentTermination(snapshot.process_exit_code, False, False, "transient service is not complete")
        self.last_snapshot = snapshot
        result = self._terminate_record(self.metadata, persist_stopping=False, on_wait=on_wait)
        clear_error = self._clear_owned_identity_after_disappearance() if result.gone else ""
        if clear_error:
            return ContainmentTermination(result.process_exit_code, result.killed, False, clear_error)
        return result

    def clear_after_termination(self, result: ContainmentTermination) -> ContainmentTermination:
        if not result.gone:
            return result
        clear_error = self._clear_owned_identity_after_disappearance()
        if clear_error:
            return ContainmentTermination(result.process_exit_code, result.killed, False, clear_error)
        return result

    def close(self) -> None:
        self._stop_resource_event_watchers()
        for stream_name in ("stdout", "stderr"):
            stream = getattr(self, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                setattr(self, stream_name, None)
        try:
            self.connection.close()
        finally:
            if self._namespace_lease_fd >= 0:
                os.close(self._namespace_lease_fd)
                self._namespace_lease_fd = -1
