"""Durable Linux identity for one active command in a Build Run."""
from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .recipe_schema import require_safe_name


ACTIVE_COMMAND_FILE = ".active-command.json"
IDENTITY_SCHEMA_VERSION = 2
LEGACY_IDENTITY_SCHEMA_VERSION = 1
MAX_IDENTITY_BYTES = 4096
MAX_ENVIRON_BYTES = 1024 * 1024
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PROC_ROOT = Path("/proc")
RUN_ID_ENV = "DEBBUILDER_INTERNAL_RUN_ID"
COMMAND_ID_ENV = "DEBBUILDER_INTERNAL_COMMAND_ID"
BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class CommandIdentityError(RuntimeError):
    """Active command identity could not be captured or persisted safely."""


class VerificationStatus(str, Enum):
    MATCH = "match"
    NOT_RUNNING = "not_running"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    reason: str


@dataclass(frozen=True)
class GroupTerminationResult:
    verification: VerificationResult
    signalled: bool = False
    killed: bool = False
    group_gone: bool = False
    error: str = ""


@dataclass(frozen=True)
class IdentityRecorder:
    run_id: str
    record: Callable[[dict], None]
    clear: Callable[[dict], object]
    update: Callable[[dict, dict], object] | None = None


_RECORDER: ContextVar[IdentityRecorder | None] = ContextVar("debbuilder_command_identity", default=None)


@contextmanager
def recording_identities(
    run_id: str,
    *,
    record: Callable[[dict], None],
    clear: Callable[[dict], object],
    update: Callable[[dict, dict], object] | None = None,
):
    """Associate command_runner calls in this execution context with one Run."""
    require_safe_name(run_id, "build run id")
    if not callable(record) or not callable(clear) or (update is not None and not callable(update)):
        raise TypeError("command identity record and clear callbacks must be callable")
    token = _RECORDER.set(IdentityRecorder(run_id, record, clear, update))
    try:
        yield
    finally:
        _RECORDER.reset(token)


def current_recorder() -> IdentityRecorder | None:
    return _RECORDER.get()


def _read_boot_id(path: Path = BOOT_ID_PATH) -> str:
    value = path.read_text(encoding="ascii").strip().lower()
    if not BOOT_ID.fullmatch(value):
        raise CommandIdentityError("Linux boot ID is malformed")
    return value


def current_boot_id() -> str:
    """Return the validated Linux boot identity used by recovery decisions."""
    return _read_boot_id()


def validated_identity(value, *, expected_run_id: str | None = None) -> dict:
    """Validate persisted identity metadata without inspecting a live workload."""
    return _validated_identity(value, expected_run_id=expected_run_id)


def _read_proc_stat(pid: int, proc_root: Path = PROC_ROOT) -> tuple[int, int]:
    """Return (PGID, starttime ticks), safely ignoring spaces/parentheses in comm."""
    if type(pid) is not int or pid <= 0:
        raise CommandIdentityError("process PID must be a positive integer")
    text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    opening = text.find("(")
    closing = text.rfind(")")
    if opening <= 0 or closing <= opening or text[closing + 1:closing + 2] != " ":
        raise CommandIdentityError("Linux process stat is malformed")
    try:
        recorded_pid = int(text[:opening].strip())
        suffix = text[closing + 2:].split()
        # suffix[0] is field 3 (state), suffix[2] is field 5 (pgrp), and
        # suffix[19] is field 22 (starttime).
        pgid = int(suffix[2])
        start_time_ticks = int(suffix[19])
    except (IndexError, ValueError) as exc:
        raise CommandIdentityError("Linux process stat is malformed") from exc
    if recorded_pid != pid or pgid <= 0 or start_time_ticks < 0:
        raise CommandIdentityError("Linux process stat contains invalid identity fields")
    return pgid, start_time_ticks


