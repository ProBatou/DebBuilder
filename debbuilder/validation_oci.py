"""Durable ownership and fail-closed recovery for validation OCI containers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import storage
from .build_models import utc_now
from .command_runner import run_command
from .command_identity import suspended_identity_recording
from .recipe_schema import require_safe_name
from .resource_limits import normalize_policy


IDENTITY_SCHEMA_VERSION = 1
OWNER_LABEL = "io.debbuilder.validation.owner"
OWNER_VALUE = "DebBuilder/1"
RUN_LABEL = "io.debbuilder.validation.run-id"
ATTEMPT_LABEL = "io.debbuilder.validation.attempt-id"
ROLE_LABEL = "io.debbuilder.validation.role"
TOKEN_LABEL = "io.debbuilder.validation.identity-token"
SCHEMA_LABEL = "io.debbuilder.validation.identity-schema"
NAME_PREFIX = "debbuilder-validation-"
ROLES = frozenset({"dependency-preparation", "lifecycle"})
MAX_IDENTITIES = 512
MAX_INVENTORY_BYTES = 2 * 1024 * 1024
MAX_IDENTITY_BYTES = 16 * 1024
TOKEN = re.compile(r"^[0-9a-f]{32}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
PODMAN_DEFAULT_CAPABILITIES = frozenset({
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL",
    "CAP_NET_BIND_SERVICE", "CAP_SETFCAP", "CAP_SETGID", "CAP_SETPCAP", "CAP_SETUID", "CAP_SYS_CHROOT",
})


class OciOwnershipError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class OciRecoveryResult:
    recovered: list[str] = field(default_factory=list)
    cleared_absent: list[str] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    resolved_identities: list[dict] = field(default_factory=list, repr=False)
    inventory_trustworthy: bool = False

    @property
    def admission_blocker(self) -> dict | None:
        if not self.blockers:
            return None
        return {
            "code": "validation_container_recovery_unresolved",
            "message": "Build/Test admission is blocked until validation container recovery is resolved",
            "details": {"unresolved_count": len(self.blockers)},
        }

    def as_dict(self) -> dict:
        return {
            "recovered": list(self.recovered),
            "cleared_absent": list(self.cleared_absent),
            "blockers": [dict(row) for row in self.blockers],
            "resolved_attempt_count": len(self.resolved_identities),
            "inventory_trustworthy": self.inventory_trustworthy,
            "admission_blocked": self.admission_blocker is not None,
        }


def ownership_labels(identity: dict) -> dict[str, str]:
    """Return the one authoritative exact ownership-label set."""
    return {
        OWNER_LABEL: OWNER_VALUE,
        RUN_LABEL: str(identity["run_id"]),
        ATTEMPT_LABEL: str(identity["attempt_id"]),
        ROLE_LABEL: str(identity["role"]),
        TOKEN_LABEL: str(identity["identity_token"]),
        SCHEMA_LABEL: str(IDENTITY_SCHEMA_VERSION),
    }


def new_identity(*, run_id: str, attempt_id: str, role: str, runtime: str, image: dict) -> dict:
    require_safe_name(run_id, "build run id")
    require_safe_name(attempt_id, "validation attempt id")
    if role not in ROLES:
        raise ValueError("validation container role is invalid")
    if runtime != "podman":
        raise OciOwnershipError("validation_runtime_unsupported", "CP1B ownership requires Podman")
    image_name = str((image or {}).get("name") or "")
    image_id = str((image or {}).get("id") or "").lower()
    if not image_name or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("exact validation image name and SHA-256 ID are required")
    token = secrets.token_hex(16)
    readable = re.sub(r"[^a-z0-9_.-]", "-", attempt_id.lower())[:40].strip(".-") or "attempt"
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "role": role,
        "identity_token": token,
        "name": f"{NAME_PREFIX}{readable}-{token[:12]}",
        "scope_name": f"debbuilder-validation-{token}.scope",
        "runtime": runtime,
        "image": {"name": image_name, "id": image_id, "digest": (image or {}).get("digest")},
        "container_id": None,
        "configuration": None,
        "state": "planned",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "cleanup_error": None,
    }


def validated_identity(value) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "run_id", "attempt_id", "role", "identity_token", "name", "scope_name",
        "runtime", "image", "container_id", "configuration", "state", "created_at", "updated_at", "cleanup_error",
    }:
        raise OciOwnershipError("invalid_validation_container_identity", "validation container identity fields are invalid")
    if value["schema_version"] != IDENTITY_SCHEMA_VERSION or value["runtime"] != "podman":
        raise OciOwnershipError("invalid_validation_container_identity", "validation container identity version or runtime is unsupported")
    require_safe_name(value["run_id"], "build run id")
    require_safe_name(value["attempt_id"], "validation attempt id")
    if value["role"] not in ROLES or not TOKEN.fullmatch(str(value["identity_token"])):
        raise OciOwnershipError("invalid_validation_container_identity", "validation container ownership fields are invalid")
    if not isinstance(value["name"], str) or not value["name"].startswith(NAME_PREFIX) or len(value["name"]) > 128:
        raise OciOwnershipError("invalid_validation_container_identity", "validation container name is invalid")
    if value["scope_name"] != f"debbuilder-validation-{value['identity_token']}.scope":
        raise OciOwnershipError("invalid_validation_container_identity", "validation container scope identity is invalid")
    image = value["image"]
    if not isinstance(image, dict) or set(image) != {"name", "id", "digest"} or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image["id"]).lower()):
        raise OciOwnershipError("invalid_validation_container_identity", "validation image identity is invalid")
    container_id = value["container_id"]
    if container_id is not None and not CONTAINER_ID.fullmatch(str(container_id).lower()):
        raise OciOwnershipError("invalid_validation_container_identity", "runtime container ID is invalid")
    configuration = value["configuration"]
    if configuration is not None:
        try:
            valid_shape = isinstance(configuration, dict) and set(configuration) == {
                "network", "mounts", "tmpfs", "policy", "io_devices", "create_argv_sha256",
            }
            expected_network = "bridge" if value["role"] == "dependency-preparation" else "none"
            valid_shape = valid_shape and configuration["network"] == expected_network
            valid_shape = valid_shape and normalize_policy(configuration["policy"]) == configuration["policy"]
            valid_shape = valid_shape and isinstance(configuration["mounts"], list) and len(configuration["mounts"]) <= 64
            valid_shape = valid_shape and all(
                isinstance(row, dict) and set(row) == {"source", "destination", "read_only"}
                and isinstance(row["source"], str) and row["source"].startswith("/")
                and isinstance(row["destination"], str) and row["destination"].startswith("/")
                and isinstance(row["read_only"], bool)
                for row in configuration["mounts"]
            )
            valid_shape = valid_shape and isinstance(configuration["tmpfs"], list) and len(configuration["tmpfs"]) <= 16
            valid_shape = valid_shape and all(
                isinstance(row, dict) and set(row) == {"destination", "size"}
                and isinstance(row["destination"], str) and row["destination"].startswith("/")
                and isinstance(row["size"], int) and not isinstance(row["size"], bool) and row["size"] > 0
                for row in configuration["tmpfs"]
            )
            valid_shape = valid_shape and isinstance(configuration["io_devices"], list) and len(configuration["io_devices"]) <= 4
            valid_shape = valid_shape and all(
                isinstance(row, dict) and set(row) == {"control", "path", "major", "minor", "rate"}
                and row["control"] in {"io_read", "io_write"} and isinstance(row["path"], str) and row["path"].startswith("/dev/")
                and all(isinstance(row[key], int) and not isinstance(row[key], bool) and row[key] >= 0 for key in ("major", "minor"))
                and isinstance(row["rate"], int) and not isinstance(row["rate"], bool) and row["rate"] > 0
                for row in configuration["io_devices"]
            )
        except (KeyError, TypeError, ValueError):
            valid_shape = False
        if not valid_shape or not re.fullmatch(r"[0-9a-f]{64}", str(configuration.get("create_argv_sha256", ""))):
            raise OciOwnershipError("invalid_validation_container_identity", "validation container command identity is invalid")
    if value["state"] not in {"planned", "created", "running", "cleanup_failed"}:
        raise OciOwnershipError("invalid_validation_container_identity", "validation container state is invalid")
    if value["cleanup_error"] is not None and (not isinstance(value["cleanup_error"], str) or len(value["cleanup_error"]) > 1000):
        raise OciOwnershipError("invalid_validation_container_identity", "validation cleanup error is invalid")
    return value


class IdentityRegistry:
    """Bounded private registry; identity filenames are unguessable ownership tokens."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def path(self, identity: dict) -> Path:
        return self.root / f"{identity['identity_token']}.json"

    def persist(self, identity: dict) -> Path:
        identity = validated_identity(identity)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_info = self.root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(root_info.st_mode):
                raise OciOwnershipError("validation_identity_unverifiable", "validation identity registry is not a directory")
            path = self.path(identity)
            storage.save_json(path, identity)
            path.chmod(0o600)
            return path

    def clear(self, identity: dict) -> None:
        with self._lock:
            self.path(validated_identity(identity)).unlink(missing_ok=True)

    def load_all(self) -> tuple[list[tuple[Path, dict]], list[dict]]:
        if not self.root.exists():
            return [], []
        try:
            root_info = self.root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(root_info.st_mode):
                return [], [{"code": "validation_identity_unverifiable", "reason": "validation identity registry is not a directory"}]
        except OSError as exc:
            return [], [{"code": "validation_identity_unverifiable", "reason": str(exc)[:1000]}]
        rows: list[tuple[Path, dict]] = []
        blockers: list[dict] = []
        with os.scandir(self.root) as entries:
            materialized = list(entries)
        if len(materialized) > MAX_IDENTITIES:
            return [], [{"code": "validation_identity_inventory_too_large", "reason": "validation identity registry exceeds its bounded count"}]
        for entry in sorted(materialized, key=lambda row: row.name):
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_IDENTITY_BYTES:
                    raise OciOwnershipError("invalid_validation_container_identity", "identity record is not a bounded regular file")
                if not re.fullmatch(r"[0-9a-f]{32}\.json", entry.name):
                    raise OciOwnershipError("invalid_validation_container_identity", "identity filename is invalid")
                descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                        raise OciOwnershipError("invalid_validation_container_identity", "identity record changed during inspection")
                    raw = os.read(descriptor, MAX_IDENTITY_BYTES + 1)
                finally:
                    os.close(descriptor)
                value = validated_identity(json.loads(raw.decode("utf-8")))
                if entry.name != f"{value['identity_token']}.json":
                    raise OciOwnershipError("invalid_validation_container_identity", "identity filename does not match its token")
                rows.append((Path(entry.path), value))
            except (OSError, ValueError, TypeError, json.JSONDecodeError, OciOwnershipError) as exc:
                blockers.append({"code": "validation_identity_unverifiable", "reason": str(exc)[:1000], "record": entry.name[:128]})
        return rows, blockers


