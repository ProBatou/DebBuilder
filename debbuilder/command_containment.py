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
import os
import re
import shutil
import signal
import stat
import sys
import threading
import time
from dataclasses import dataclass
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


SYSTEMD_DESTINATION = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
UNIT_INTERFACE = "org.freedesktop.systemd1.Unit"
SERVICE_INTERFACE = "org.freedesktop.systemd1.Service"
CGROUP_ROOT = Path("/sys/fs/cgroup")
UNIT_PREFIX = "debbuilder-command-"
STOP_TIMEOUT_USEC = 200_000
OPERATION_TIMEOUT = 3.0
POLL_INTERVAL = 0.02


class ContainmentError(RuntimeError):
    """Strong containment could not be established or proved empty."""


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


@dataclass(frozen=True)
class ContainmentRecovery:
    """Authoritative result of reconciling one historical systemd command."""

    verification: VerificationResult
    signalled: bool = False
    killed: bool = False
    gone: bool = False


_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY: ContainmentCapability | None = None


def command_unit_name(run_id: str, command_id: str) -> str:
    """Derive a bounded unit name without embedding the user-controlled Run ID."""
    if not isinstance(run_id, str) or not run_id:
        raise ContainmentError("Run ID is unavailable for systemd containment")
    if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f]{32}", command_id):
        raise ContainmentError("command ID is invalid for systemd containment")
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"{UNIT_PREFIX}{run_digest}-{command_id}.service"


def expected_control_group(unit_name: str) -> str:
    if not re.fullmatch(r"debbuilder-command-[0-9a-f]{16}-[0-9a-f]{32}\.service", unit_name):
        raise ContainmentError("transient unit name is invalid")
    return f"/system.slice/{unit_name}"


def unit_description(run_id: str, command_id: str) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"DebBuilder command {digest}/{command_id}"


def starting_metadata(run_id: str, command_id: str) -> dict:
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "backend": "systemd_cgroup",
        "boot_id": _read_boot_id(),
        "run_id": run_id,
        "command_id": command_id,
        "unit_name": command_unit_name(run_id, command_id),
        "containment_state": "starting",
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
            ("KillMode", dbus.String("control-group")),
            ("KillSignal", dbus.Int32(signal.SIGTERM)),
            ("SendSIGKILL", dbus.Boolean(True)),
            ("FinalKillSignal", dbus.Int32(signal.SIGKILL)),
            ("TimeoutStopUSec", dbus.UInt64(STOP_TIMEOUT_USEC)),
            # Failed units must remain inspectable until their exit status is
            # consumed; ResetFailedUnit then permits collection.
            ("CollectMode", dbus.String("inactive")),
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
                unit_name=unit_name,
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
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContainmentError("transient unit returned incomplete properties") from exc

    def stop(self, unit_name: str) -> None:
        self.manager.StopUnit(unit_name, "replace")

    def reset_failed(self, unit_name: str) -> None:
        self.manager.ResetFailedUnit(unit_name)

    def command_units(self) -> set[str]:
        """List loaded units in our namespace for diagnostics, never signalling."""
        import dbus

        try:
            rows = self.manager.ListUnitsByPatterns(
                dbus.Array([], signature="s"),
                dbus.Array([f"{UNIT_PREFIX}*.service"], signature="s"),
            )
        except Exception as exc:
            raise ContainmentError(f"DebBuilder transient units could not be listed: {exc}") from exc
        names = {
            str(row[0]) for row in rows
            if row and str(row[0]).startswith(UNIT_PREFIX) and str(row[0]).endswith(".service")
        }
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


def _snapshot_matches_intent(snapshot: UnitSnapshot, metadata: dict) -> VerificationResult:
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
    return VerificationResult(VerificationStatus.MATCH, "precommitted transient unit properties match")


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