def _read_proc_identity_environment(pid: int, proc_root: Path = PROC_ROOT) -> tuple[str, str]:
    path = proc_root / str(pid) / "environ"
    with path.open("rb") as handle:
        raw = handle.read(MAX_ENVIRON_BYTES + 1)
    if len(raw) > MAX_ENVIRON_BYTES:
        raise CommandIdentityError("Linux process environment is oversized")
    values = {}
    try:
        for item in raw.split(b"\0"):
            if not item or b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            if key in {RUN_ID_ENV.encode(), COMMAND_ID_ENV.encode()}:
                values[key.decode("ascii")] = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandIdentityError("Linux process identity environment is malformed") from exc
    return values.get(RUN_ID_ENV, ""), values.get(COMMAND_ID_ENV, "")


def capture_identity(pid: int, *, run_id: str, command_id: str, process_exited=None) -> dict:
    """Capture the durable identity of a freshly spawned process-group leader.

    Linux exposes an empty ``/proc/PID/environ`` once a very short-lived child
    becomes a zombie.  The caller may therefore confirm that its unreaped
    ``Popen`` child has exited after the stat fields were captured.  This does
    not authorize a signal: a later verification still requires the markers.
    """
    require_safe_name(run_id, "build run id")
    if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
        raise CommandIdentityError("command ID is invalid")
    pgid, start_time_ticks = _read_proc_stat(pid)
    if pgid != pid:
        raise CommandIdentityError("active command is not its process-group leader")
    process_run_id, process_command_id = _read_proc_identity_environment(pid)
    markers_match = (process_run_id, process_command_id) == (run_id, command_id)
    if not markers_match and not (callable(process_exited) and process_exited()):
        raise CommandIdentityError("active command process markers do not match")
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "backend": "process_group",
        "containment_state": "active",
        "pid": pid,
        "pgid": pgid,
        "start_time_ticks": start_time_ticks,
        "boot_id": _read_boot_id(),
        "run_id": run_id,
        "command_id": command_id,
    }


