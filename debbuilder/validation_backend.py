"""Replaceable execution backends for Debian installation validation."""
from __future__ import annotations

import json
import shlex
import shutil
import time
from pathlib import Path

from .command_runner import run_command


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class OciSystemdBackend:
    """Run validation commands in a disposable, systemd-enabled OCI container."""

    def __init__(self, workspace: str | Path, *, image: str = "debbuilder-validation:bookworm", runtime: str = "", runner=run_command, on_result=None):
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.runtime = runtime or next((name for name in ("podman", "docker") if shutil.which(name)), "")
        self.runner = runner
        self.on_result = on_result
        self.container = ""
        self.index = 0

    def _run(self, arguments: list[str], *, timeout: int = 120) -> dict:
        self.index += 1
        command = " ".join(shlex.quote(str(value)) for value in arguments)
        result = self.runner(command, workspace=self.workspace, working_directory=".", environment={"LC_ALL": "C"}, timeout=timeout)
        result["index"] = self.index
        if self.on_result:
            self.on_result(result)
        return result

    def start(self, validation_id: str) -> dict:
        if not self.runtime:
            raise BackendError("validation_backend_unavailable", "No supported isolated validation runtime is available; install Docker or Podman")
        image_check = self._run([self.runtime, "image", "inspect", self.image], timeout=30)
        if image_check["status"] != "success":
            raise BackendError(
                "validation_image_unavailable",
                f"Validation image {self.image} is unavailable; build it from validation/Dockerfile",
                details={"command": image_check},
            )
        try:
            inspected = json.loads(image_check.get("stdout") or "[]")[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            inspected = {}
        self.container = f"debbuilder-{validation_id}"
        result = self._run([
            self.runtime, "run", "--detach", "--rm", "--privileged", "--name", self.container,
            "--network", "none", "--systemd", "always", "--tmpfs", "/run", "--tmpfs", "/run/lock", "--volume", f"{self.workspace}:/validation:ro",
            self.image, "/sbin/init",
        ])
        if result["status"] != "success":
            self.container = ""
            raise BackendError("validation_environment_failed", result["stderr"] or "Unable to start validation container", details={"command": result})
        ready = None
        for _attempt in range(20):
            ready = self.exec(["systemctl", "is-system-running", "--wait"], timeout=10, accepted_exit_codes={0, 1})
            if ready.get("accepted") and "Failed to connect to bus" not in ready.get("stderr", ""):
                break
            time.sleep(0.25)
        assert ready is not None
        if not ready.get("accepted") or "Failed to connect to bus" in ready.get("stderr", ""):
            raise BackendError("validation_systemd_unavailable", ready.get("stderr") or "systemd did not become available", details={"command": ready})
        digests = inspected.get("RepoDigests") or []
        return {"runtime": self.runtime, "image": self.image, "image_id": inspected.get("Id", ""), "image_digest": digests[0] if digests else inspected.get("Digest", ""), "network": "disabled", "container": self.container, "start": result, "systemd": ready}

    def exec(self, arguments: list[str], *, timeout: int = 120, accepted_exit_codes: set[int] | None = None) -> dict:
        if not self.container:
            raise BackendError("validation_environment_not_started", "Validation environment is not running")
        result = self._run([self.runtime, "exec", self.container, *arguments], timeout=timeout)
        accepted = {0} if accepted_exit_codes is None else accepted_exit_codes
        result["accepted"] = result.get("exit_code") in accepted and not result.get("timed_out")
        return result

    def stop(self) -> dict | None:
        if not self.container:
            return None
        container, self.container = self.container, ""
        return self._run([self.runtime, "rm", "--force", container], timeout=30)
