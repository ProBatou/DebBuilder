"""Single secure subprocess boundary for the future build engine."""
from __future__ import annotations

import errno
import os
import re
import secrets
import selectors
import shlex
import signal
import subprocess
import time
from pathlib import Path

from .command_containment import (
    ContainmentCleanupError,
    ContainmentError,
    SystemdCommandContainment,
    containment_cleanup_blocker,
    containment_capability,
    latch_runtime_cleanup_blocker,
    resource_limit_capability,
)
from .command_identity import COMMAND_ID_ENV, RUN_ID_ENV, CommandIdentityError, VerificationStatus, capture_identity, current_recorder, verify_identity
from .execution_cancellation import CANCELLATION_CODE, USER_REQUESTED, ExecutionCancelled
from .resource_limits import ResourceLimitError, empty_policy, enforcement_required, normalize_policy

SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|credential)")
SECRET_OPTION = re.compile(r"(?i)^--?(?:token|secret|password|passwd|api[_-]?key|credential)(?:=|$)")
BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "HOME")
TERMINATION_GRACE = 0.2
TERMINATION_REAP_TIMEOUT = 1.0


class CommandValidationError(ValueError):
    pass


def _unsupported_shell_syntax(command: str) -> str | None:
    quote = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "`":
            return "backtick command substitution"
        elif char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            return "command substitution"
        elif char in {"&", "|", ">", "<", ";", "\n", "\r"}:
            return repr(char)
        index += 1
    return None


def parse_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise CommandValidationError("command must be a non-empty string")
    unsupported = _unsupported_shell_syntax(command)
    if unsupported:
        raise CommandValidationError(f"unsupported shell operator or syntax: {unsupported}; use working_directory and separate commands")
    try:
        arguments = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CommandValidationError(f"invalid command quoting: {exc}") from exc
    if not arguments:
        raise CommandValidationError("command must contain an executable")
    if arguments[0] == "cd":
        raise CommandValidationError("cd is not supported; set build.working_directory instead")
    return arguments


def resolve_working_directory(workspace: str | Path, working_directory: str = ".") -> Path:
    root = Path(workspace).resolve()
    if Path(working_directory).is_absolute():
        raise CommandValidationError("working_directory must be relative to the workspace")
    candidate = (root / working_directory).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CommandValidationError("working_directory escapes the workspace") from exc
    if not candidate.is_dir():
        raise CommandValidationError("working_directory does not exist or is not a directory")
    return candidate


def controlled_environment(workspace: str | Path, additions: dict[str, str] | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {key: source[key] for key in BASE_ENV_KEYS if source.get(key)}
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    for key, value in (additions or {}).items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str):
            raise CommandValidationError("environment variables require valid names and string values")
        if "\x00" in value:
            raise CommandValidationError(f"environment variable {key} contains a null byte")
        result[key] = value
    return result


def redact_text(text: str, environment: dict[str, str]) -> str:
    redacted = str(text or "")
    secrets = [value for key, value in environment.items() if SECRET_KEY.search(key) and value]
    for value in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def redact_arguments(arguments: list[str], environment: dict[str, str] | None = None) -> list[str]:
    values = []
    hide_next = False
    for argument in arguments:
        if hide_next:
            values.append("[REDACTED]")
            hide_next = False
        elif SECRET_OPTION.match(argument):
            if "=" in argument:
                values.append(argument.split("=", 1)[0] + "=[REDACTED]")
            else:
                values.append(argument)
                hide_next = True
        else:
            values.append(redact_text(argument, environment or {}))
    return values


def secret_argument_values(arguments: list[str]) -> list[str]:
    values, hide_next = [], False
    for argument in arguments:
        if hide_next:
            values.append(argument)
            hide_next = False
        elif SECRET_OPTION.match(argument):
            if "=" in argument:
                values.append(argument.split("=", 1)[1])
            else:
                hide_next = True
    return [value for value in values if value]


