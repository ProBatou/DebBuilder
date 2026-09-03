"""Deterministic host availability checks for Debian build dependencies."""
from __future__ import annotations

import re
from pathlib import Path

from .command_runner import run_command

DEBIAN_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


class DependencyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def check_dependencies(detected: list[str], manually_added: list[str], *, workspace: str | Path, runner=run_command) -> dict:
    detected = _unique(detected)
    manually_added = _unique(manually_added)
    required = _unique(detected + manually_added)
    invalid = [name for name in required if not DEBIAN_PACKAGE_NAME.fullmatch(name)]
    if invalid:
        raise DependencyError("invalid_dependency", f"Invalid Debian dependency name: {invalid[0]}")
    available, missing, checks = [], [], []
    for name in required:
        result = runner(
            f"dpkg-query --show --showformat=${{db:Status-Abbrev}} {name}",
            workspace=workspace,
            working_directory=".",
            environment={"LC_ALL": "C"},
            timeout=15,
        )
        installed = result["status"] == "success" and result["stdout"].strip().startswith("ii")
        (available if installed else missing).append(name)
        checks.append({
            "dependency": name,
            "available": installed,
            "command": result["command"],
            "arguments": result["arguments"],
            "working_directory": result["working_directory"],
            "exit_code": result["exit_code"],
            "status": result["status"],
            "duration": result["duration"],
        })
    state = {
        "detected": detected,
        "manually_added": manually_added,
        "required": required,
        "available": available,
        "missing": missing,
        "checks": checks,
        "installation_attempted": False,
    }
    if missing:
        raise DependencyError(
            "missing_build_dependencies",
            f"Missing required build dependencies: {', '.join(missing)}. Automatic installation is disabled.",
            details=state,
        )
    return state
