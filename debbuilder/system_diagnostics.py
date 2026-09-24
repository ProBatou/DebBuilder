"""Bounded, read-only operator snapshot; no raw subsystem data escapes."""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from pathlib import Path

from . import __version__, apt_repo, command_containment, local_repository_bootstrap
from .build_models import CURRENT_RUN_SCHEMA_VERSION
from .recipe_schema import SCHEMA_VERSION
from .runtime import native_debian_architecture


SCHEMA_VERSION_DIAGNOSTICS = 1
STATUSES = ("ok", "warning", "failed", "unknown")
CHECK_IDS = (
    "application.runtime", "settings.documents", "repository.publication",
    "validation.oci", "execution.admission", "execution.containment",
    "application.mutations", "automation.scheduler", "automation.orchestrator",
)
_FINGERPRINT = re.compile(r"(?:[0-9A-F]{40}|[0-9A-F]{64})\Z")


def _regular_file(path: Path, maximum: int) -> bytes | None:
    """Read one small file without following its final symlink or special file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            return None
        data = os.read(fd, maximum + 1)
        return data if len(data) <= maximum else None
    finally:
        os.close(fd)


def _repository(root: Path, apt: dict, active: bool) -> tuple[str, str, dict]:
    if not root.is_dir():
        return "warning", "Repository root is unavailable", {"listener_active": active}
    try:
        raw = _regular_file(root / "conf" / "distributions", 16 * 1024)
        if raw is None:
            raise ValueError("unsafe configuration")
        fingerprint, distribution = local_repository_bootstrap._identity_from_config(
            raw.decode("utf-8", "strict"), apt["distribution"], apt["component"],
        )
        configured_arch = apt["architecture"]
        if configured_arch not in distribution["architectures"]:
            raise ValueError("architecture mismatch")
        # Presence is not signature verification or a complete publication proof.
        release = _regular_file(root / "dists" / distribution["codename"] / "InRelease", 512 * 1024)
        key = _regular_file(root / "repository.gpg", 256 * 1024)
    except (OSError, ValueError, UnicodeError, KeyError):
        return "warning", "Repository configuration cannot be confirmed", {"listener_active": active}
    details = {"listener_active": active, "configuration_valid": True,
               "signed_release_present": bool(release), "public_key_present": bool(key)}
    if _FINGERPRINT.fullmatch(fingerprint):
        details["signing_fingerprint"] = fingerprint
    if not release or not key:
        return "warning", "Repository public metadata is incomplete", details
    return "ok", "Repository configuration and public metadata are present", details


def build_system_diagnostics(*, runtime, load_configuration, load_secret_document,
                             execution_manager=None, mutation_gate=None,
                             validation_manager=None, scheduler=None, orchestrator=None,
                             repository_active=False) -> dict:
    """Build a reusable snapshot; each independent probe fails closed to unknown.

    Callers inject the existing process services. This function never starts them.
    No exception text, filesystem path, command output, or subsystem dictionary
    is copied to the response.
    """
    checks = []

    def add(check_id, probe):
        try:
            status, message, details = probe()
            if status not in STATUSES or not isinstance(details, dict):
                raise ValueError("invalid probe contract")
        except Exception:
            status, message, details = "unknown", "Check could not be completed", {}
        checks.append({"id": check_id, "status": status, "message": message, "details": details})

    def application():
        arch = native_debian_architecture()
        return "ok", "Application runtime identified", {
            "version": __version__, "recipe_schema_version": SCHEMA_VERSION,
            "run_schema_version": CURRENT_RUN_SCHEMA_VERSION,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "debian_architecture": arch,
        }

    configuration = None

    def settings():
        nonlocal configuration
        configuration = load_configuration()
        load_secret_document()
        mode = configuration["security"]["auth_mode"]
        if mode not in {"none", "header", "oidc"}:
            raise ValueError("invalid auth mode")
        return "ok", "Settings and secrets documents are valid", {"auth_mode": mode}

    def repository():
        if configuration is None:
            return "unknown", "Repository settings are unavailable", {}
        return _repository(runtime.repository_root, configuration["apt"], bool(repository_active))

    def oci():
        blocked = validation_manager is not None and validation_manager.blocker is not None
        installed = shutil.which("podman") is not None
        if not installed:
            return "warning", "Podman is not installed", {"podman_installed": False, "runtime_verified": False,
                                                           "admission_blocked": blocked}
        return ("warning" if blocked else "unknown"), (
            "Validation admission is blocked" if blocked else
            "Podman is installed but runtime and images were not probed"), {
            "podman_installed": True, "runtime_verified": False, "admission_blocked": blocked,
        }

    def admission():
        if execution_manager is None:
            return "warning", "Execution manager is unavailable", {"open": False}
        blocker = execution_manager.admission_blocker
        open_now = bool(execution_manager.accepting) and blocker is None
        details = {"open": open_now, "recovery_blocked": blocker is not None}
        return ("ok" if open_now else "warning", "Build/Test admission is open" if open_now else
                "Build/Test admission is closed", details)

    def containment():
        capability = command_containment.cached_containment_capability()
        backend = capability.backend if capability.backend in {"systemd", "systemd_cgroup", "process_group", "unresolved", "unknown"} else "unknown"
        details = {"backend": backend, "available": bool(capability.available)}
        return ("ok" if capability.available else "warning", "Command containment is available" if
                capability.available else "Command containment is unavailable or unverified", details)

    def mutations():
        accepting = mutation_gate is not None and mutation_gate.accepting
        return ("ok" if accepting else "warning", "Durable mutations are open" if accepting else
                "Durable mutations are closed", {"open": accepting})

    def automation_scheduler():
        if scheduler is None:
            return "warning", "Automation scheduler is unavailable", {"running": False}
        state = scheduler.status()
        running = state.get("state") == "running" and state.get("admission_open") is True
        return ("ok" if running else "warning", "Automation scheduler is available" if running else
                "Automation scheduler is not accepting work", {"running": running,
                "checks_enabled": state.get("checks_enabled") is True})

    def automation_orchestrator():
        available = orchestrator is not None
        return ("ok" if available else "warning", "Automation orchestrator is available" if available else
                "Automation orchestrator is unavailable", {"available": available})

    probes = (application, settings, repository, oci, admission, containment,
              mutations, automation_scheduler, automation_orchestrator)
    for check_id, probe in zip(CHECK_IDS, probes, strict=True):
        add(check_id, probe)
    statuses = {row["status"] for row in checks}
    global_status = "failed" if "failed" in statuses else "warning" if statuses & {"warning", "unknown"} else "ok"
    return {"schema_version": SCHEMA_VERSION_DIAGNOSTICS, "status": global_status, "checks": checks}
