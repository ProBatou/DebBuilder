"""Replaceable execution backends for Debian installation validation."""
from __future__ import annotations

import time
import threading
from pathlib import Path

from .command_runner import run_command
from .validation_oci import (
    IdentityRegistry,
    OciOwnershipError,
    OwnedContainer,
    PodmanRuntime,
    new_identity,
    verify_container_configuration,
    verify_owned_container,
    verify_resource_enforcement,
)


LIFECYCLE_APT_TMPFS_BYTES = 8 * 1024 * 1024


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class OwnedOciSystemdBackend:
    """Networkless lifecycle backend using CP1B's durable OCI ownership."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        image: str,
        run_id: str,
        attempt_id: str,
        registry_root: str | Path,
        resource_policy: dict,
        mounts: list[tuple[Path, str, str]],
        expected_image: dict,
        runner=run_command,
        cancellation_event: threading.Event | None = None,
        on_result=None,
    ):
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.registry = IdentityRegistry(registry_root)
        self.resource_policy = resource_policy
        self.mounts = mounts
        self.expected_image = dict(expected_image)
        self.on_result = on_result
        self.index = 0
        self.container: OwnedContainer | None = None
        self.runtime = PodmanRuntime(
            self.workspace,
            runner=runner,
            cancellation_event=cancellation_event,
            on_result=self._record,
        )

    def _record(self, result: dict) -> None:
        self.index += 1
        result["index"] = self.index
        if self.on_result:
            self.on_result(result)

    @staticmethod
    def _backend_error(exc: OciOwnershipError) -> BackendError:
        return BackendError(exc.code, str(exc), details=exc.details)

    def start(self, validation_id: str) -> dict:
        if validation_id != self.attempt_id:
            raise BackendError("validation_attempt_identity_mismatch", "Lifecycle validation identity changed before container creation")
        try:
            existing, blockers = self.registry.load_all()
            if existing or blockers:
                raise OciOwnershipError(
                    "validation_container_overlap",
                    "Dependency preparation absence was not proved before lifecycle creation",
                )
            image = self.runtime.inspect_image(self.image)
            if image != self.expected_image:
                raise OciOwnershipError(
                    "validation_image_identity_mismatch",
                    "Selected lifecycle image no longer matches dependency preparation",
                )
            identity = new_identity(
                run_id=self.run_id,
                attempt_id=self.attempt_id,
                role="lifecycle",
                runtime="podman",
                image=image,
            )
            self.container = OwnedContainer(self.runtime, self.registry, identity)
            create = self.container.create(
                mounts=self.mounts,
                tmpfs=[
                    ("/var/lib/apt/lists", LIFECYCLE_APT_TMPFS_BYTES),
                    ("/var/cache/apt/archives", LIFECYCLE_APT_TMPFS_BYTES),
                ],
                policy=self.resource_policy,
                network="none",
            )
            _facts, launch = self.container.start(policy=self.resource_policy)
            inspected = self.runtime.inspect_container(self.container.identity["container_id"])
            if inspected is None:
                raise OciOwnershipError("validation_container_start_failed", "Lifecycle container disappeared after start")
            facts = verify_owned_container(self.container.identity, inspected)
            verify_container_configuration(self.container.identity, inspected)
            enforcement = verify_resource_enforcement(
                self.container.identity,
                inspected,
                self.resource_policy,
                workspace=self.workspace,
                launcher_result=launch,
            )
            network_probe = self.container.exec([
                "python3", "-c",
                "import pathlib,socket,sys; names={n for _,n in socket.if_nameindex()}; "
                "routes=pathlib.Path('/proc/net/route').read_text().splitlines()[1:]; "
                "sys.exit(0 if names <= {'lo'} and not routes else 1)",
            ], timeout=30, accepted_exit_codes={0}, check=False)
            if not network_probe.get("accepted"):
                raise OciOwnershipError(
                    "validation_network_isolation_unverified",
                    "Lifecycle container has a non-loopback interface or route",
                    details={"command": network_probe},
                )
            ready = None
            for _attempt in range(20):
                ready = self.exec(
                    ["systemctl", "is-system-running", "--wait"],
                    timeout=10,
                    accepted_exit_codes={0, 1},
                )
                if ready.get("accepted") and "Failed to connect to bus" not in ready.get("stderr", ""):
                    break
                time.sleep(0.25)
            if ready is None or not ready.get("accepted") or "Failed to connect to bus" in ready.get("stderr", ""):
                raise BackendError(
                    "validation_systemd_unavailable",
                    (ready or {}).get("stderr") or "systemd did not become available",
                    details={"command": ready or {}},
                )
            return {
                "runtime": "podman",
                "image": self.image,
                "image_id": image["id"],
                "image_digest": image["digest"],
                "network": "disabled",
                "network_verified": True,
                "container": facts["name"],
                "container_id": facts["id"],
                "create": create,
                "systemd": ready,
                "resource_enforcement": enforcement,
            }
        except BackendError:
            raise
        except OciOwnershipError as exc:
            raise self._backend_error(exc) from exc

    def exec(self, arguments: list[str], *, timeout: int = 120, accepted_exit_codes: set[int] | None = None) -> dict:
        if self.container is None:
            raise BackendError("validation_environment_not_started", "Validation environment is not running")
        try:
            return self.container.exec(
                arguments,
                timeout=timeout,
                accepted_exit_codes={0} if accepted_exit_codes is None else accepted_exit_codes,
                check=False,
            )
        except OciOwnershipError as exc:
            raise self._backend_error(exc) from exc

    def stop(self) -> dict | None:
        if self.container is None:
            return None
        container, self.container = self.container, None
        identity = dict(container.identity)
        try:
            container.cleanup()
        except OciOwnershipError as exc:
            raise self._backend_error(exc) from exc
        return {"status": "success", "container_id": identity.get("container_id"), "absence_proved": True}