def _validated_identity(value, *, expected_run_id: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise CommandIdentityError("active command identity has invalid fields")
    legacy_fields = {
        "schema_version", "pid", "pgid", "start_time_ticks", "boot_id", "run_id", "command_id",
    }
    process_group_fields = legacy_fields | {"backend", "containment_state"}
    systemd_starting_fields = {
        "schema_version", "backend", "boot_id", "run_id", "command_id", "unit_name", "containment_state",
    }
    systemd_active_required = systemd_starting_fields | {"invocation_id", "control_group"}
    fields = set(value)
    schema_version = value.get("schema_version")
    if schema_version == LEGACY_IDENTITY_SCHEMA_VERSION and fields == legacy_fields:
        backend = "process_group"
    elif schema_version == IDENTITY_SCHEMA_VERSION and fields == process_group_fields:
        backend = value.get("backend")
    elif schema_version == IDENTITY_SCHEMA_VERSION and fields == systemd_starting_fields:
        backend = value.get("backend")
    elif (
        schema_version == IDENTITY_SCHEMA_VERSION
        and fields == systemd_active_required
    ):
        backend = value.get("backend")
    else:
        raise CommandIdentityError("active command identity has invalid fields")
    if schema_version not in {LEGACY_IDENTITY_SCHEMA_VERSION, IDENTITY_SCHEMA_VERSION}:
        raise CommandIdentityError("active command identity schema is unsupported")
    if backend not in {"process_group", "systemd_cgroup"}:
        raise CommandIdentityError("active command backend is invalid")
    if backend == "process_group":
        if schema_version == IDENTITY_SCHEMA_VERSION and value.get("containment_state") != "active":
            raise CommandIdentityError("process-group containment state is invalid")
        for field in ("pid", "pgid"):
            if type(value.get(field)) is not int or value[field] <= 0:
                raise CommandIdentityError(f"active command {field} is invalid")
        if type(value.get("start_time_ticks")) is not int or value["start_time_ticks"] < 0:
            raise CommandIdentityError("active command start time is invalid")
    else:
        if value.get("containment_state") not in {"starting", "active", "stopping"}:
            raise CommandIdentityError("systemd containment state is invalid")
        if not isinstance(value.get("unit_name"), str) or not re.fullmatch(
            r"debbuilder-command-[0-9a-f]{16}-[0-9a-f]{32}\.service", value["unit_name"],
        ):
            raise CommandIdentityError("systemd unit name is invalid")
        if value["containment_state"] == "starting":
            if fields != systemd_starting_fields:
                raise CommandIdentityError("starting systemd containment has invalid fields")
        else:
            if not isinstance(value.get("invocation_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["invocation_id"]):
                raise CommandIdentityError("systemd invocation ID is invalid")
            if not isinstance(value.get("control_group"), str) or not re.fullmatch(
                r"/system\.slice/debbuilder-command-[0-9a-f]{16}-[0-9a-f]{32}\.service", value["control_group"],
            ):
                raise CommandIdentityError("systemd control group is invalid")
    if not isinstance(value.get("boot_id"), str) or not BOOT_ID.fullmatch(value["boot_id"]):
        raise CommandIdentityError("active command boot ID is invalid")
    require_safe_name(value.get("run_id"), "build run id")
    if expected_run_id is not None and value["run_id"] != require_safe_name(expected_run_id, "build run id"):
        raise CommandIdentityError("active command belongs to a different Run")
    if not isinstance(value.get("command_id"), str) or not COMMAND_ID.fullmatch(value["command_id"]):
        raise CommandIdentityError("active command ID is invalid")
    return dict(value)


def verify_identity(value, *, expected_run_id: str | None = None) -> VerificationResult:
    """Fail closed while comparing untrusted metadata with the live Linux process."""
    try:
        identity = _validated_identity(value, expected_run_id=expected_run_id)
        current_boot_id = _read_boot_id()
    except (CommandIdentityError, OSError, TypeError, ValueError) as exc:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if identity.get("backend", "process_group") != "process_group":
        return VerificationResult(VerificationStatus.UNVERIFIABLE, "systemd containment requires canonical unit verification")
    if identity["pid"] != identity["pgid"]:
        return VerificationResult(VerificationStatus.MISMATCH, "recorded process is not the process-group leader")
    if identity["boot_id"] != current_boot_id:
        return VerificationResult(VerificationStatus.MISMATCH, "recorded boot ID does not match the current boot")
    try:
        pgid, start_time_ticks = _read_proc_stat(identity["pid"])
    except FileNotFoundError:
        return VerificationResult(VerificationStatus.NOT_RUNNING, "recorded process does not exist")
    except (CommandIdentityError, OSError) as exc:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if pgid != identity["pgid"]:
        return VerificationResult(VerificationStatus.MISMATCH, "recorded process group does not match")
    if start_time_ticks != identity["start_time_ticks"]:
        return VerificationResult(VerificationStatus.MISMATCH, "recorded process start time does not match")
    try:
        process_run_id, process_command_id = _read_proc_identity_environment(identity["pid"])
    except FileNotFoundError:
        return VerificationResult(VerificationStatus.NOT_RUNNING, "recorded process does not exist")
    except (CommandIdentityError, OSError) as exc:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if (process_run_id, process_command_id) != (identity["run_id"], identity["command_id"]):
        return VerificationResult(VerificationStatus.MISMATCH, "recorded Run and command markers do not match")
    try:
        final_pgid, final_start_time_ticks = _read_proc_stat(identity["pid"])
    except FileNotFoundError:
        return VerificationResult(VerificationStatus.NOT_RUNNING, "recorded process does not exist")
    except (CommandIdentityError, OSError) as exc:
        return VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if (final_pgid, final_start_time_ticks) != (pgid, start_time_ticks):
        return VerificationResult(VerificationStatus.MISMATCH, "process identity changed during verification")
    return VerificationResult(VerificationStatus.MATCH, "all durable process identity fields match")


def read_persisted_identity(workspace_fd: int) -> dict | None:
    """Read bounded, no-follow metadata relative to a locked Run workspace."""
    try:
        file_fd = os.open(ACTIVE_COMMAND_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=workspace_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_IDENTITY_BYTES:
            raise CommandIdentityError("active command metadata is unsafe or oversized")
        raw = handle.read(MAX_IDENTITY_BYTES + 1)
    if len(raw) > MAX_IDENTITY_BYTES:
        raise CommandIdentityError("active command metadata is oversized")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandIdentityError("active command metadata is malformed") from exc


def persist_identity(workspace_fd: int, identity: dict) -> None:
    """Atomically record one active command in its already-locked Run."""
    from .workspace_cleanup import write_json

    validated = _validated_identity(identity)
    if read_persisted_identity(workspace_fd) is not None:
        raise CommandIdentityError("Run already has an active command identity")
    write_json(workspace_fd, ACTIVE_COMMAND_FILE, validated)


def clear_identity(workspace_fd: int, identity: dict) -> bool:
    """Clear only the exact identity that this command recorded."""
    try:
        expected = _validated_identity(identity)
        current = _validated_identity(read_persisted_identity(workspace_fd), expected_run_id=expected["run_id"])
    except (CommandIdentityError, OSError, TypeError, ValueError):
        return False
    if current != expected:
        return False
    os.unlink(ACTIVE_COMMAND_FILE, dir_fd=workspace_fd)
    os.fsync(workspace_fd)
    return True


def update_identity(workspace_fd: int, expected: dict, updated: dict) -> bool:
    """Atomically replace only the exact active-command record we own."""
    from .workspace_cleanup import write_json

    try:
        expected_value = _validated_identity(expected)
        updated_value = _validated_identity(updated, expected_run_id=expected_value["run_id"])
        current = _validated_identity(read_persisted_identity(workspace_fd), expected_run_id=expected_value["run_id"])
    except (CommandIdentityError, OSError, TypeError, ValueError):
        return False
    if current != expected_value:
        return False
    invariant_fields = ("backend", "boot_id", "run_id", "command_id", "unit_name")
    if any(updated_value.get(field) != expected_value.get(field) for field in invariant_fields):
        return False
    transition = (expected_value.get("containment_state"), updated_value.get("containment_state"))
    if expected_value.get("backend", "process_group") != "systemd_cgroup" or transition not in {
        ("starting", "active"), ("active", "stopping"),
    }:
        return False
    write_json(workspace_fd, ACTIVE_COMMAND_FILE, updated_value)
    return True


def verify_persisted_identity(workspace_fd: int, *, expected_run_id: str) -> tuple[dict | None, VerificationResult]:
    try:
        identity = read_persisted_identity(workspace_fd)
    except (CommandIdentityError, OSError) as exc:
        return None, VerificationResult(VerificationStatus.UNVERIFIABLE, str(exc))
    if identity is None:
        return None, VerificationResult(VerificationStatus.UNVERIFIABLE, "active command identity is absent")
    return identity, verify_identity(identity, expected_run_id=expected_run_id)


def terminate_verified_process_group(
    identity,
    *,
    expected_run_id: str,
    grace: float = 0.2,
    reap_timeout: float = 1.0,
    on_wait: Callable[[float], None] | None = None,
) -> GroupTerminationResult:
    """Signal a process group only while its durable leader identity still matches."""
    verification = verify_identity(identity, expected_run_id=expected_run_id)
    if verification.status is not VerificationStatus.MATCH:
        return GroupTerminationResult(verification=verification)

    # Import lazily: command_runner captures identities and owns the canonical
    # TERM/grace/KILL implementation used by active and recovered commands.
    from .command_runner import terminate_process_group

    authorization_failure = None
    authorized_signals = []

    def authorize(requested_signal: int) -> bool:
        nonlocal authorization_failure
        checked = verify_identity(identity, expected_run_id=expected_run_id)
        if checked.status is VerificationStatus.MATCH:
            authorized_signals.append(requested_signal)
            return True
        authorization_failure = checked
        return False

    killed, group_gone, error = terminate_process_group(
        identity["pgid"], grace=grace, reap_timeout=reap_timeout,
        on_wait=on_wait, authorize_signal=authorize,
    )
    if authorization_failure is not None:
        error = error or f"identity changed before signalling: {authorization_failure.reason}"
    final = verify_identity(identity, expected_run_id=expected_run_id)
    if group_gone and final.status is VerificationStatus.MATCH:
        group_gone = False
        error = error or "process group disappeared but its leader identity still matches"
    return GroupTerminationResult(
        verification=verification,
        signalled=bool(authorized_signals),
        killed=killed,
        group_gone=group_gone,
        error=error,
    )
