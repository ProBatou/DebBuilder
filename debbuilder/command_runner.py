"""Single secure subprocess boundary for the future build engine."""
from __future__ import annotations

import os
import re
import selectors
import shlex
import signal
import subprocess
import time
from pathlib import Path

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


def _terminate_process_group(process: subprocess.Popen, process_group: int, selector, output: dict[str, list[str]], redaction_environment: dict[str, str], *, on_output=None, grace: float = TERMINATION_GRACE) -> tuple[int | None, bool, str]:
    """Stop the whole command session and reap its direct child."""
    errors = []
    killed = False
    try:
        _signal_process_group(process_group, signal.SIGTERM)
    except OSError as exc:
        errors.append(f"could not terminate process group {process_group}: {exc}")

    grace_deadline = time.monotonic() + grace
    while time.monotonic() < grace_deadline:
        process.poll()
        try:
            if not _process_group_exists(process_group):
                break
        except OSError as exc:
            errors.append(f"could not inspect process group {process_group}: {exc}")
            break
        _read_process_output(
            selector, output, redaction_environment,
            wait=min(0.02, max(0.0, grace_deadline - time.monotonic())), on_output=on_output,
        )

    try:
        group_alive = _process_group_exists(process_group)
    except OSError as exc:
        group_alive = True
        errors.append(f"could not inspect process group {process_group}: {exc}")
    if group_alive:
        try:
            killed = _signal_process_group(process_group, signal.SIGKILL)
        except OSError as exc:
            errors.append(f"could not kill process group {process_group}: {exc}")

    reap_deadline = time.monotonic() + TERMINATION_REAP_TIMEOUT
    while time.monotonic() < reap_deadline:
        process.poll()
        _read_process_output(
            selector, output, redaction_environment,
            wait=min(0.02, max(0.0, reap_deadline - time.monotonic())), on_output=on_output,
        )
        try:
            if not _process_group_exists(process_group):
                break
        except OSError as exc:
            errors.append(f"could not inspect process group {process_group}: {exc}")
            break
    else:
        errors.append(f"process group {process_group} remained alive after SIGKILL")

    remaining = max(0.0, reap_deadline - time.monotonic())
    try:
        exit_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        exit_code = None
        errors.append(f"process {process.pid} could not be reaped")

    while selector.get_map() and time.monotonic() < reap_deadline:
        _read_process_output(
            selector, output, redaction_environment,
            wait=min(0.02, max(0.0, reap_deadline - time.monotonic())), on_output=on_output,
        )
    if selector.get_map():
        errors.append("command output pipes remained open after process-group termination")
        for key in list(selector.get_map().values()):
            selector.unregister(key.fileobj)
            key.fileobj.close()
    return exit_code, killed, "; ".join(dict.fromkeys(errors))


def _stream_process(arguments: list[str], *, cwd: Path, env: dict[str, str], inactivity_timeout: float | None, maximum_runtime: float | None, redaction_environment: dict[str, str], on_output=None) -> dict:
    process = subprocess.Popen(
        arguments, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, bufsize=0, start_new_session=True,
    )
    process_group = process.pid
    selector = selectors.DefaultSelector()
    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    if process.stderr:
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": [], "stderr": []}
    started = time.monotonic()
    last_activity = started
    timeout_reason = None
    try:
        while True:
            process.poll()
            group_alive = _process_group_exists(process_group)
            if not selector.get_map() and not group_alive:
                break
            now = time.monotonic()
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
                process, process_group, selector, output, redaction_environment, on_output=on_output,
            )
            if timeout_reason == "inactivity":
                message = f"command stopped after {inactivity_timeout:g} seconds without stdout/stderr activity"
            else:
                message = f"command stopped after maximum runtime of {maximum_runtime:g} seconds"
            output["stderr"].append(("\n" if output["stderr"] else "") + message)
            if termination_error:
                output["stderr"].append("\nprocess-group termination error: " + termination_error)
            return {"exit_code": None, "stdout": "".join(output["stdout"]), "stderr": "".join(output["stderr"]), "timed_out": True, "timeout_reason": timeout_reason, "process_exit_code": exit_code, "killed": killed, "termination_error": termination_error}
        exit_code = process.wait()
        return {"exit_code": exit_code, "stdout": "".join(output["stdout"]), "stderr": "".join(output["stderr"]), "timed_out": False, "timeout_reason": "", "termination_error": ""}
    except BaseException:
        _terminate_process_group(process, process_group, selector, output, redaction_environment)
        raise
    finally:
        selector.close()


def _validated_timeout(value: float | None, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CommandValidationError(f"{what} must be a positive number or None")
    return float(value)


def run_command(command: str, *, workspace: str | Path, working_directory: str = ".", environment: dict[str, str] | None = None, timeout: float | None = None, inactivity_timeout: float | None = 300, maximum_runtime: float | None = None, on_output=None) -> dict:
    started = time.monotonic()
    display_cwd = str(working_directory or ".")
    safe_for_redaction = {key: value for key, value in (environment or {}).items() if isinstance(key, str) and isinstance(value, str)}
    if timeout is not None and maximum_runtime is None:
        maximum_runtime = timeout
    result = {"command": redact_command(command, [], safe_for_redaction), "arguments": [], "working_directory": display_cwd, "configured_working_directory": display_cwd, "status": "failed", "exit_code": None, "process_exit_code": None, "stdout": "", "stderr": "", "duration": 0.0, "timed_out": False, "timeout_reason": "", "killed": False, "termination_error": ""}
    try:
        inactivity_timeout = _validated_timeout(inactivity_timeout, "inactivity_timeout")
        maximum_runtime = _validated_timeout(maximum_runtime, "maximum_runtime")
        env = controlled_environment(workspace, environment)
        arguments = parse_command(command)
        redaction_environment = {**env, **{f"SECRET_ARGUMENT_{index}": value for index, value in enumerate(secret_argument_values(arguments), 1)}}
        result["command"] = redact_command(command, arguments, redaction_environment)
        cwd = resolve_working_directory(workspace, display_cwd)
        result["working_directory"] = str(cwd)
        result["arguments"] = redact_arguments(arguments, redaction_environment)
        completed = _stream_process(arguments, cwd=cwd, env=env, inactivity_timeout=inactivity_timeout, maximum_runtime=maximum_runtime, redaction_environment=redaction_environment, on_output=on_output)
        result.update({"exit_code": completed["exit_code"], "process_exit_code": completed.get("process_exit_code"), "stdout": completed["stdout"], "stderr": completed["stderr"], "timed_out": completed["timed_out"], "timeout_reason": completed.get("timeout_reason", ""), "killed": completed.get("killed", False), "termination_error": completed.get("termination_error", ""), "status": "failed" if completed["timed_out"] else "success" if completed["exit_code"] == 0 else "failed"})
    except (CommandValidationError, OSError) as exc:
        result["stderr"] = str(exc)
    result["duration"] = round(time.monotonic() - started, 6)
    return result