class PodmanRuntime:
    """Structured Podman boundary with cancellable workload and image pulls."""

    def __init__(self, workspace: str | Path, *, runner=run_command, cancellation_event=None, on_result=None):
        self.workspace = Path(workspace).resolve()
        self.runner = runner
        self.cancellation_event = cancellation_event
        self.on_result = on_result

    def run(
        self,
        arguments: list[str],
        *,
        timeout: float,
        workload: bool = False,
        cancellable_control: bool = False,
        output_limit: int | None = None,
    ) -> dict:
        effective_arguments = list(arguments)
        if output_limit is not None:
            if not 1 <= output_limit <= MAX_INVENTORY_BYTES:
                raise ValueError("OCI query output limit is invalid")
            effective_arguments = [
                sys.executable, str(Path(__file__).with_name("bounded_process.py")),
                "--limit", str(output_limit), "--", *effective_arguments,
            ]
        command = " ".join(shlex.quote(str(value)) for value in effective_arguments)
        kwargs = {
            "workspace": self.workspace, "working_directory": ".", "environment": {"LC_ALL": "C"},
            "timeout": timeout, "inactivity_timeout": timeout,
        }
        if workload or cancellable_control:
            kwargs["cancellation_event"] = self.cancellation_event
        if workload:
            result = self.runner(command, **kwargs)
        else:
            # Read-only inventory and cleanup clients are control-plane work;
            # the container payload and create/start/copy clients carry the
            # admitted validation policy. Re-probing transient systemd scopes
            # for every inspect command introduces a race without constraining
            # any workload.
            with suspended_identity_recording():
                result = self.runner(command, **kwargs)
        if callable(self.on_result):
            self.on_result(result)
        return result

    @staticmethod
    def require_success(result: dict, code: str, message: str) -> dict:
        if result.get("status") != "success":
            raise OciOwnershipError(code, message, details={"command": result})
        return result

    def inspect_image(self, image_name: str) -> dict:
        result = self.require_success(
            self.run(["podman", "image", "inspect", image_name], timeout=30, output_limit=MAX_INVENTORY_BYTES),
            "validation_image_unavailable", f"Validation image {image_name} is unavailable",
        )
        try:
            row = json.loads(result.get("stdout") or "[]")[0]
            image_id = str(row["Id"]).lower()
            if re.fullmatch(r"[0-9a-f]{64}", image_id):
                image_id = "sha256:" + image_id
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ValueError("image ID is not exact")
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise OciOwnershipError("validation_image_unverifiable", "Podman returned malformed image identity") from exc
        digests = row.get("RepoDigests") or []
        return {"name": image_name, "id": image_id, "digest": digests[0] if digests else None}

    def inspect_container(self, reference: str) -> dict | None:
        result = self.run(
            ["podman", "container", "inspect", reference],
            timeout=30, output_limit=MAX_INVENTORY_BYTES,
        )
        if result.get("status") != "success":
            stderr = str(result.get("stderr") or "").lower()
            if result.get("exit_code") in {1, 125} and ("no such" in stderr or "does not exist" in stderr):
                return None
            reason = str(result.get("stderr") or "Podman container inspection failed")[:1000]
            raise OciOwnershipError("validation_container_inventory_unavailable", reason, details={"command": result})
        try:
            row = json.loads(result.get("stdout") or "[]")[0]
        except (ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
            raise OciOwnershipError("validation_container_inventory_unavailable", "Podman returned malformed container inspection") from exc
        return row

    def namespace_candidates(self) -> list[dict]:
        found: dict[str, dict] = {}
        result = self.require_success(
            self.run(
                ["podman", "ps", "--all", "--format", "json"],
                timeout=30, output_limit=MAX_INVENTORY_BYTES,
            ),
            "validation_container_inventory_unavailable", "Podman namespace inventory failed",
        )
        output = str(result.get("stdout") or "")
        if len(output.encode("utf-8")) > MAX_INVENTORY_BYTES:
            raise OciOwnershipError("validation_container_inventory_unavailable", "Podman namespace inventory exceeds its byte bound")
        try:
            rows = json.loads(output or "[]")
        except json.JSONDecodeError as exc:
            raise OciOwnershipError("validation_container_inventory_unavailable", "Podman returned malformed namespace inventory") from exc
        if not isinstance(rows, list) or len(rows) > MAX_IDENTITIES:
            raise OciOwnershipError("validation_container_inventory_unavailable", "Podman namespace inventory exceeds its count bound")
        for row in rows:
            if not isinstance(row, dict):
                raise OciOwnershipError("validation_container_inventory_unavailable", "Podman inventory row is malformed")
            names = row.get("Names") or row.get("Name") or []
            if isinstance(names, str):
                names = [names]
            labels = row.get("Labels") or {}
            if isinstance(labels, str):
                labels = dict(partition for item in labels.split(",") if "=" in item for partition in [item.split("=", 1)])
            namespace_looking = any(str(name).lstrip("/").startswith(NAME_PREFIX) for name in names) or any(
                str(key).startswith("io.debbuilder.validation.") for key in labels
            )
            if not namespace_looking:
                continue
            candidate = str(row.get("Id") or row.get("ID") or "").lower()
            if not CONTAINER_ID.fullmatch(candidate):
                raise OciOwnershipError("validation_container_inventory_unavailable", "Podman inventory contained an invalid container ID")
            found[candidate] = row
        return [found[key] for key in sorted(found)]

    @staticmethod
    def verify_payload_capabilities(pid: int) -> None:
        verify_payload_capabilities(pid)


def _inspect_facts(inspected: dict) -> dict:
    config = inspected.get("Config") or {}
    state = inspected.get("State") or {}
    image_id = str(inspected.get("Image") or inspected.get("ImageDigest") or "").lower()
    if not image_id.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", image_id):
        image_id = "sha256:" + image_id
    return {
        "id": str(inspected.get("Id") or "").lower(),
        "name": str(inspected.get("Name") or "").lstrip("/"),
        "labels": config.get("Labels") or {},
        "image_id": image_id,
        "running": bool(state.get("Running")),
        "pid": int(state.get("Pid") or 0),
    }


def verify_owned_container(identity: dict, inspected: dict) -> dict:
    identity = validated_identity(identity)
    facts = _inspect_facts(inspected)
    expected_id = str(identity.get("container_id") or "").lower()
    if expected_id and facts["id"] != expected_id:
        raise OciOwnershipError("validation_container_identity_mismatch", "runtime container ID does not match durable identity")
    expected_labels = ownership_labels(identity)
    owned_labels = {key: facts["labels"].get(key) for key in expected_labels}
    extra_owned_labels = [key for key in facts["labels"] if key.startswith("io.debbuilder.validation.") and key not in expected_labels]
    if facts["name"] != identity["name"] or owned_labels != expected_labels or extra_owned_labels:
        raise OciOwnershipError("validation_container_identity_mismatch", "container name or exact ownership labels do not match durable identity")
    if facts["image_id"] != identity["image"]["id"]:
        raise OciOwnershipError("validation_container_identity_mismatch", "container image ID does not match durable identity")
    return facts


def _configuration_digest(arguments: list[str]) -> str:
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def verify_container_configuration(identity: dict, inspected: dict) -> None:
    """Authenticate the complete create-time isolation contract before start/recovery."""
    configuration = validated_identity(identity)["configuration"]
    if configuration is None:
        raise OciOwnershipError("validation_container_configuration_unverifiable", "durable create configuration is absent")
    host = inspected.get("HostConfig") or {}
    config = inspected.get("Config") or {}
    create_command = config.get("CreateCommand") or []
    lifecycle = identity["role"] == "lifecycle"
    expected_cap_drop = set() if lifecycle else PODMAN_DEFAULT_CAPABILITIES
    expected_security = set() if lifecycle else {"no-new-privileges"}
    if (
        not isinstance(create_command, list)
        or _configuration_digest([str(value) for value in create_command]) != configuration["create_argv_sha256"]
        or host.get("NetworkMode") != configuration["network"]
        or host.get("ReadonlyRootfs") is not (not lifecycle)
        or host.get("Privileged") is not False
        or (host.get("CapAdd") or []) != []
        or set(host.get("CapDrop") or []) != expected_cap_drop
        or set(host.get("SecurityOpt") or []) != expected_security
    ):
        raise OciOwnershipError("validation_container_configuration_mismatch", "runtime security configuration does not match durable intent")
    expected_mounts = {
        (row["source"], row["destination"], row["read_only"])
        for row in configuration["mounts"]
    }
    actual_mounts = {
        (str(row.get("Source") or ""), str(row.get("Destination") or ""), not bool(row.get("RW")))
        for row in (inspected.get("Mounts") or []) if row.get("Type") == "bind"
    }
    if actual_mounts != expected_mounts:
        raise OciOwnershipError("validation_container_configuration_mismatch", "runtime bind mounts do not match durable intent")
    actual_tmpfs = host.get("Tmpfs") or {}
    if set(actual_tmpfs) != {row["destination"] for row in configuration["tmpfs"]}:
        raise OciOwnershipError("validation_container_configuration_mismatch", "runtime tmpfs mounts do not match durable intent")
    for row in configuration["tmpfs"]:
        option_rows = str(actual_tmpfs[row["destination"]]).split(",")
        options = set(option_rows)
        if (
            len(options) != len(option_rows)
            or not {"rw", "nosuid", "nodev", "noexec", f"size={row['size']}"}.issubset(options)
            or options.intersection({"ro", "suid", "dev", "exec"})
            or sum(option.startswith("size=") for option in options) != 1
        ):
            raise OciOwnershipError("validation_container_configuration_mismatch", "runtime tmpfs options do not match durable intent")
    policy = configuration["policy"]
    expected_scalars = {}
    if policy["memory_max_bytes"] is not None:
        expected_scalars["Memory"] = policy["memory_max_bytes"]
    if policy["tasks_max"] is not None:
        expected_scalars["PidsLimit"] = policy["tasks_max"]
    if policy["cpu_quota_percent"] is not None:
        expected_scalars.update({"CpuPeriod": 100000, "CpuQuota": policy["cpu_quota_percent"] * 1000})
    if any(host.get(key, 0) != value for key, value in expected_scalars.items()):
        raise OciOwnershipError("validation_container_configuration_mismatch", "runtime resource configuration does not match durable intent")
    for control, host_field in (("io_read", "BlkioDeviceReadBps"), ("io_write", "BlkioDeviceWriteBps")):
        expected = {(row["path"], row["rate"]) for row in configuration["io_devices"] if row["control"] == control}
        actual = {(str(row.get("Path") or ""), row.get("Rate")) for row in (host.get(host_field) or [])}
        if actual != expected:
            raise OciOwnershipError("validation_container_configuration_mismatch", "runtime I/O configuration does not match durable intent")


def _block_device(path: Path, *, sys_dev_block: Path = Path("/sys/dev/block"), dev_root: Path = Path("/dev")) -> tuple[Path, tuple[int, int]]:
    device = os.stat(path, follow_symlinks=False).st_dev
    node = sys_dev_block / f"{os.major(device)}:{os.minor(device)}"
    try:
        resolved = node.resolve(strict=True)
    except OSError as exc:
        raise OciOwnershipError("validation_io_enforcement_unavailable", "I/O backing device cannot be resolved") from exc
    parts = resolved.parts
    try:
        block_index = parts.index("block")
        block_name = parts[block_index + 1]
    except (ValueError, IndexError) as exc:
        raise OciOwnershipError("validation_io_enforcement_unavailable", "I/O backing device topology is unsupported") from exc
    block_path = dev_root / block_name
    try:
        info = os.stat(block_path, follow_symlinks=False)
    except OSError as exc:
        raise OciOwnershipError("validation_io_enforcement_unavailable", "I/O backing block device is unavailable") from exc
    if not stat.S_ISBLK(info.st_mode):
        raise OciOwnershipError("validation_io_enforcement_unavailable", "I/O backing path is not a block device")
    return block_path, (os.major(info.st_rdev), os.minor(info.st_rdev))


def resource_arguments(policy: dict, workspace: Path) -> tuple[list[str], list[dict]]:
    policy = normalize_policy(policy)
    arguments: list[str] = []
    devices: list[dict] = []
    if policy["memory_max_bytes"] is not None:
        arguments += ["--memory", str(policy["memory_max_bytes"])]
    if policy["tasks_max"] is not None:
        arguments += ["--pids-limit", str(policy["tasks_max"])]
    if policy["cpu_quota_percent"] is not None:
        arguments += ["--cpu-period", "100000", "--cpu-quota", str(policy["cpu_quota_percent"] * 1000)]
    for field, option, label in (
        ("io_read_bandwidth_max_bytes_per_sec", "--device-read-bps", "io_read"),
        ("io_write_bandwidth_max_bytes_per_sec", "--device-write-bps", "io_write"),
    ):
        value = policy[field]
        if value is None:
            continue
        seen: set[str] = set()
        for target in (Path("/"), workspace):
            block, major_minor = _block_device(target)
            key = str(block)
            if key in seen:
                continue
            seen.add(key)
            arguments += [option, f"{block}:{value}"]
            devices.append({
                "control": label, "path": key, "major": major_minor[0], "minor": major_minor[1], "rate": value,
            })
    return arguments, devices


def scope_resource_arguments(policy: dict, workspace: Path) -> list[str]:
    """Map the same immutable policy to the Podman/conmon/payload scope."""
    policy = normalize_policy(policy)
    properties = ["--property", "Delegate=yes", "--property", "KillMode=control-group"]
    if policy["memory_max_bytes"] is not None:
        properties += ["--property", f"MemoryMax={policy['memory_max_bytes']}"]
    if policy["tasks_max"] is not None:
        properties += ["--property", f"TasksMax={policy['tasks_max']}"]
    if policy["cpu_quota_percent"] is not None:
        properties += ["--property", f"CPUQuota={policy['cpu_quota_percent']}%"]
    for field, property_name in (
        ("io_read_bandwidth_max_bytes_per_sec", "IOReadBandwidthMax"),
        ("io_write_bandwidth_max_bytes_per_sec", "IOWriteBandwidthMax"),
    ):
        if policy[field] is None:
            continue
        seen: set[str] = set()
        for target in (Path("/"), workspace):
            block, _major_minor = _block_device(target)
            if str(block) in seen:
                continue
            seen.add(str(block))
            properties += ["--property", f"{property_name}={block} {policy[field]}"]
    return properties


def _cgroup_files(pid: int) -> Path:
    if pid <= 0:
        raise OciOwnershipError("validation_resource_enforcement_unverified", "container payload PID is unavailable")
    try:
        lines = (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
        unified = next(line.split("::", 1)[1] for line in lines if line.startswith("0::"))
        root = Path("/sys/fs/cgroup").resolve()
        candidate = (root / unified.lstrip("/")).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError, StopIteration) as exc:
        raise OciOwnershipError("validation_resource_enforcement_unverified", "container payload cgroup is unavailable") from exc
    return candidate


def _read_control(root: Path, name: str) -> str:
    try:
        value = (root / name).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OciOwnershipError("validation_resource_enforcement_unverified", f"container {name} control is unavailable") from exc
    if len(value) > 8192:
        raise OciOwnershipError("validation_resource_enforcement_unverified", f"container {name} control is oversized")
    return value


def _owned_control_roots(payload_cgroup: Path, container_id: str) -> list[Path]:
    """Return only cgroups structurally bound to the exact Podman container ID."""
    # Unit tests replace _cgroup_files with a relative synthetic root.
    if not payload_cgroup.is_absolute():
        return [payload_cgroup]
    cgroup_root = Path("/sys/fs/cgroup").resolve()
    try:
        payload_cgroup.resolve(strict=True).relative_to(cgroup_root)
    except (OSError, ValueError) as exc:
        raise OciOwnershipError(
            "validation_resource_enforcement_unverified", "container cgroup path is outside the unified hierarchy",
        ) from exc
    expected_scope = f"libpod-{container_id}.scope"
    expected_payload = f"libpod-payload-{container_id}"
    roots = []
    current = payload_cgroup.resolve(strict=True)
    while current != cgroup_root:
        if current.name in {expected_scope, expected_payload} or (
            current.name == "container" and current.parent.name == expected_scope
        ):
            roots.append(current)
        current = current.parent
    if not roots:
        raise OciOwnershipError(
            "validation_resource_enforcement_unverified",
            "container payload cgroup is not bound to the exact Podman container identity",
        )
    return roots


def _owned_control_values(payload_cgroup: Path, container_id: str, name: str) -> list[str]:
    return [_read_control(root, name) for root in _owned_control_roots(payload_cgroup, container_id)]


def verify_payload_capabilities(pid: int) -> None:
    try:
        rows = (Path("/proc") / str(pid) / "status").read_text(encoding="ascii").splitlines()
        values = {
            key: value.strip() for line in rows if ":" in line
            for key, value in [line.split(":", 1)] if key in {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
        }
    except OSError as exc:
        raise OciOwnershipError("validation_container_configuration_unverifiable", "container capability state is unavailable") from exc
    if set(values) != {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"} or any(
        not re.fullmatch(r"0+", value) for value in values.values()
    ):
        raise OciOwnershipError("validation_container_configuration_mismatch", "container payload retains Linux capabilities")


def verify_resource_enforcement(
    identity: dict,
    inspected: dict,
    policy: dict,
    *,
    workspace: Path,
    launcher_result: dict,
) -> list[dict]:
    """Verify finite limits in runtime inspection and the payload cgroup."""
    facts = verify_owned_container(identity, inspected)
    policy = normalize_policy(policy)
    host = inspected.get("HostConfig") or {}
    cgroup = _cgroup_files(facts["pid"])
    container_id = facts["id"]
    observations: list[dict] = []

    launcher_control = launcher_result.get("resource_control") or {}
    finite = any(value is not None for value in policy.values())
    if finite and launcher_control.get("verification") != "verified":
        raise OciOwnershipError("validation_resource_enforcement_unverified", "Podman launcher containment was not verified")
    observations.append({
        "control": "launcher", "status": "enforced" if finite else "not_requested",
        "observed": "canonical command cgroup verified" if finite else "no finite policy requested",
    })

    memory = policy["memory_max_bytes"]
    if memory is None:
        observations.append({"control": "memory", "status": "not_requested", "observed": "unlimited by policy"})
    elif host.get("Memory") != memory or str(memory) not in _owned_control_values(cgroup, container_id, "memory.max"):
        raise OciOwnershipError("validation_resource_enforcement_unverified", "container memory limit does not match admitted policy")
    else:
        observations.append({"control": "memory", "status": "enforced", "observed": f"memory.max={memory}"})

    tasks = policy["tasks_max"]
    if tasks is None:
        observations.append({"control": "tasks", "status": "not_requested", "observed": "unlimited by policy"})
    elif host.get("PidsLimit") != tasks or str(tasks) not in _owned_control_values(cgroup, container_id, "pids.max"):
        raise OciOwnershipError("validation_resource_enforcement_unverified", "container task limit does not match admitted policy")
    else:
        observations.append({"control": "tasks", "status": "enforced", "observed": f"pids.max={tasks}"})

    cpu = policy["cpu_quota_percent"]
    expected_cpu = f"{cpu * 1000} 100000" if cpu is not None else ""
    if cpu is None:
        observations.append({"control": "cpu", "status": "not_requested", "observed": "unlimited by policy"})
    elif host.get("CpuPeriod") != 100000 or host.get("CpuQuota") != cpu * 1000 or expected_cpu not in _owned_control_values(cgroup, container_id, "cpu.max"):
        raise OciOwnershipError("validation_resource_enforcement_unverified", "container CPU limit does not match admitted policy")
    else:
        observations.append({"control": "cpu", "status": "enforced", "observed": f"cpu.max={expected_cpu}"})

    io_devices = (identity.get("configuration") or {}).get("io_devices") or []
    io_text = "\n".join(_owned_control_values(cgroup, container_id, "io.max")) if io_devices else ""
    for field, control, host_field, token in (
        ("io_read_bandwidth_max_bytes_per_sec", "io_read", "BlkioDeviceReadBps", "rbps"),
        ("io_write_bandwidth_max_bytes_per_sec", "io_write", "BlkioDeviceWriteBps", "wbps"),
    ):
        requested = policy[field]
        if requested is None:
            observations.append({"control": control, "status": "not_requested", "observed": "unlimited by policy"})
            continue
        runtime_rows = host.get(host_field) or []
        expected_devices = [row for row in io_devices if row["control"] == control]
        if not expected_devices:
            raise OciOwnershipError("validation_resource_enforcement_unverified", f"container {control} device intent is absent")
        if any(
            not any(runtime.get("Path") == device["path"] and runtime.get("Rate") == requested for runtime in runtime_rows)
            or not any(
                line.startswith(f"{device['major']}:{device['minor']} ") and f"{token}={requested}" in line.split()
                for line in io_text.splitlines()
            )
            for device in expected_devices
        ):
            raise OciOwnershipError("validation_resource_enforcement_unverified", f"container {control} limit does not match admitted policy")
        observations.append({"control": control, "status": "enforced", "observed": f"{token}={requested} on {len(expected_devices)} device(s)"})
    return observations


class OwnedContainer:
    """One create-before-start container with durable cleanup authority."""

    def __init__(self, runtime: PodmanRuntime, registry: IdentityRegistry, identity: dict):
        self.runtime = runtime
        self.registry = registry
        self.identity = validated_identity(identity)

    def create(self, *, mounts: list[tuple[Path, str, str]], tmpfs: list[tuple[str, int]], policy: dict, network: str = "bridge") -> dict:
        if self.identity["state"] != "planned":
            raise OciOwnershipError("validation_container_state_invalid", "container creation requires planned identity")
        policy = normalize_policy(policy)
        resource_argv, devices = resource_arguments(policy, self.runtime.workspace)
        lifecycle = self.identity["role"] == "lifecycle"
        expected_network = "none" if lifecycle else "bridge"
        if network != expected_network:
            raise OciOwnershipError(
                "validation_container_network_invalid",
                f"{self.identity['role']} container requires network mode {expected_network}",
            )
        arguments = [
            "podman", "create", "--pull", "never", "--cgroups", "split", "--name", self.identity["name"], "--network", network,
        ]
        if lifecycle:
            arguments += ["--systemd", "always"]
        else:
            arguments += ["--read-only", "--security-opt", "no-new-privileges", "--cap-drop", "all"]
        arguments += resource_argv
        for key, value in ownership_labels(self.identity).items():
            arguments += ["--label", f"{key}={value}"]
        for source, target, options in mounts:
            arguments += ["--volume", f"{Path(source).resolve()}:{target}:{options}"]
        for target, size in tmpfs:
            arguments += ["--tmpfs", f"{target}:rw,nosuid,nodev,noexec,size={size}"]
        arguments += [self.identity["image"]["id"], *( ["/sbin/init"] if lifecycle else ["sleep", "infinity"] )]
        self.identity["configuration"] = {
            "network": network,
            "mounts": [
                {"source": str(Path(source).resolve()), "destination": target, "read_only": "ro" in options.split(",")}
                for source, target, options in mounts
            ],
            "tmpfs": [{"destination": target, "size": size} for target, size in tmpfs],
            "policy": policy,
            "io_devices": devices,
            "create_argv_sha256": _configuration_digest(arguments),
        }
        self.registry.persist(self.identity)  # closes the pre-create crash window with exact intent
        result = self.runtime.run(arguments, timeout=60, workload=True)
        PodmanRuntime.require_success(result, "validation_container_create_failed", "Podman could not create the validation container")
        container_id = str(result.get("stdout") or "").strip().lower()
        if not CONTAINER_ID.fullmatch(container_id):
            raise OciOwnershipError("validation_container_create_failed", "Podman did not return an exact container ID")
        inspected = self.runtime.inspect_container(container_id)
        if inspected is None:
            raise OciOwnershipError("validation_container_create_failed", "created container disappeared before inspection")
        candidate = {**self.identity, "container_id": container_id}
        verify_owned_container(candidate, inspected)
        verify_container_configuration(candidate, inspected)
        self.identity.update({"container_id": container_id, "state": "created", "updated_at": utc_now()})
        self.registry.persist(self.identity)
        return result

    def start(self, *, policy: dict) -> tuple[dict, dict]:
        if self.identity["state"] != "created" or not self.identity["container_id"]:
            raise OciOwnershipError("validation_container_state_invalid", "container start requires durable created identity")
        before_start = self.runtime.inspect_container(self.identity["container_id"])
        if before_start is None:
            raise OciOwnershipError("validation_container_start_failed", "created container disappeared before start")
        verify_owned_container(self.identity, before_start)
        verify_container_configuration(self.identity, before_start)
        result = self.runtime.run([
            "systemd-run", "--scope", "--collect", "--quiet", "--unit", self.identity["scope_name"],
            *scope_resource_arguments(policy, self.runtime.workspace),
            "podman", "start", self.identity["container_id"],
        ], timeout=60, workload=True)
        PodmanRuntime.require_success(result, "validation_container_start_failed", "Podman could not start the validation container")
        inspected = self.runtime.inspect_container(self.identity["container_id"])
        if inspected is None:
            raise OciOwnershipError("validation_container_start_failed", "started container disappeared before inspection")
        facts = verify_owned_container(self.identity, inspected)
        verify_container_configuration(self.identity, inspected)
        if not facts["running"] or facts["pid"] <= 0:
            raise OciOwnershipError("validation_container_start_failed", "container payload is not running")
        if self.identity["role"] == "dependency-preparation":
            self.runtime.verify_payload_capabilities(facts["pid"])
        self.identity.update({"state": "running", "updated_at": utc_now()})
        self.registry.persist(self.identity)
        return facts, result

    def exec(
        self,
        arguments: list[str],
        *,
        timeout: float,
        accepted_exit_codes: set[int] | None = None,
        check: bool = True,
    ) -> dict:
        if self.identity["state"] != "running" or not self.identity["container_id"]:
            raise OciOwnershipError("validation_container_state_invalid", "container exec requires running identity")
        # The executed process joins the already verified container cgroup.
        # Re-probing host command containment for every short-lived Podman
        # client creates a race with the probe's transient unit and does not
        # strengthen payload enforcement. Cancellation still reaches the
        # client process group and owned-container cleanup remains authoritative.
        with suspended_identity_recording():
            result = self.runtime.run(["podman", "exec", self.identity["container_id"], *arguments], timeout=timeout, workload=True)
        accepted = {0} if accepted_exit_codes is None else set(accepted_exit_codes)
        result["accepted"] = result.get("exit_code") in accepted and not result.get("timed_out")
        if check and not result["accepted"]:
            reason = str(result.get("stderr") or result.get("stdout") or "validation container command failed")[:1000]
            raise OciOwnershipError("validation_container_command_failed", reason, details={"command": result})
        return result

    def cleanup(self, *, timeout: float = 30) -> None:
        """Cleanup ignores workload cancellation and clears identity only after exact absence."""
        with suspended_identity_recording():
            self._cleanup_uncontained(timeout=timeout)

    def _cleanup_uncontained(self, *, timeout: float) -> None:
        try:
            references = [value for value in (self.identity.get("container_id"), self.identity["name"]) if value]
            inspected = None
            for reference in references:
                inspected = self.runtime.inspect_container(reference)
                if inspected is not None:
                    break
            if inspected is not None:
                verify_owned_container(self.identity, inspected)
                remove_result = self.runtime.run(["podman", "rm", "--force", "--time", "3", _inspect_facts(inspected)["id"]], timeout=timeout, workload=False)
            else:
                remove_result = None
            for reference in references:
                if self.runtime.inspect_container(reference) is not None:
                    reason = "exact preparation-container absence was not proved"
                    if remove_result is not None and remove_result.get("status") != "success":
                        reason += "; Podman removal also failed"
                    raise OciOwnershipError("validation_container_cleanup_unproved", reason)
            self.registry.clear(self.identity)
        except BaseException as exc:
            self.identity.update({"state": "cleanup_failed", "updated_at": utc_now(), "cleanup_error": str(exc)[:1000]})
            self.registry.persist(self.identity)
            if isinstance(exc, OciOwnershipError):
                raise
            raise OciOwnershipError("validation_container_cleanup_failed", str(exc)) from exc


def recover_owned_containers(registry_root: str | Path, *, workspace: str | Path, runner=run_command) -> OciRecoveryResult:
    """Inventory the namespace, remove only authenticated orphans, and fail closed."""
    registry = IdentityRegistry(registry_root)
    runtime = PodmanRuntime(workspace, runner=runner)
    result = OciRecoveryResult()
    identities, registry_blockers = registry.load_all()
    result.blockers.extend(registry_blockers)
    try:
        candidates = runtime.namespace_candidates()
        inspected_by_id = {}
        for candidate in candidates:
            candidate_id = str(candidate.get("Id") or candidate.get("ID") or "").lower()
            inspected = runtime.inspect_container(candidate_id)
            if inspected is None:
                continue
            inspected_by_id[_inspect_facts(inspected)["id"]] = inspected
    except OciOwnershipError as exc:
        if not identities and not registry_blockers:
            # Podman is optional for Docker-only installations. With no durable
            # validation ownership state there is no destructive recovery duty.
            return result
        result.blockers.append({"code": exc.code, "reason": str(exc)[:1000]})
        return result
    result.inventory_trustworthy = True

    authenticated: set[str] = set()
    for _path, identity in identities:
        try:
            inspected = None
            if identity.get("container_id"):
                inspected = runtime.inspect_container(identity["container_id"])
            if inspected is None:
                inspected = runtime.inspect_container(identity["name"])
            if inspected is None:
                registry.clear(identity)
                result.cleared_absent.append(identity["attempt_id"])
                result.resolved_identities.append(dict(identity))
                continue
            facts = verify_owned_container(identity, inspected)
            verify_container_configuration(identity, inspected)
            authenticated.add(facts["id"])
            owned = OwnedContainer(runtime, registry, identity)
            owned.cleanup()
            result.recovered.append(identity["attempt_id"])
            result.resolved_identities.append(dict(identity))
        except OciOwnershipError as exc:
            result.blockers.append({"code": exc.code, "reason": str(exc)[:1000], "attempt_id": identity["attempt_id"]})

    for container_id, inspected in inspected_by_id.items():
        if container_id in authenticated:
            continue
        facts = _inspect_facts(inspected)
        result.blockers.append({
            "code": "validation_container_ownership_unverifiable",
            "reason": "namespace-looking container has no matching trustworthy durable identity",
            "container_id": facts["id"][:64], "container_name": facts["name"][:128],
        })
    return result