def redact_command(command: str, arguments: list[str], environment: dict[str, str] | None = None) -> str:
    redacted = redact_text(command, environment or {})
    redacted = re.sub(r"(?i)(--?(?:token|secret|password|passwd|api[_-]?key|credential)=)([^\s]+)", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(--?(?:token|secret|password|passwd|api[_-]?key|credential)\s+)([^\s]+)", r"\1[REDACTED]", redacted)
    return redacted


def _read_process_output(selector, output: dict[str, list[str]], redaction_environment: dict[str, str], *, wait: float, on_output=None) -> bool:
    activity = False
    for key, _mask in selector.select(wait):
        chunk = os.read(key.fileobj.fileno(), 4096)
        if chunk:
            activity = True
            redacted = redact_text(chunk.decode("utf-8", errors="replace"), redaction_environment)
            output[key.data].append(redacted)
            if callable(on_output):
                on_output({"stream": key.data, "text": redacted})
        else:
            selector.unregister(key.fileobj)
            key.fileobj.close()
    return activity


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_process_group(process_group: int, requested_signal: int) -> bool:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return False
    return True


def terminate_process_group(process_group: int, *, grace: float = TERMINATION_GRACE, reap_timeout: float = TERMINATION_REAP_TIMEOUT, on_wait=None, authorize_signal=None) -> tuple[bool, bool, str]:
    """Apply the canonical TERM/grace/KILL policy to one process group."""
    errors = []
    killed = False

    def wait(deadline: float) -> None:
        delay = min(0.02, max(0.0, deadline - time.monotonic()))
        if callable(on_wait):
            on_wait(delay)
        elif delay:
            time.sleep(delay)

    def signal_group(requested_signal: int) -> bool:
        if callable(authorize_signal) and not authorize_signal(requested_signal):
            errors.append(f"process identity could not be verified before {signal.Signals(requested_signal).name}")
            return False
        return _signal_process_group(process_group, requested_signal)

    try:
        signal_group(signal.SIGTERM)
    except OSError as exc:
        errors.append(f"could not terminate process group {process_group}: {exc}")

    grace_deadline = time.monotonic() + grace
    while time.monotonic() < grace_deadline:
        try:
            if not _process_group_exists(process_group):
                break
        except OSError as exc:
            errors.append(f"could not inspect process group {process_group}: {exc}")
            break
        wait(grace_deadline)

    try:
        group_alive = _process_group_exists(process_group)
    except OSError as exc:
        group_alive = True
        errors.append(f"could not inspect process group {process_group}: {exc}")
    if group_alive:
        try:
            killed = signal_group(signal.SIGKILL)
        except OSError as exc:
            errors.append(f"could not kill process group {process_group}: {exc}")

    reap_deadline = time.monotonic() + reap_timeout
    while time.monotonic() < reap_deadline:
        try:
            if not _process_group_exists(process_group):
                break
        except OSError as exc:
            errors.append(f"could not inspect process group {process_group}: {exc}")
            break
        wait(reap_deadline)
    try:
        group_gone = not _process_group_exists(process_group)
    except OSError as exc:
        group_gone = False
        errors.append(f"could not inspect process group {process_group}: {exc}")
    if not group_gone:
        errors.append(f"process group {process_group} remained alive after SIGKILL")
    return killed, group_gone, "; ".join(dict.fromkeys(errors))


def _terminate_process_group(process: subprocess.Popen, process_group: int, selector, output: dict[str, list[str]], redaction_environment: dict[str, str], *, on_output=None, grace: float = TERMINATION_GRACE, authorize_signal=None) -> tuple[int | None, bool, str]:
    """Stop the whole command session and reap its direct child."""
    def drain(wait: float) -> None:
        process.poll()
        _read_process_output(
            selector, output, redaction_environment,
            wait=wait, on_output=on_output,
        )
    killed, _group_gone, error = terminate_process_group(
        process_group, grace=grace, on_wait=drain, authorize_signal=authorize_signal,
    )
    errors = [error] if error else []

    try:
        exit_code = process.wait(timeout=TERMINATION_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        exit_code = None
        errors.append(f"process {process.pid} could not be reaped")

    drain_deadline = time.monotonic() + TERMINATION_REAP_TIMEOUT
    while selector.get_map() and time.monotonic() < drain_deadline:
        _read_process_output(
            selector, output, redaction_environment,
            wait=min(0.02, max(0.0, drain_deadline - time.monotonic())), on_output=on_output,
        )
    if selector.get_map():
        errors.append("command output pipes remained open after process-group termination")
        for key in list(selector.get_map().values()):
            selector.unregister(key.fileobj)
            key.fileobj.close()
    return exit_code, killed, "; ".join(dict.fromkeys(errors))


def _stream_process_group(arguments: list[str], *, cwd: Path, env: dict[str, str], inactivity_timeout: float | None, maximum_runtime: float | None, redaction_environment: dict[str, str], on_output=None, cancellation_event=None, on_cancel=None, identity_recorder=None, pass_fds: tuple[int, ...] = (), resource_policy: dict | None = None, resource_workspace: Path | None = None) -> dict:
    command_id = secrets.token_hex(16) if identity_recorder is not None else ""
    process_environment = dict(env)
    if identity_recorder is not None:
        process_environment.update({
            RUN_ID_ENV: identity_recorder.run_id,
            COMMAND_ID_ENV: command_id,
        })
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            arguments, cwd=cwd, env=process_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, bufsize=0, start_new_session=True, pass_fds=pass_fds,
        )
    except BaseException:
        selector.close()
        raise
    process_group = process.pid
    output = {"stdout": [], "stderr": []}
    started = time.monotonic()
    last_activity = started
    timeout_reason = None
    identity = None

    def authorize_signal(_requested_signal: int) -> bool:
        if identity_recorder is None:
            # Commands outside a persisted Build Run retain the pre-existing
            # direct-child/session ownership model and have no recovery record.
            return True
        if identity is None:
            # Before durable capture, the unreleased Popen child and the fresh
            # start_new_session boundary are the only available ownership proof.
            return process.returncode is None
        checked = verify_identity(identity, expected_run_id=identity_recorder.run_id)
        return checked.status is VerificationStatus.MATCH

    try:
        if process.stdout:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        if process.stderr:
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if identity_recorder is not None:
            try:
                identity = capture_identity(
                    process.pid,
                    run_id=identity_recorder.run_id,
                    command_id=command_id,
                    process_exited=lambda: process.poll() is not None,
                    allow_vanished_environment=True,
                    resource_policy=resource_policy,
                )
            except (CommandIdentityError, OSError) as exc:
                # A very short command can exit while its several /proc fields
                # are being captured.  Any capture failure is safe to consume
                # only when both the direct child and its original process
                # group have already disappeared.
                if isinstance(exc, OSError) and exc.errno in {errno.ENOENT, errno.ESRCH}:
                    # Synchronize with the directly-owned child instead of
                    # sampling a transient kernel exit state.  This is result
                    # collection, not a timing retry.
                    process.wait()
                else:
                    process.poll()
                if process.returncode is None or _process_group_exists(process_group):
                    raise
            if identity is not None:
                identity_recorder.record(identity)
        while True:
            process.poll()
            group_alive = _process_group_exists(process_group)
            if not selector.get_map() and not group_alive:
                break
            now = time.monotonic()
            # Cancellation wins over a timeout that becomes observable in this
            # same loop iteration.  Once timeout_reason is selected below, the
            # outcome is not retroactively converted while termination runs.
            if cancellation_event is not None and cancellation_event.is_set():
                cancellation = on_cancel() if callable(on_cancel) else {}
                exit_code, killed, termination_error = _terminate_process_group(
                    process, process_group, selector, output, redaction_environment,
                    on_output=on_output, authorize_signal=authorize_signal,
                )
                return {
                    "exit_code": None,
                    "stdout": "".join(output["stdout"]),
                    "stderr": "".join(output["stderr"]),
                    "timed_out": False,
                    "timeout_reason": "",
                    "process_exit_code": exit_code,
                    "killed": killed,
                    "termination_error": termination_error or None,
                    "cancellation_requested": True,
                    "cancelled": not bool(termination_error),
                    "cancellation": cancellation or {},
                }
            if maximum_runtime is not None and now - started >= maximum_runtime:
                timeout_reason = "maximum_runtime"
                break
            if inactivity_timeout is not None and now - last_activity >= inactivity_timeout:
                timeout_reason = "inactivity"
                break
            waits = [0.1]
            if maximum_runtime is not None:
                waits.append(max(0.0, maximum_runtime - (now - started)))
            if inactivity_timeout is not None:
                waits.append(max(0.0, inactivity_timeout - (now - last_activity)))
            if selector.get_map():
                activity = _read_process_output(selector, output, redaction_environment, wait=min(waits), on_output=on_output)
            else:
                time.sleep(min(waits))
                activity = False
            if activity:
                last_activity = time.monotonic()
        if timeout_reason:
            exit_code, killed, termination_error = _terminate_process_group(
                process, process_group, selector, output, redaction_environment,
                on_output=on_output, authorize_signal=authorize_signal,
            )
            if timeout_reason == "inactivity":
                message = f"command stopped after {inactivity_timeout:g} seconds without stdout/stderr activity"
            else:
                message = f"command stopped after maximum runtime of {maximum_runtime:g} seconds"
            output["stderr"].append(("\n" if output["stderr"] else "") + message)
            if termination_error:
                output["stderr"].append("\nprocess-group termination error: " + termination_error)
            return {"exit_code": None, "stdout": "".join(output["stdout"]), "stderr": "".join(output["stderr"]), "timed_out": True, "timeout_reason": timeout_reason, "process_exit_code": exit_code, "killed": killed, "termination_error": termination_error, "cancellation_requested": False, "cancelled": False}
        exit_code = process.wait()
        return {"exit_code": exit_code, "stdout": "".join(output["stdout"]), "stderr": "".join(output["stderr"]), "timed_out": False, "timeout_reason": "", "termination_error": "", "cancellation_requested": False, "cancelled": False}
    except BaseException:
        _terminate_process_group(
            process, process_group, selector, output, redaction_environment,
            authorize_signal=authorize_signal,
        )
        raise
    finally:
        try:
            if identity is not None:
                try:
                    group_gone = not _process_group_exists(process_group)
                except OSError:
                    group_gone = False
                if group_gone:
                    if identity_recorder.clear(identity) is not True:
                        raise CommandIdentityError("active command identity could not be cleared exactly")
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _stream_systemd_cgroup(arguments: list[str], *, cwd: Path, env: dict[str, str], inactivity_timeout: float | None, maximum_runtime: float | None, redaction_environment: dict[str, str], on_output=None, cancellation_event=None, on_cancel=None, identity_recorder, pass_fds: tuple[int, ...] = (), resource_policy: dict | None = None, resource_workspace: Path | None = None) -> dict:
    """Stream one command spawned directly by PID 1 in its transient cgroup."""
    if pass_fds:
        raise CommandValidationError("descriptor inheritance is not supported by systemd containment")
    command_id = secrets.token_hex(16)
    selector = selectors.DefaultSelector()
    try:
        containment = SystemdCommandContainment.start(
            arguments, cwd=cwd, environment=env,
            recorder=identity_recorder, command_id=command_id,
            resource_policy=resource_policy,
            resource_workspace=resource_workspace,
        )
    except BaseException:
        selector.close()
        raise
    output = {"stdout": [], "stderr": []}
    started = time.monotonic()
    last_activity = started
    timeout_reason = None

    def drain(wait: float, *, emit=True) -> None:
        if selector.get_map():
            _read_process_output(
                selector, output, redaction_environment, wait=wait,
                on_output=on_output if emit else None,
            )

    def drain_remaining(*, emit=True) -> None:
        deadline = time.monotonic() + TERMINATION_REAP_TIMEOUT
        while selector.get_map() and time.monotonic() < deadline:
            drain(min(0.02, max(0.0, deadline - time.monotonic())), emit=emit)

    try:
        selector.register(containment.stdout, selectors.EVENT_READ, "stdout")
        selector.register(containment.stderr, selectors.EVENT_READ, "stderr")
        process_exit_code = None
        while True:
            completed = containment.poll()
            if completed is not None:
                process_exit_code = completed
                # PID 1 retains its copies of the passed output descriptors for
                # a retained unit.  Completion proves the cgroup empty; remove
                # the unit below to close those copies and produce pipe EOF.
                break
            now = time.monotonic()
            if cancellation_event is not None and cancellation_event.is_set():
                cancellation = on_cancel() if callable(on_cancel) else {}
                terminated = containment.terminate(on_wait=drain)
                terminated = containment.clear_after_termination(terminated)
                drain_remaining()
                return {
                    "exit_code": None,
                    "stdout": "".join(output["stdout"]),
                    "stderr": "".join(output["stderr"]),
                    "timed_out": False,
                    "timeout_reason": "",
                    "process_exit_code": terminated.process_exit_code,
                    "killed": terminated.killed,
                    "termination_error": terminated.error or None,
                    "containment_gone": terminated.gone,
                    "cancellation_requested": True,
                    "cancelled": terminated.gone and not bool(terminated.error),
                    "cancellation": cancellation or {},
                    "resource_control": containment.resource_control(),
                }
            if maximum_runtime is not None and now - started >= maximum_runtime:
                timeout_reason = "maximum_runtime"
                break
            if inactivity_timeout is not None and now - last_activity >= inactivity_timeout:
                timeout_reason = "inactivity"
                break
            waits = [0.1]
            if maximum_runtime is not None:
                waits.append(max(0.0, maximum_runtime - (now - started)))
            if inactivity_timeout is not None:
                waits.append(max(0.0, inactivity_timeout - (now - last_activity)))
            before = sum(len(parts) for parts in output.values())
            if selector.get_map():
                drain(min(waits))
            else:
                time.sleep(min(waits))
            if sum(len(parts) for parts in output.values()) != before:
                last_activity = time.monotonic()

        if timeout_reason:
            terminated = containment.terminate(on_wait=drain)
            terminated = containment.clear_after_termination(terminated)
            drain_remaining()
            if timeout_reason == "inactivity":
                message = f"command stopped after {inactivity_timeout:g} seconds without stdout/stderr activity"
            else:
                message = f"command stopped after maximum runtime of {maximum_runtime:g} seconds"
            output["stderr"].append(("\n" if output["stderr"] else "") + message)
            if terminated.error:
                output["stderr"].append("\ncontainment termination error: " + terminated.error)
            return {
                "exit_code": None,
                "stdout": "".join(output["stdout"]),
                "stderr": "".join(output["stderr"]),
                "timed_out": True,
                "timeout_reason": timeout_reason,
                "process_exit_code": terminated.process_exit_code,
                "killed": terminated.killed,
                "termination_error": terminated.error,
                "containment_gone": terminated.gone,
                "cancellation_requested": False,
                "cancelled": False,
                "resource_control": containment.resource_control(),
            }

        finished = containment.finish(on_wait=drain)
        drain_remaining()
        if not finished.gone or finished.error or finished.enforcement_error:
            resource_control = containment.resource_control(
                verification="failed" if finished.enforcement_error else "verified",
            )
            return {
                "exit_code": process_exit_code,
                "stdout": "".join(output["stdout"]),
                "stderr": "".join(output["stderr"]),
                "timed_out": False,
                "timeout_reason": "",
                "process_exit_code": process_exit_code,
                "killed": finished.killed,
                "termination_error": finished.error or ("transient containment disappearance was not proved" if not finished.gone else ""),
                "containment_gone": finished.gone,
                "cancellation_requested": False,
                "cancelled": False,
                "resource_control": resource_control,
            }
        return {
            "exit_code": process_exit_code,
            "stdout": "".join(output["stdout"]),
            "stderr": "".join(output["stderr"]),
            "timed_out": False,
            "timeout_reason": "",
            "termination_error": "",
            "containment_gone": True,
            "cancellation_requested": False,
            "cancelled": False,
            "resource_control": containment.resource_control(),
        }
    except BaseException as original:
        try:
            terminated = containment.terminate(on_wait=lambda wait: drain(wait, emit=False))
            terminated = containment.clear_after_termination(terminated)
            drain_remaining(emit=False)
        except BaseException as cleanup:
            raise ContainmentCleanupError(f"command callback failed and containment cleanup also failed: {cleanup}") from original
        if not terminated.gone:
            raise ContainmentCleanupError(
                f"command callback failed and containment cleanup could not be proved: {terminated.error or 'unit remains'}"
            ) from original
        raise
    finally:
        selector.close()
        containment.close()


def _validated_timeout(value: float | None, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CommandValidationError(f"{what} must be a positive number or None")
    return float(value)


def run_command(command: str, *, workspace: str | Path, working_directory: str = ".", environment: dict[str, str] | None = None, timeout: float | None = None, inactivity_timeout: float | None = 300, maximum_runtime: float | None = None, on_output=None, cancellation_event=None, on_cancel=None, pass_fds: tuple[int, ...] = ()) -> dict:
    started = time.monotonic()
    display_cwd = str(working_directory or ".")
    safe_for_redaction = {key: value for key, value in (environment or {}).items() if isinstance(key, str) and isinstance(value, str)}
    if timeout is not None and maximum_runtime is None:
        maximum_runtime = timeout
    result = {"command": redact_command(command, [], safe_for_redaction), "arguments": [], "working_directory": display_cwd, "configured_working_directory": display_cwd, "status": "failed", "exit_code": None, "process_exit_code": None, "stdout": "", "stderr": "", "duration": 0.0, "timed_out": False, "timeout_reason": "", "killed": False, "termination_error": "", "cancelled": False, "cancellation_requested": False, "error_code": ""}
    try:
        inactivity_timeout = _validated_timeout(inactivity_timeout, "inactivity_timeout")
        maximum_runtime = _validated_timeout(maximum_runtime, "maximum_runtime")
        if not isinstance(pass_fds, tuple) or any(
            isinstance(fd, bool) or not isinstance(fd, int) or fd < 0 for fd in pass_fds
        ):
            raise CommandValidationError("pass_fds must be a tuple of non-negative file descriptors")
        env = controlled_environment(workspace, environment)
        arguments = parse_command(command)
        redaction_environment = {**env, **{f"SECRET_ARGUMENT_{index}": value for index, value in enumerate(secret_argument_values(arguments), 1)}}
        result["command"] = redact_command(command, arguments, redaction_environment)
        cwd = resolve_working_directory(workspace, display_cwd)
        result["working_directory"] = str(cwd)
        result["arguments"] = redact_arguments(arguments, redaction_environment)
        if cancellation_event is not None and cancellation_event.is_set():
            cancellation = on_cancel() if callable(on_cancel) else {}
            result.update({
                "status": "cancelled", "cancelled": True, "cancellation_requested": True,
                "termination_error": None,
                "cancellation": cancellation or {"code": CANCELLATION_CODE, "reason": USER_REQUESTED},
            })
            result["duration"] = round(time.monotonic() - started, 6)
            raise ExecutionCancelled(result["cancellation"], command_result=result)
        identity_recorder = current_recorder()
        resource_policy = normalize_policy(identity_recorder.resource_policy if identity_recorder is not None else empty_policy())
        finite_policy = enforcement_required(resource_policy)
        unresolved_cleanup = containment_cleanup_blocker()
        if identity_recorder is not None and unresolved_cleanup:
            raise ContainmentCleanupError(
                f"containment cleanup is unresolved: {unresolved_cleanup}"
            )
        capability = containment_capability() if identity_recorder is not None and identity_recorder.update is not None else None
        if finite_policy and identity_recorder is not None and identity_recorder.update is not None:
            checked = resource_limit_capability(
                resource_policy, workspace=Path(workspace).resolve(), refresh=True,
            )
            capability = type(capability)(
                checked["backend"], checked["available"], checked["reason"],
            )
        unresolved_cleanup = containment_cleanup_blocker()
        if identity_recorder is not None and unresolved_cleanup:
            raise ContainmentCleanupError(
                f"containment cleanup is unresolved: {unresolved_cleanup}"
            )
        if finite_policy and (pass_fds or capability is None or not capability.available):
            raise ContainmentError(
                "resource_limit_enforcement_failed: finite resource policy requires available systemd/cgroup containment"
            )
        # A repository lease descriptor must reach the actual mutation process.
        # Repository commands currently run outside Build command identity
        # recording, so this branch preserves their lock across parent death.
        uses_systemd_containment = not pass_fds and capability is not None and capability.available
        stream = _stream_systemd_cgroup if uses_systemd_containment else _stream_process_group
        completed = stream(
            arguments, cwd=cwd, env=env, inactivity_timeout=inactivity_timeout,
            maximum_runtime=maximum_runtime, redaction_environment=redaction_environment,
            on_output=on_output, cancellation_event=cancellation_event, on_cancel=on_cancel,
            identity_recorder=identity_recorder, pass_fds=pass_fds,
            resource_policy=resource_policy,
            resource_workspace=Path(workspace).resolve(),
        )
        if (
            uses_systemd_containment
            and completed.get("termination_error")
            and not completed.get("containment_gone", False)
        ):
            latch_runtime_cleanup_blocker(str(completed["termination_error"]))
        if not completed.get("resource_control"):
            completed["resource_control"] = {
                "backend": "process_group",
                "enforcement_required": False,
                "verification": "fallback",
                "requested": resource_policy,
                "unit_result": "",
                "observations": {},
                "outcome": None,
            }
        resource_outcome = ((completed.get("resource_control") or {}).get("outcome") or {})
        if completed.get("termination_error"):
            status = "failed"
        elif completed.get("cancellation_requested"):
            status = "cancelled" if completed.get("cancelled") else "failed"
        elif completed["timed_out"] or resource_outcome:
            status = "failed"
        else:
            status = "success" if completed["exit_code"] == 0 else "failed"
        result.update({"exit_code": completed["exit_code"], "process_exit_code": completed.get("process_exit_code"), "stdout": completed["stdout"], "stderr": completed["stderr"], "timed_out": completed["timed_out"], "timeout_reason": completed.get("timeout_reason", ""), "killed": completed.get("killed", False), "termination_error": completed.get("termination_error", ""), "cancelled": completed.get("cancelled", False), "cancellation_requested": completed.get("cancellation_requested", False), "status": status, "resource_control": completed.get("resource_control")})
        if completed.get("termination_error"):
            result["error_code"] = "command_containment_termination_failed"
        elif resource_outcome and not completed["timed_out"] and not completed.get("cancellation_requested"):
            result["error_code"] = resource_outcome.get("code", "command_resource_limit_exceeded")
        if completed.get("cancellation_requested"):
            result["cancellation"] = completed.get("cancellation") or {"code": CANCELLATION_CODE, "reason": USER_REQUESTED}
            result["duration"] = round(time.monotonic() - started, 6)
            raise ExecutionCancelled(result["cancellation"], command_result=result)
    except (CommandValidationError, ContainmentError, CommandIdentityError, ResourceLimitError, OSError) as exc:
        result["stderr"] = str(exc)
        if isinstance(exc, ContainmentCleanupError):
            if locals().get("uses_systemd_containment", False):
                latch_runtime_cleanup_blocker(str(exc))
            result["error_code"] = "command_containment_termination_failed"
        elif isinstance(exc, (ContainmentError, ResourceLimitError)) and enforcement_required(locals().get("resource_policy", empty_policy())):
            result["error_code"] = "resource_limit_enforcement_failed"
        if isinstance(exc, (ContainmentError, ResourceLimitError)) and enforcement_required(locals().get("resource_policy", empty_policy())):
            result["resource_control"] = {
                "backend": "systemd_cgroup",
                "enforcement_required": True,
                "verification": "failed",
                "requested": normalize_policy(resource_policy),
                "unit_result": "",
                "observations": {},
                "outcome": {"code": "resource_limit_enforcement_failed", "control": None},
            }
    result["duration"] = round(time.monotonic() - started, 6)
    return result