def verify_unit(connection: _SystemdConnection, metadata: dict) -> tuple[UnitSnapshot | None, VerificationResult]:
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
        return snapshot, _snapshot_matches_metadata(snapshot, metadata)
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
        snapshot, verification = verify_unit(connection, value)
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

        command = SystemdCommandContainment(connection, recorder, value, None, None)
        command.last_snapshot = snapshot
        terminated = command._terminate_record(
            value,
            persist_stopping=value.get("containment_state") == "active",
            on_wait=on_wait,
        )
        if terminated.gone:
            return ContainmentRecovery(
                VerificationResult(VerificationStatus.NOT_RUNNING, "transient service and cgroup are absent"),
                signalled=True,
                killed=terminated.killed,
                gone=True,
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


def loaded_command_units(*, connection_factory=_SystemdConnection) -> set[str]:
    """Return loaded/cgroup namespace members for conservative diagnostics."""
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
                    if not entry.name.startswith(UNIT_PREFIX) or not entry.name.endswith(".service"):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        cgroup_units.add(entry.name)
                    else:
                        raise ContainmentError(
                            f"DebBuilder command cgroup has an unexpected filesystem type: {entry.name}"
                        )
    except OSError as exc:
        raise ContainmentError(f"DebBuilder command cgroups could not be listed: {exc}") from exc

    connection = None
    try:
        connection = connection_factory()
        return cgroup_units | connection.command_units()
    except Exception:
        # A present cgroup is sufficient to block safely.  If no cgroup is
        # present, a missing systemd manager cannot conceal a live strong
        # workload on the current boot because that workload's cgroup is its
        # authoritative ownership boundary.
        return cgroup_units
    finally:
        if connection is not None:
            connection.close()


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
    try:
        connection = _SystemdConnection()
        read_fd, write_fd = os.pipe()
        # Use the same required property set and FD transport as real commands.
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
        synthetic = {
            "run_id": probe_run, "command_id": command_id, "unit_name": unit_name,
        }
        matched = _snapshot_matches_intent(snapshot, synthetic)
        if matched.status is not VerificationStatus.MATCH:
            raise ContainmentError(matched.reason)
        if snapshot.control_group != control_group:
            raise ContainmentError("transient-service probe did not enter the expected cgroup")
        events = CGROUP_ROOT / control_group.removeprefix("/") / "cgroup.events"
        if not events.is_file() or "populated " not in events.read_text(encoding="ascii"):
            raise ContainmentError("transient-service cgroup lifecycle is not inspectable")
        connection.stop(unit_name)
        deadline = time.monotonic() + OPERATION_TIMEOUT
        while time.monotonic() < deadline:
            current = connection.snapshot(unit_name)
            if current is None and _cgroup_is_absent(control_group):
                return ContainmentCapability("systemd_cgroup", True, "systemd transient-service cgroup probe succeeded")
            if current is not None and current.active_state == "failed" and not current.control_group:
                connection.reset_failed(unit_name)
            time.sleep(POLL_INTERVAL)
        raise ContainmentError("transient-service probe could not prove cgroup disappearance")
    except Exception as exc:
        if connection is not None:
            try:
                connection.stop(unit_name)
            except Exception:
                pass
            try:
                connection.reset_failed(unit_name)
            except Exception:
                pass
        return ContainmentCapability("process_group", False, str(exc))
    finally:
        for fd in (write_fd, read_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if connection is not None:
            connection.close()


def containment_capability(*, refresh: bool = False) -> ContainmentCapability:
    global _CAPABILITY
    with _CAPABILITY_LOCK:
        if refresh or _CAPABILITY is None:
            _CAPABILITY = _probe_capability()
        return _CAPABILITY


class SystemdCommandContainment:
    """One active command whose canonical owner is a transient service cgroup."""

    def __init__(self, connection, recorder, metadata, stdout, stderr):
        self.connection = connection
        self.recorder = recorder
        self.metadata = metadata
        self.stdout = stdout
        self.stderr = stderr
        self.last_snapshot: UnitSnapshot | None = None

    @classmethod
    def start(
        cls,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        recorder: IdentityRecorder,
        command_id: str,
    ) -> "SystemdCommandContainment":
        if recorder.update is None:
            raise ContainmentError("durable containment metadata updates are unavailable")
        executable = _resolve_executable(arguments[0], cwd=cwd, environment=environment)
        intent = starting_metadata(recorder.run_id, command_id)
        recorder.record(intent)
        connection = None
        stdout_read = stdout_write = stderr_read = stderr_write = -1
        try:
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            connection = _SystemdConnection()
            process_environment = {
                **environment,
                RUN_ID_ENV: recorder.run_id,
                COMMAND_ID_ENV: command_id,
            }
            connection.start_transient(
                intent["unit_name"], arguments=arguments, executable=executable,
                cwd=cwd, environment=process_environment,
                stdout_fd=stdout_write, stderr_fd=stderr_write,
                description=unit_description(recorder.run_id, command_id),
            )
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
            return command
        except BaseException as original:
            # A failed D-Bus call is ambiguous: the precommitted intent remains
            # for Checkpoint 4 unless this process can prove and clear cleanup.
            if connection is not None:
                try:
                    temporary = cls(connection, recorder, intent, None, None)
                    terminated = temporary._terminate_record(intent, persist_stopping=False)
                    if terminated.gone:
                        recorder.clear(intent)
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
            raise original
        finally:
            for fd in (stdout_write, stderr_write, stdout_read, stderr_read):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def poll(self) -> int | None:
        snapshot, verification = verify_unit(self.connection, self.metadata)
        if verification.status is not VerificationStatus.MATCH:
            raise ContainmentError(f"active containment verification failed: {verification.reason}")
        self.last_snapshot = snapshot
        if snapshot is not None and snapshot.command_complete:
            return snapshot.process_exit_code
        return None

    def _wait_for_disappearance(self, *, on_wait: Callable[[float], None] | None = None) -> ContainmentTermination:
        deadline = time.monotonic() + OPERATION_TIMEOUT
        last = self.last_snapshot
        reset_attempted = False
        while time.monotonic() < deadline:
            try:
                snapshot = self.connection.snapshot(self.metadata["unit_name"])
            except ContainmentError as exc:
                return ContainmentTermination(last.process_exit_code if last else None, False, False, str(exc))
            if snapshot is None:
                verification = VerificationResult(VerificationStatus.NOT_RUNNING, "transient unit does not exist")
            else:
                verification = _snapshot_matches_metadata(snapshot, self.metadata)
            # During unload systemd can briefly expose an inactive, empty unit
            # object with Transient=no.  No further signal is sent in this
            # phase; wait for disappearance and the cgroup proof.  A populated
            # or active mismatching unit is still a hard unit-reuse failure.
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
                    control_group = self.metadata.get("control_group") or expected_control_group(self.metadata["unit_name"])
                    gone = _cgroup_is_absent(control_group)
                except (ContainmentError, OSError) as exc:
                    return ContainmentTermination(last.process_exit_code if last else None, False, False, str(exc))
                if gone:
                    killed = bool(last and last.process_exit_code == -signal.SIGKILL)
                    return ContainmentTermination(last.process_exit_code if last else None, killed, True)
            elif not unloading_stub:
                last = snapshot
                self.last_snapshot = snapshot
                if snapshot.active_state == "failed" and not snapshot.control_group and not reset_attempted:
                    try:
                        self.connection.reset_failed(snapshot.unit_name)
                        reset_attempted = True
                    except Exception as exc:
                        return ContainmentTermination(last.process_exit_code, False, False, f"failed unit could not be reset: {exc}")
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

    def _terminate_record(self, record: dict, *, persist_stopping: bool, on_wait=None) -> ContainmentTermination:
        self.metadata = record
        snapshot, verification = verify_unit(self.connection, record)
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
        if persist_stopping and record.get("containment_state") == "active":
            stopping = {**record, "containment_state": "stopping"}
            if self.recorder.update(record, stopping) is not True:
                return ContainmentTermination(snapshot.process_exit_code, False, False, "active containment metadata changed before termination")
            self.metadata = stopping
            snapshot, verification = verify_unit(self.connection, stopping)
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
            return ContainmentTermination(snapshot.process_exit_code, False, False, f"systemd StopUnit failed: {exc}")
        return self._wait_for_disappearance(on_wait=on_wait)

    def terminate(self, *, on_wait=None) -> ContainmentTermination:
        return self._terminate_record(self.metadata, persist_stopping=True, on_wait=on_wait)

    def finish(self, *, on_wait=None) -> ContainmentTermination:
        """Remove a completed retained unit, prove cgroup disappearance, then clear metadata."""
        snapshot, verification = verify_unit(self.connection, self.metadata)
        if verification.status is not VerificationStatus.MATCH or snapshot is None:
            return ContainmentTermination(None, False, False, f"completed containment could not be verified: {verification.reason}")
        if not snapshot.command_complete:
            return ContainmentTermination(snapshot.process_exit_code, False, False, "transient service is not complete")
        self.last_snapshot = snapshot
        result = self._terminate_record(self.metadata, persist_stopping=False, on_wait=on_wait)
        if result.gone and self.recorder.clear(self.metadata) is not True:
            return ContainmentTermination(result.process_exit_code, result.killed, False, "active containment metadata could not be cleared exactly")
        return result

    def clear_after_termination(self, result: ContainmentTermination) -> ContainmentTermination:
        if not result.gone:
            return result
        if self.recorder.clear(self.metadata) is not True:
            return ContainmentTermination(result.process_exit_code, result.killed, False, "active containment metadata could not be cleared exactly")
        return result

    def close(self) -> None:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(self, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                setattr(self, stream_name, None)
        self.connection.close()
