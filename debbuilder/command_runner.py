"""Single secure subprocess boundary for the future build engine."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|credential)")
SECRET_OPTION = re.compile(r"(?i)^--?(?:token|secret|password|passwd|api[_-]?key|credential)(?:=|$)")
BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "HOME")


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


def run_command(command: str, *, workspace: str | Path, working_directory: str = ".", environment: dict[str, str] | None = None, timeout: float = 120) -> dict:
    started = time.monotonic()
    display_cwd = str(working_directory or ".")
    safe_for_redaction = {key: value for key, value in (environment or {}).items() if isinstance(key, str) and isinstance(value, str)}
    result = {"command": redact_command(command, [], safe_for_redaction), "arguments": [], "working_directory": display_cwd, "configured_working_directory": display_cwd, "status": "failed", "exit_code": None, "stdout": "", "stderr": "", "duration": 0.0, "timed_out": False}
    try:
        env = controlled_environment(workspace, environment)
        arguments = parse_command(command)
        redaction_environment = {**env, **{f"SECRET_ARGUMENT_{index}": value for index, value in enumerate(secret_argument_values(arguments), 1)}}
        result["command"] = redact_command(command, arguments, redaction_environment)
        cwd = resolve_working_directory(workspace, display_cwd)
        result["working_directory"] = str(cwd)
        result["arguments"] = redact_arguments(arguments, redaction_environment)
        completed = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False, shell=False)
        result.update({"exit_code": completed.returncode, "stdout": redact_text(completed.stdout, redaction_environment), "stderr": redact_text(completed.stderr, redaction_environment), "status": "success" if completed.returncode == 0 else "failed"})
    except subprocess.TimeoutExpired as exc:
        result.update({"timed_out": True, "stderr": f"command timed out after {timeout} seconds", "stdout": redact_text(exc.stdout or "", redaction_environment)})
    except (CommandValidationError, OSError) as exc:
        result["stderr"] = str(exc)
    result["duration"] = round(time.monotonic() - started, 6)
    return result
