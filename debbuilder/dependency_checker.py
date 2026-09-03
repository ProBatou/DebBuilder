"""Availability checks for executable build tools and Debian dependencies."""
from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path

from .command_runner import controlled_environment, resolve_working_directory, run_command

DEBIAN_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
VERSION_CLAUSE = re.compile(r"^\s*(>=|<=|==|=|>|<)?\s*[vV]?([0-9]+(?:\.[0-9]+)*)\s*$")
VERSION_NUMBER = re.compile(r"(?<![0-9])(?:go|v)?([0-9]+(?:\.[0-9]+)+)(?:[-+][A-Za-z0-9_.-]+)?")


class DependencyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = VERSION_NUMBER.search(str(value or ""))
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _version_satisfies(version: str, requirement: str) -> bool | None:
    """Evaluate simple numeric constraints; return None for unsupported syntax."""
    actual = _version_tuple(version)
    clauses = [clause for clause in re.split(r"\s*,\s*|\s+", str(requirement or "").strip()) if clause]
    if not clauses or actual is None:
        return None
    for clause in clauses:
        match = VERSION_CLAUSE.fullmatch(clause)
        if not match:
            return None
        operator, expected_text = match.groups()
        expected = tuple(int(part) for part in expected_text.split("."))
        width = max(len(actual), len(expected))
        left = actual + (0,) * (width - len(actual))
        right = expected + (0,) * (width - len(expected))
        if not {
            None: left == right, "=": left == right, "==": left == right,
            ">=": left >= right, "<=": left <= right, ">": left > right, "<": left < right,
        }[operator]:
            return False
    return True


def _tool_check(name: str, requirement: str, *, workspace: str | Path, working_directory: str, environment: dict[str, str] | None, runner) -> dict:
    # Resolution and execution deliberately share command_runner's environment
    # construction. This prevents an interactive shell/check/build PATH split.
    effective_environment = controlled_environment(workspace, environment)
    cwd = resolve_working_directory(workspace, working_directory)
    search_path = ":".join(
        entry if Path(entry).is_absolute() else str(cwd / entry)
        for entry in effective_environment["PATH"].split(":")
    )
    path = shutil.which(name, path=search_path)
    if not path:
        return {"tool": name, "name": name, "path": "", "version": "", "version_output": "", "requirement": requirement, "status": "missing", "available": False, "version_satisfied": None}
    result = runner(
        f"{shlex.quote(path)} --version", workspace=workspace,
        working_directory=working_directory, environment=environment or {}, timeout=15,
    )
    output = (result.get("stdout") or result.get("stderr") or "").strip()
    version_output = output.splitlines()[0].strip() if output else ""
    parsed_version = _version_tuple(version_output)
    version = ".".join(str(part) for part in parsed_version) if parsed_version else ""
    satisfies = _version_satisfies(version, requirement) if requirement else True
    if result.get("status") != "success":
        status = "unusable"
    elif satisfies is False:
        status = "version_mismatch"
    else:
        status = "available"
    return {
        "tool": name, "name": name, "path": path, "version": version, "version_output": version_output, "requirement": requirement,
        "status": status, "available": status == "available", "version_satisfied": satisfies,
        "command": result.get("command", ""), "arguments": result.get("arguments", []),
        "working_directory": result.get("working_directory", working_directory),
        "exit_code": result.get("exit_code"), "duration": result.get("duration", 0),
    }


def check_dependencies(detected: list[str], manually_added: list[str], *, workspace: str | Path, tools: list[str] | None = None, tool_version_requirements: dict[str, str] | None = None, working_directory: str = ".", environment: dict[str, str] | None = None, runner=run_command) -> dict:
    """Check tools through PATH and system dependencies through dpkg."""
    detected = _unique(detected)
    manually_added = _unique(manually_added)
    required = _unique(detected + manually_added)
    tools = _unique(tools or [])
    requirements = {str(key): str(value).strip() for key, value in (tool_version_requirements or {}).items() if str(value).strip()}
    invalid = [name for name in required if not DEBIAN_PACKAGE_NAME.fullmatch(name)]
    if invalid:
        raise DependencyError("invalid_dependency", f"Invalid Debian dependency name: {invalid[0]}")
    invalid_tools = [name for name in tools if not TOOL_NAME.fullmatch(name)]
    if invalid_tools:
        raise DependencyError("invalid_build_tool", f"Invalid build tool name: {invalid_tools[0]}")

    tool_checks = [_tool_check(name, requirements.get(name, ""), workspace=workspace, working_directory=working_directory, environment=environment, runner=runner) for name in tools]
    available_tools = [row["tool"] for row in tool_checks if row["available"]]
    missing_tools = [row["tool"] for row in tool_checks if not row["available"]]
    available, missing, checks = [], [], []
    for name in required:
        result = runner(
            f"dpkg-query --show --showformat=${{db:Status-Abbrev}} {name}", workspace=workspace,
            working_directory=working_directory, environment={**(environment or {}), "LC_ALL": "C"}, timeout=15,
        )
        installed = result["status"] == "success" and result["stdout"].strip().startswith("ii")
        (available if installed else missing).append(name)
        checks.append({
            "dependency": name, "available": installed, "command": result["command"], "arguments": result["arguments"],
            "working_directory": result["working_directory"], "exit_code": result["exit_code"],
            "status": result["status"], "duration": result["duration"],
        })
    state = {
        "detected": detected, "manually_added": manually_added, "required": required,
        "available": available, "missing": missing, "checks": checks,
        "tools": tools, "detected_tools": tools, "available_tools": available_tools, "missing_tools": missing_tools, "tool_checks": tool_checks,
        "system_dependencies": {"detected": detected, "manually_added": manually_added, "required": required, "available": available, "missing": missing, "checks": checks},
        "installation_attempted": False,
    }
    if missing_tools:
        descriptions = []
        for row in tool_checks:
            if not row["available"]:
                suffix = f" (requires {row['requirement']})" if row["requirement"] else ""
                descriptions.append(f"{row['tool']}{suffix}: {row['status']}")
        raise DependencyError("missing_build_tools", f"Required build tools are unavailable: {', '.join(descriptions)}.", details=state)
    if missing:
        raise DependencyError("missing_build_dependencies", f"Missing required system build dependencies: {', '.join(missing)}. Automatic installation is disabled.", details=state)
    return state
