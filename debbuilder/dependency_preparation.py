"""Networked, non-installing APT dependency preparation for validation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import storage
from .apt_repo import debian_version_relation
from .apt_repository_trust import apt_configuration, deb822_source, inspect_public_key
from .build_models import utc_now
from .build_store import BuildStore
from .command_identity import clear_identity, persist_identity, recording_identities, update_identity
from .command_runner import run_command
from .execution_cancellation import ExecutionCancelled
from .runtime_apt_repositories import normalize_runtime_apt_repositories
from .recipe_schema import require_safe_name
from .validation_contracts import (
    PREPARED_DEPENDENCIES_CONTRACT_VERSION,
    VALIDATION_RESULT_CONTRACT_VERSION,
    normalize_prepared_runtime_dependencies,
    normalize_validation_attempt,
    normalize_validation_result,
)
from .validation_automation import normalize_validation_automation
from .validation_oci import (
    IdentityRegistry,
    OciOwnershipError,
    OwnedContainer,
    PodmanRuntime,
    new_identity,
    verify_resource_enforcement,
)
from .validation_profiles import resolve_profile
from .validation_images import provision_admitted_image


METADATA_TMPFS_BYTES = 256 * 1024 * 1024
ARCHIVE_TMPFS_BYTES = 384 * 1024 * 1024
LOG_TMPFS_BYTES = 4 * 1024 * 1024
STATE_TMPFS_BYTES = 32 * 1024 * 1024
MAX_PACKAGES = 512
MAX_DECLARED_PACKAGE_BYTES = 384 * 1024 * 1024
MAX_ACTUAL_PACKAGE_BYTES = 384 * 1024 * 1024
MAX_INDIVIDUAL_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_SOLVER_OUTPUT_BYTES = 1024 * 1024
MAX_RECOVERY_ATTEMPTS = 512
MAX_RECOVERY_RUN_ENTRIES = 4096
SAFE_ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.+_~:-]|%[0-9A-Fa-f]{2}){0,254}\.deb$")
INST = re.compile(r"^Inst\s+(\S+)(?:\s+\[([^\]]+)\])?\s+\((\S+)(?:\s+(.*?))?\s+\[([^\]]+)\]\)\s*$")
PRINT_URI = re.compile(r"^'([^']+)'\s+(?:'([^']+)'|(\S+))\s+(\d+)(?:\s+\S+)?\s*$")
MARK_KEEP = re.compile(r"^\s+MarkInstall ([a-z0-9][a-z0-9+.-]{0,127}):([a-z0-9-]{1,32}) < (\S+) @ii .* > FU=0\s*$")


class DependencyPreparationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class ArtifactMetadata:
    path: Path
    package: str
    version: str
    architecture: str
    depends: str
    pre_depends: str
    size: int
    sha256: str

    def identity(self) -> dict:
        return {
            "package": self.package, "version": self.version, "architecture": self.architecture,
            "size": self.size, "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AptDecision:
    package: str
    previous_version: str | None
    version: str
    architecture: str
    description: str


class PreparationSupervisor:
    """In-process shutdown registry; it is deliberately not an execution manager."""

    def __init__(self):
        self._condition = threading.Condition()
        self._accepting = True
        self._active: dict[str, threading.Event] = {}
        self._blocker = ""

    def open_admission(self) -> None:
        with self._condition:
            self._blocker = ""
            self._accepting = True

    def block(self, reason: str) -> None:
        with self._condition:
            self._blocker = str(reason or "validation container cleanup is unresolved")[:500]
            self._accepting = False

    @property
    def blocker(self) -> str:
        with self._condition:
            return self._blocker

    def register(self, attempt_id: str, event: threading.Event) -> None:
        with self._condition:
            if not self._accepting:
                code = "validation_container_recovery_required" if self._blocker else "validation_preparation_shutting_down"
                raise DependencyPreparationError(code, "Dependency preparation admission is closed")
            if self._active:
                raise DependencyPreparationError("validation_attempt_conflict", "Another validation preparation is already active")
            self._active[attempt_id] = event

    def unregister(self, attempt_id: str) -> None:
        with self._condition:
            self._active.pop(attempt_id, None)
            self._condition.notify_all()

    def shutdown(self, timeout: float | None) -> bool:
        import time

        with self._condition:
            self._accepting = False
            for event in self._active.values():
                event.set()
            if timeout is None:
                while self._active:
                    self._condition.wait()
                return True
            deadline = time.monotonic() + max(0.0, timeout)
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


SUPERVISOR = PreparationSupervisor()


def _load_recovery_json(path: Path, limit: int) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Durable validation record is unsafe")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Durable validation record changed during inventory")
        raw = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    return json.loads(raw.decode("utf-8"))


def _inventory_attempt_records(store: BuildStore) -> list[tuple[str, str, Path]]:
    rows: list[tuple[str, str, Path]] = []
    if not store.root.exists():
        return rows
    with os.scandir(store.root) as run_entries:
        for run_index, run_entry in enumerate(run_entries, start=1):
            if run_index > MAX_RECOVERY_RUN_ENTRIES:
                raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Build Run recovery inventory exceeds its count bound")
            if not run_entry.is_dir(follow_symlinks=False):
                continue
            require_safe_name(run_entry.name, "build run id")
            attempts = Path(run_entry.path) / "manifests" / "validation-attempts"
            if attempts.is_symlink():
                raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Validation attempt inventory is unsafe")
            if not attempts.exists():
                continue
            if not stat.S_ISDIR(attempts.lstat().st_mode):
                raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Validation attempt inventory is unsafe")
            with os.scandir(attempts) as attempt_entries:
                for attempt_entry in attempt_entries:
                    if not attempt_entry.is_dir(follow_symlinks=False):
                        raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Validation attempt entry is unsafe")
                    require_safe_name(attempt_entry.name, "validation attempt id")
                    rows.append((run_entry.name, attempt_entry.name, Path(attempt_entry.path)))
                    if len(rows) > MAX_RECOVERY_ATTEMPTS:
                        raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "Validation attempt inventory exceeds its count bound")
    return sorted(rows)


def recover_interrupted_attempts(store: BuildStore, identities: list[dict], *, inventory_trustworthy: bool = True) -> dict:
    """Inventory every durable attempt and terminalize only after OCI absence is proved."""
    recovered: list[str] = []
    blockers: list[dict] = []
    resolved = {(str(row.get("run_id") or ""), str(row.get("attempt_id") or "")) for row in identities}
    seen: set[tuple[str, str]] = set()
    try:
        records = _inventory_attempt_records(store)
    except (OSError, ValueError, DependencyPreparationError) as exc:
        return {"recovered": recovered, "blockers": [{
            "code": "validation_attempt_recovery_unverifiable", "reason": str(exc)[:1000],
            "run_id": "", "attempt_id": "",
        }]}
    for run_id, attempt_id, root in records:
        try:
            seen.add((run_id, attempt_id))
            with store.locked_run(run_id):
                from . import validation_service

                attempt_path = root / "attempt.json"
                attempt = validation_service.load_attempt(store, run_id, attempt_id)
                if attempt["status"] not in {"running", "cancelling"}:
                    continue
                if not inventory_trustworthy and (run_id, attempt_id) not in resolved:
                    raise DependencyPreparationError("validation_attempt_recovery_unverifiable", "OCI absence was not proved for a nonterminal validation attempt")
                prepared_path = root / "prepared.json"
                prepared = None
                if prepared_path.exists() or prepared_path.is_symlink():
                    prepared = normalize_prepared_runtime_dependencies(_load_recovery_json(prepared_path, 1024 * 1024))
                automation_path = root / "automation.json"
                try:
                    normalize_validation_automation(_load_recovery_json(automation_path, 16 * 1024))
                except (FileNotFoundError, ValueError) as exc:
                    raise DependencyPreparationError(
                        "validation_attempt_recovery_unverifiable",
                        "Validation automation metadata is missing or invalid",
                    ) from exc
                if attempt["status"] == "cancelling":
                    attempt = _terminal_attempt(
                        attempt, status="cancelled",
                        code="validation_recovery_cancelled",
                        message="Validation cancellation completed during startup recovery",
                    )
                elif prepared is not None:
                    attempt = _terminal_attempt(
                        attempt, status="failed",
                        code="validation_lifecycle_interrupted",
                        message="Offline lifecycle validation was interrupted after dependency preparation",
                    )
                else:
                    attempt = _terminal_attempt(
                        attempt, status="failed", code="validation_preparation_interrupted",
                        message="Dependency preparation was interrupted before durable completion",
                    )
                _persist(attempt_path, attempt)
                recovered.append(attempt_id)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, json.JSONDecodeError, DependencyPreparationError) as exc:
            blockers.append({
                "code": "validation_attempt_recovery_unverifiable",
                "reason": str(exc)[:1000],
                "run_id": run_id[:128],
                "attempt_id": attempt_id[:128],
            })
    for run_id, attempt_id in sorted(resolved - seen):
        blockers.append({
            "code": "validation_attempt_recovery_unverifiable",
            "reason": "Recovered OCI identity has no matching durable validation attempt",
            "run_id": run_id[:128], "attempt_id": attempt_id[:128],
        })
    return {"recovered": recovered, "blockers": blockers}


def _bounded_output(result: dict) -> str:
    output = str(result.get("stdout") or "")
    if len(output.encode("utf-8")) > MAX_SOLVER_OUTPUT_BYTES:
        raise DependencyPreparationError("apt_output_too_large", "APT solver output exceeded its byte bound")
    return output


def _bounded_combined_output(result: dict) -> str:
    output = str(result.get("stdout") or "") + "\n" + str(result.get("stderr") or "")
    if len(output.encode("utf-8")) > MAX_SOLVER_OUTPUT_BYTES:
        raise DependencyPreparationError("apt_output_too_large", "APT solver output exceeded its byte bound")
    return output


def _run_host(arguments: list[str], *, workspace: Path, runner, cancellation_event=None, timeout=30) -> dict:
    result = runner(
        " ".join(shlex.quote(str(value)) for value in arguments), workspace=workspace,
        working_directory=".", environment={"LC_ALL": "C"}, timeout=timeout,
        inactivity_timeout=timeout, cancellation_event=cancellation_event,
    )
    if result.get("status") != "success":
        raise DependencyPreparationError("artifact_metadata_invalid", str(result.get("stderr") or "Debian artifact inspection failed")[:1000])
    return result


def _control_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line[1:]
        elif ":" in line:
            key, value = line.split(":", 1)
            if key in fields:
                raise DependencyPreparationError("artifact_metadata_invalid", f"Artifact repeats control field {key}")
            fields[key], current = value.lstrip(), key
    return fields


def inspect_artifact(path: str | Path, *, workspace: Path, runner=run_command, cancellation_event=None) -> ArtifactMetadata:
    raw_path = Path(path).absolute()
    raw_info = raw_path.lstat()
    if stat.S_ISLNK(raw_info.st_mode):
        raise DependencyPreparationError("artifact_metadata_invalid", "Artifact path must not be a symbolic link")
    candidate = raw_path.resolve(strict=True)
    descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size <= 0:
            raise DependencyPreparationError("artifact_metadata_invalid", "Artifact must be one non-empty regular file without hard links")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise DependencyPreparationError("artifact_changed_during_inspection", "Artifact changed during immutable identity capture")
    finally:
        os.close(descriptor)
    result = _run_host(["dpkg-deb", "--field", str(candidate)], workspace=workspace, runner=runner, cancellation_event=cancellation_event)
    fields = _control_fields(_bounded_output(result))
    required = {key: fields.get(key, "") for key in ("Package", "Version", "Architecture")}
    if any(not value for value in required.values()):
        raise DependencyPreparationError("artifact_metadata_invalid", "Artifact lacks package, version, or architecture metadata")
    return ArtifactMetadata(
        candidate, required["Package"], required["Version"], required["Architecture"],
        fields.get("Depends", ""), fields.get("Pre-Depends", ""), before.st_size, digest.hexdigest(),
    )


def _snapshot_artifact(source: ArtifactMetadata, destination: Path) -> ArtifactMetadata:
    """Pin an inspected artifact into attempt-private storage before OCI mounting."""
    source_fd = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != source.size:
            raise DependencyPreparationError("artifact_changed_during_snapshot", "Artifact identity changed before private snapshot")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o400,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_fd, remaining)
                if written <= 0:
                    raise OSError("artifact snapshot write made no progress")
                remaining = remaining[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or copied != source.size
            or digest.hexdigest() != source.sha256
        ):
            raise DependencyPreparationError("artifact_changed_during_snapshot", "Artifact changed while creating its private snapshot")
    except BaseException:
        if destination_fd >= 0:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
    return ArtifactMetadata(
        destination, source.package, source.version, source.architecture,
        source.depends, source.pre_depends, source.size, source.sha256,
    )


def parse_simulation(output: str, *, target: ArtifactMetadata) -> tuple[list[AptDecision], bool]:
    decisions: list[AptDecision] = []
    removal = False
    for line in output.splitlines():
        if line.startswith("Remv "):
            removal = True
            continue
        if not line.startswith("Inst "):
            continue
        match = INST.fullmatch(line)
        if not match:
            raise DependencyPreparationError("apt_solver_output_invalid", "APT emitted an unrecognized installation decision")
        package, previous, version, description, architecture = match.groups()
        decisions.append(AptDecision(package, previous, version, architecture, description or ""))
    if removal:
        raise DependencyPreparationError("apt_removal_required", "APT dependency solution requires package removal")
    target_rows = [row for row in decisions if row.package.split(":", 1)[0] == target.package and row.version == target.version]
    if len(target_rows) != 1:
        raise DependencyPreparationError("apt_target_decision_missing", "APT did not select the exact target artifact")
    if len(decisions) > MAX_PACKAGES + 1:
        raise DependencyPreparationError("apt_package_count_exceeded", "APT solution exceeds the package-count bound")
    return decisions, removal


def parse_print_uris(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        if not line.startswith("'"):
            continue
        match = PRINT_URI.fullmatch(line)
        if not match:
            raise DependencyPreparationError("apt_download_plan_invalid", f"APT emitted an unrecognized download plan: {line[:500]}")
        uri, quoted_filename, bare_filename, raw_size = match.groups()
        filename = quoted_filename or bare_filename
        size = int(raw_size)
        if size <= 0 or size > MAX_INDIVIDUAL_PACKAGE_BYTES:
            raise DependencyPreparationError("apt_package_size_exceeded", "APT package exceeds the individual size bound")
        rows.append({"uri": uri, "filename": filename, "size": size})
    if len(rows) > MAX_PACKAGES or sum(row["size"] for row in rows) > MAX_DECLARED_PACKAGE_BYTES:
        raise DependencyPreparationError("apt_download_size_exceeded", "APT declared download set exceeds its hard bound")
    return rows


def _apt_exec(container: OwnedContainer, arguments: list[str], *, timeout: float) -> dict:
    return container.exec([
        "env", "LC_ALL=C", "APT_CONFIG=/debbuilder-input/apt.conf", "python3",
        "/debbuilder-input/bounded_process.py", "--limit", str(MAX_SOLVER_OUTPUT_BYTES), "--", *arguments,
    ], timeout=timeout)


def _solve(container: OwnedContainer, artifact_name: str, artifact: ArtifactMetadata, *, status_path: str, timeout: float) -> tuple[list[AptDecision], list[dict]]:
    result = _apt_exec(container, [
        "apt-get", "-q=2", "--simulate", "--no-install-recommends", "--no-remove", "--assume-yes",
        "-o", "Debug::pkgDepCache::Marker=1",
        "-o", f"Dir::State::status={status_path}", "install", f"/debbuilder-input/{artifact_name}",
    ], timeout=timeout)
    output = _bounded_combined_output(result)
    decisions, _ = parse_simulation(output, target=artifact)
    satisfied = []
    for line in output.splitlines():
        match = MARK_KEEP.fullmatch(line)
        if match:
            package, architecture, version = match.groups()
            satisfied.append({"package": package, "version": version, "architecture": architecture})
    if len(satisfied) > MAX_PACKAGES:
        raise DependencyPreparationError("base_package_inventory_invalid", "APT selected too many already-installed packages")
    return decisions, satisfied


def _download(container: OwnedContainer, artifact_name: str, *, status_path: str, timeout: float) -> list[dict]:
    planned = _apt_exec(container, [
        "apt-get", "-q=2", "--print-uris", "--download-only", "--no-install-recommends", "--no-remove", "--assume-yes",
        "-o", f"Dir::State::status={status_path}", "install", f"/debbuilder-input/{artifact_name}",
    ], timeout=timeout)
    plan = [row for row in parse_print_uris(_bounded_output(planned)) if not row["uri"].startswith("file:")]
    actual = _apt_exec(container, [
        "apt-get", "-q=2", "--download-only", "--no-install-recommends", "--no-remove", "--assume-yes",
        "-o", f"Dir::State::status={status_path}", "install", f"/debbuilder-input/{artifact_name}",
    ], timeout=timeout)
    _bounded_output(actual)
    return plan


def _model_previous(container: OwnedContainer, decisions: list[AptDecision], previous: ArtifactMetadata) -> None:
    dependencies = [row for row in decisions if not (row.package.split(":", 1)[0] == previous.package and row.version == previous.version)]
    arguments = [
        "python3", "/debbuilder-input/apt_state_model.py", "--base-status", "/var/lib/dpkg/status",
        "--output", "/prep/status.previous", "--artifact", "/debbuilder-input/previous.deb",
    ]
    for row in dependencies:
        arguments += ["--decision", f"{row.package}={row.version}"]
    _apt_exec(container, arguments, timeout=60)


def _installed_base_packages(container: OwnedContainer, *, native_architecture: str) -> list[dict]:
    """Capture the complete immutable base dpkg state that APT solved against."""
    result = _apt_exec(container, [
        "dpkg-query", "--show",
        "--showformat=${binary:Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\n",
    ], timeout=30)
    rows: list[dict] = []
    for line in _bounded_output(result).splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise DependencyPreparationError("base_package_inventory_invalid", "dpkg-query emitted malformed base package state")
        package, version, architecture, status_name = fields
        if status_name != "installed":
            continue
        if package.endswith(f":{architecture}"):
            package = package[:-(len(architecture) + 1)]
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", package)
            or not version or len(version) > 256
            or architecture not in {"all", native_architecture}
        ):
            raise DependencyPreparationError("base_package_inventory_invalid", "dpkg-query emitted unsafe base package identity")
        rows.append({"package": package, "version": version, "architecture": architecture})
    if len(rows) > MAX_PACKAGES or len({(row["package"], row["architecture"]) for row in rows}) != len(rows):
        raise DependencyPreparationError("base_package_inventory_invalid", "Base package inventory is duplicate or exceeds its count bound")
    return sorted(rows, key=lambda row: (row["package"], row["architecture"]))


def _list_archives(container: OwnedContainer) -> list[dict]:
    script = (
        "import json,pathlib; p=pathlib.Path('/prep/archives'); out=[]; "
        "entries=list(p.iterdir()); "
        "assert all((x.name=='lock' and x.is_file() and not x.is_symlink()) or "
        "(x.name=='partial' and x.is_dir() and not x.is_symlink() and not list(x.iterdir())) or "
        "(x.is_file() and not x.is_symlink() and x.name.endswith('.deb')) for x in entries); "
        "out=sorted((x.name,x.stat().st_size) for x in entries if x.name.endswith('.deb')); print(json.dumps(out))"
    )
    result = container.exec(["python3", "-c", script], timeout=30)
    try:
        rows = json.loads(_bounded_output(result))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DependencyPreparationError("apt_archive_inventory_invalid", "Downloaded archive inventory is malformed") from exc
    if not isinstance(rows, list) or len(rows) > MAX_PACKAGES or any(
        not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
        or len(row[0]) > 259 or not SAFE_ARCHIVE_NAME.fullmatch(row[0])
        or isinstance(row[1], bool) or not isinstance(row[1], int)
        or row[1] <= 0 or row[1] > MAX_INDIVIDUAL_PACKAGE_BYTES
        for row in rows
    ):
        raise DependencyPreparationError("apt_archive_inventory_invalid", "Downloaded archive inventory is unsafe or oversized")
    if sum(row[1] for row in rows) > MAX_ACTUAL_PACKAGE_BYTES:
        raise DependencyPreparationError("apt_download_size_exceeded", "Downloaded packages exceed the actual-byte bound")
    return [{"name": row[0], "size": row[1]} for row in rows]


def _origin(container: OwnedContainer, decision: AptDecision, repositories: list[dict], *, download_uri: str) -> dict | None:
    eligible = [
        repository for repository in repositories
        if download_uri == repository["uri"].rstrip("/") or download_uri.startswith(repository["uri"].rstrip("/") + "/")
    ]
    if not eligible:
        return None
    result = _apt_exec(container, ["apt-cache", "policy", decision.package], timeout=30)
    lines = _bounded_output(result).splitlines()
    marker = re.compile(rf"^\s+{re.escape(decision.version)}\s")
    matches = []
    for index, line in enumerate(lines):
        if not marker.match(line):
            continue
        for detail in lines[index + 1:index + 5]:
            match = re.match(r"^\s+\d+\s+(https://\S+)\s+(\S+)/(\S+)\s+", detail)
            if not match:
                continue
            uri, suite, component = match.groups()
            for repository in eligible:
                repository_uri = repository["uri"].rstrip("/")
                if (uri == repository_uri or uri.startswith(repository_uri + "/")) and suite == repository["suite"] and component in repository["components"]:
                    matches.append({"repository_id": repository["id"], "suite": suite, "component": component})
    unique = {(row["repository_id"], row["suite"], row["component"]): row for row in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _verify_downloads(
    container: OwnedContainer,
    output: Path,
    expected: list[tuple[str, AptDecision]],
    repositories: list[dict],
    plans: list[dict],
    *,
    native_architecture: str,
    workspace: Path,
    runner,
    cancellation_event,
) -> list[dict]:
    inventory = _list_archives(container)
    output.mkdir(mode=0o700)
    copied: list[ArtifactMetadata] = []
    total = 0
    for archive in inventory:
        name = archive["name"]
        target = output / name
        result = container.runtime.run([
            "podman", "cp", f"{container.identity['container_id']}:/prep/archives/{name}", str(target),
        ], timeout=60, workload=True)
        PodmanRuntime.require_success(result, "apt_archive_copy_failed", "Downloaded dependency could not be copied from bounded storage")
        metadata = inspect_artifact(target, workspace=workspace, runner=runner, cancellation_event=cancellation_event)
        if metadata.size != archive["size"]:
            raise DependencyPreparationError("apt_archive_changed", "Downloaded package changed while leaving bounded storage")
        if metadata.architecture not in {"all", native_architecture}:
            raise DependencyPreparationError("apt_archive_architecture_mismatch", "Downloaded package has a foreign architecture")
        if metadata.size > MAX_INDIVIDUAL_PACKAGE_BYTES:
            raise DependencyPreparationError("apt_package_size_exceeded", "Downloaded package exceeds the individual size bound")
        total += metadata.size
        if total > MAX_ACTUAL_PACKAGE_BYTES:
            raise DependencyPreparationError("apt_download_size_exceeded", "Downloaded packages exceed the actual-byte bound")
        copied.append(metadata)
    expected_by_key = {}
    for role, row in expected:
        key = (row.package.split(":", 1)[0], row.version)
        expected_by_key.setdefault(key, []).append((role, row))
    copied_by_key = {(row.package, row.version): row for row in copied}
    if set(copied_by_key) != set(expected_by_key) or len(copied_by_key) != len(copied):
        raise DependencyPreparationError("apt_archive_set_mismatch", "Downloaded package files do not exactly match APT decisions")
    durable = []
    for key in sorted(expected_by_key):
        metadata = copied_by_key[key]
        for role, decision in expected_by_key[key]:
            matching_plans = [
                row for row in plans
                if row["role"] == role and row["filename"] == metadata.path.name and row["size"] == metadata.size
            ]
            if len({(row["uri"], row["filename"], row["size"]) for row in matching_plans}) != 1:
                raise DependencyPreparationError("apt_download_plan_mismatch", "Downloaded package cannot be bound to one authenticated APT download URI")
            durable.append({
                "package": metadata.package, "version": metadata.version, "architecture": metadata.architecture,
                "role": role, "size": metadata.size, "sha256": metadata.sha256,
                "origin": _origin(container, decision, repositories, download_uri=matching_plans[0]["uri"]),
            })
    return durable


def _persist(path: Path, value: dict) -> None:
    storage.save_json(path, value)
    path.chmod(0o600)


def _terminal_attempt(attempt: dict, *, status: str, code: str = "", message: str = "") -> dict:
    finished = utc_now()
    attempt.update({
        "status": status, "finished_at": finished,
        "result": None,
        "error": None if status == "success" else {"code": code, "message": message},
    })
    return normalize_validation_attempt(attempt)


def begin_lifecycle_attempt(store: BuildStore, run_id: str, attempt_id: str, prepared: dict) -> dict:
    """Confirm a preparation-complete attempt is active before OCI creation."""
    path = store.run_dir(run_id) / "manifests" / "validation-attempts" / attempt_id / "attempt.json"
    with store.locked_run(run_id):
        with storage.locked_path(path):
            attempt = normalize_validation_attempt(_load_recovery_json(path, 256 * 1024))
            normalized = normalize_prepared_runtime_dependencies(prepared)
            persisted = normalize_prepared_runtime_dependencies(
                _load_recovery_json(path.with_name("prepared.json"), 1024 * 1024),
            )
            if (
                attempt["id"] != attempt_id or attempt["build_run_id"] != run_id
                or attempt["status"] not in {"running", "cancelling"}
                or persisted != normalized
                or normalized["profile_name"] != attempt["inputs"]["profile"]
                or normalized["artifacts"] != {
                    "current": attempt["inputs"]["artifact"],
                    "previous": attempt["inputs"]["previous_artifact"],
                }
                or normalized["image"] != attempt["selected_profile"]["image"]
            ):
                raise DependencyPreparationError(
                    "validation_attempt_state_invalid",
                    "Prepared validation attempt cannot enter lifecycle execution",
                )
            return attempt


def _canonical_lifecycle_result(attempt: dict, prepared: dict, validation: dict) -> dict:
    status = str(validation.get("status") or "failed")
    if status not in {"success", "failed", "cancelled"}:
        status = "failed"
    raw_error = validation.get("error") if isinstance(validation.get("error"), dict) else {}
    code = str(raw_error.get("code") or (
        "validation_lifecycle_cancelled" if status == "cancelled" else "validation_lifecycle_failed"
    ))
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", code):
        code = "validation_lifecycle_failed"
    checks = []
    for row in validation.get("checks") or []:
        if not isinstance(row, dict):
            continue
        checks.append({
            "name": str(row.get("name") or "validation_check")[:256],
            "status": "success" if row.get("status") == "success" else "failed",
            "error": "Validation check failed" if row.get("error") else "",
        })
    if not checks:
        checks = [{"name": "lifecycle_execution", "status": "failed", "error": "Validation lifecycle did not produce checks"}]
        if status == "success":
            status = "failed"
            code = "validation_lifecycle_evidence_missing"
    backend = validation.get("backend") if isinstance(validation.get("backend"), dict) else {}
    stop = backend.get("stop") if isinstance(backend.get("stop"), dict) else {}
    commands = []
    for row in validation.get("commands") or []:
        if not isinstance(row, dict):
            continue
        commands.append({
            "command": str(row.get("command") or "")[:1000],
            "arguments": [str(item)[:1000] for item in (row.get("arguments") or [])[:128]],
            "status": str(row.get("status") or ("success" if row.get("accepted") else "failed")),
            "exit_code": row.get("exit_code") if isinstance(row.get("exit_code"), int) and not isinstance(row.get("exit_code"), bool) else None,
            "accepted": row.get("accepted") if isinstance(row.get("accepted"), bool) else None,
            "stdout": str(row.get("stdout") or "")[:4096],
            "stderr": str(row.get("stderr") or "")[:4096],
        })
        if len(commands) >= 100:
            break
    details = raw_error.get("details") if isinstance(raw_error.get("details"), dict) else {}
    failed_checks = details.get("failed_checks") if isinstance(details.get("failed_checks"), list) else [
        row["name"] for row in checks if row["status"] == "failed"
    ]
    error = None if status == "success" else {
        "code": code,
        "message": (
            "Offline lifecycle validation was cancelled"
            if status == "cancelled"
            else "Offline lifecycle validation failed"
        ),
        "failed_checks": [str(item)[:128] for item in failed_checks[:50]],
        "cleanup_code": str(details.get("cleanup_code") or "")[:128],
    }
    return normalize_validation_result({
        "contract_version": VALIDATION_RESULT_CONTRACT_VERSION,
        "attempt_id": attempt["id"],
        "build_run_id": attempt["build_run_id"],
        "artifact": attempt["inputs"]["artifact"],
        "profile": attempt["selected_profile"],
        "status": status,
        "started_at": validation.get("started_at") or prepared["finished_at"],
        "finished_at": validation.get("finished_at") or utc_now(),
        "checks": checks,
        "execution": {
            "network": "disabled" if backend.get("network") == "disabled" else "unverified",
            "network_verified": backend.get("network_verified") is True,
            "cleanup": {
                "status": "success" if stop.get("status") == "success" else "unresolved",
                "absence_proved": stop.get("absence_proved") is True,
            },
        },
        "commands": commands,
        "error": error,
    })


def complete_lifecycle_attempt(store: BuildStore, run_id: str, attempt_id: str, validation: dict) -> dict:
    """Persist result.json after cleanup proof, then terminalize attempt.json."""
    root = store.run_dir(run_id) / "manifests" / "validation-attempts" / attempt_id
    path = root / "attempt.json"
    result_path = root / "result.json"
    with store.locked_run(run_id):
        with storage.locked_path(path):
            attempt = normalize_validation_attempt(_load_recovery_json(path, 256 * 1024))
            if attempt["id"] != attempt_id or attempt["build_run_id"] != run_id or attempt["status"] not in {"running", "cancelling"}:
                raise DependencyPreparationError("validation_attempt_state_invalid", "Lifecycle attempt state is inconsistent")
            prepared = normalize_prepared_runtime_dependencies(
                _load_recovery_json(root / "prepared.json", 1024 * 1024),
            )
            raw_error = validation.get("error") if isinstance(validation.get("error"), dict) else {}
            code = str(raw_error.get("code") or "validation_lifecycle_failed")
            cleanup_unresolved = code in {
                "validation_container_cleanup_unproved", "validation_container_cleanup_failed",
                "validation_container_cleanup_unresolved",
            }
            if cleanup_unresolved:
                attempt.update({"status": "cancelling", "finished_at": None, "result": None, "error": None})
                SUPERVISOR.block("validation lifecycle cleanup is unresolved")
                _persist(path, normalize_validation_attempt(attempt))
                return attempt
            result = _canonical_lifecycle_result(attempt, prepared, validation)
            if attempt["status"] == "cancelling" and result["status"] != "cancelled":
                result = dict(result)
                result.update({
                    "status": "cancelled",
                    "error": {
                        "code": "validation_lifecycle_cancelled",
                        "message": "Offline lifecycle validation was cancelled",
                        "failed_checks": [row["name"] for row in result["checks"] if row["status"] == "failed"][:50],
                        "cleanup_code": "",
                    },
                })
                result = normalize_validation_result(result)
            _persist(result_path, result)
            terminal_status = result["status"]
            attempt.update({
                "status": terminal_status,
                "finished_at": result["finished_at"],
                "result": {"status": terminal_status, "reference": "result.json"},
                "error": None if terminal_status == "success" else {
                    "code": result["error"]["code"],
                    "message": result["error"]["message"],
                },
            })
            attempt = normalize_validation_attempt(attempt)
            _persist(path, attempt)
            return attempt


def prepare_runtime_dependencies(
    run_id: str,
    attempt_id: str,
    *,
    store: BuildStore,
    current_artifact: str | Path,
    previous_artifact: str | Path | None = None,
    profile_name: str = "bookworm",
    repositories: list[dict] | None = None,
    registry_root: str | Path,
    runner=run_command,
    cancellation_event: threading.Event | None = None,
    test_ca_certificate: str | Path | None = None,
    update_timeout: float = 180,
    solve_timeout: float = 120,
) -> dict:
    """Prepare an exact verified package bundle; never install or unpack it."""
    require_safe_name(run_id, "build run id")
    require_safe_name(attempt_id, "validation attempt id")
    event = cancellation_event or threading.Event()
    SUPERVISOR.register(attempt_id, event)
    attempt_root = store.run_dir(run_id) / "manifests" / "validation-attempts" / attempt_id
    attempt_path = attempt_root / "attempt.json"
    container: OwnedContainer | None = None
    attempt: dict | None = None
    main_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        existing_identities, identity_blockers = IdentityRegistry(registry_root).load_all()
        if existing_identities or identity_blockers:
            raise DependencyPreparationError(
                "validation_container_recovery_required",
                "Durable validation container state must be recovered before new preparation",
            )
        with store.locked_run(run_id) as workspace_fd:
            run = store.load(run_id)
            if run is None:
                raise FileNotFoundError(f"Build Run {run_id} was not found")
            workspace = store.run_dir(run_id).resolve()
            attempt_root.resolve(strict=False).relative_to(workspace)
            with storage.locked_path(attempt_path):
                attempt = normalize_validation_attempt(_load_recovery_json(attempt_path, 256 * 1024))
                if (
                    attempt["id"] != attempt_id or attempt["build_run_id"] != run_id
                    or attempt["status"] != "queued"
                ):
                    raise DependencyPreparationError(
                        "validation_attempt_state_invalid", "Admitted validation attempt is not queued",
                    )
                # Claim the durable attempt before any potentially slow
                # image/artifact inspection. Cancellation can then CAS it
                # to cancelling without waiting for the Run lease.
                attempt.update({"status": "running", "started_at": utc_now()})
                attempt = normalize_validation_attempt(attempt)
                _persist(attempt_path, attempt)
            with recording_identities(
                run_id,
                record=lambda identity: persist_identity(workspace_fd, identity),
                clear=lambda identity: clear_identity(workspace_fd, identity),
                update=lambda expected, updated: update_identity(workspace_fd, expected, updated),
                resource_policy=run["resource_limits"]["effective"],
            ):
                runtime = PodmanRuntime(workspace, runner=runner, cancellation_event=event)
                resolve_profile(profile_name)
                image = attempt["selected_profile"]["image"]
                if image["id"] is None:
                    resolved = provision_admitted_image(runtime, profile_name, image)
                    with storage.locked_path(attempt_path):
                        latest = normalize_validation_attempt(_load_recovery_json(attempt_path, 256 * 1024))
                        if event.is_set() or latest["status"] == "cancelling":
                            raise ExecutionCancelled()
                        if latest["status"] != "running" or latest["selected_profile"]["image"] != image:
                            raise DependencyPreparationError("validation_admission_identity_changed", "Admitted Validation image changed during provisioning")
                        latest["selected_profile"]["image"] = resolved
                        attempt = normalize_validation_attempt(latest)
                        _persist(attempt_path, attempt)
                        image = resolved
                observed_image = runtime.inspect_image(image["id"])
                if observed_image["id"] != image["id"]:
                    raise DependencyPreparationError("validation_admission_identity_changed", "Admitted Validation image is no longer available by immutable ID")
                current = inspect_artifact(current_artifact, workspace=workspace, runner=runner, cancellation_event=event)
                previous = inspect_artifact(previous_artifact, workspace=workspace, runner=runner, cancellation_event=event) if previous_artifact else None
                if previous is not None:
                    if previous.package != current.package or previous.architecture != current.architecture:
                        raise DependencyPreparationError("previous_artifact_incompatible", "Previous and current artifacts must have the same package and architecture")
                    relation = debian_version_relation(current.version, previous.version, workspace=workspace, runner=runner)
                    if relation["relation"] != "newer":
                        raise DependencyPreparationError("previous_artifact_version_invalid", "Previous artifact version must be strictly older than current")
                started = utc_now()
                observed_inputs = {
                    "profile": profile_name,
                    "artifact": current.identity(),
                    "previous_artifact": previous.identity() if previous else None,
                }
                if attempt["inputs"] != observed_inputs or attempt["selected_profile"] != {"name": profile_name, "image": image}:
                    raise DependencyPreparationError(
                        "validation_admission_identity_changed",
                        "Validation inputs changed after durable admission",
                    )
                # Cancellation owns this manifest independently from the
                # Run lease. Re-read it under its path lock instead of
                # overwriting a concurrently persisted cancelling state.
                with storage.locked_path(attempt_path):
                    attempt = normalize_validation_attempt(
                        _load_recovery_json(attempt_path, 256 * 1024),
                    )
                    if event.is_set() or attempt["status"] == "cancelling":
                        raise ExecutionCancelled()
                    if attempt["status"] != "running":
                        raise DependencyPreparationError(
                            "validation_attempt_state_invalid",
                            "Admitted validation attempt lost execution ownership",
                        )
                normalized_repositories = normalize_runtime_apt_repositories(repositories or [])
                coordinates: set[tuple[str, str, str]] = set()
                for repository in normalized_repositories:
                    for component in repository["components"]:
                        coordinate = (repository["uri"].rstrip("/"), repository["suite"], component)
                        if coordinate in coordinates:
                            raise DependencyPreparationError("apt_repository_origin_ambiguous", "External repository coordinates overlap")
                        coordinates.add(coordinate)
                with tempfile.TemporaryDirectory(prefix="dependency-input-", dir=workspace) as temporary:
                    inputs = Path(temporary)
                    inputs.chmod(0o700)
                    current = _snapshot_artifact(current, inputs / "current.deb")
                    if previous:
                        previous = _snapshot_artifact(previous, inputs / "previous.deb")
                    apt_conf = inputs / "apt.conf"
                    apt_conf.write_text(apt_configuration(
                        ca_info="/debbuilder-input/test-ca.crt" if test_ca_certificate else None,
                    ), encoding="utf-8")
                    apt_conf.chmod(0o400)
                    empty_apt_parts = inputs / "empty-apt-conf"
                    empty_apt_parts.mkdir(mode=0o500)
                    mounts = [
                        (apt_conf, "/debbuilder-input/apt.conf", "ro"),
                        (empty_apt_parts, "/debbuilder-empty-apt-conf", "ro"),
                        (Path(__file__).with_name("apt_state_model.py"), "/debbuilder-input/apt_state_model.py", "ro"),
                        (Path(__file__).with_name("bounded_process.py"), "/debbuilder-input/bounded_process.py", "ro"),
                        (current.path, "/debbuilder-input/current.deb", "ro"),
                    ]
                    if previous:
                        mounts.append((previous.path, "/debbuilder-input/previous.deb", "ro"))
                    if test_ca_certificate:
                        test_ca = Path(test_ca_certificate).resolve(strict=True)
                        mounts.append((test_ca, "/debbuilder-input/test-ca.crt", "ro"))
                    provenance = []
                    for index, repository in enumerate(normalized_repositories):
                        suffix = f"{index:02d}-{repository['id']}"
                        key_path = inputs / f"{suffix}.gpg"
                        inspected_key = inspect_public_key(
                            repository["signing_key"]["armored"], workspace=workspace,
                            output_keyring=key_path, runner=runner,
                        )
                        container_key = f"/etc/apt/keyrings/debbuilder-{attempt_id}-{suffix}.gpg"
                        source_path = inputs / f"{suffix}.sources"
                        source_path.write_text(deb822_source(repository, signed_by=container_key), encoding="utf-8")
                        source_path.chmod(0o400)
                        mounts += [
                            (key_path, container_key, "ro"),
                            (source_path, f"/etc/apt/sources.list.d/debbuilder-{attempt_id}-{suffix}.sources", "ro"),
                        ]
                        provenance.append({
                            "id": repository["id"], "uri": repository["uri"], "suite": repository["suite"],
                            "components": repository["components"],
                            "signing_key_sha256": inspected_key["content_sha256"],
                            "signing_key_fingerprints": [inspected_key["primary_fingerprint"], *[
                                value for value in inspected_key["signing_fingerprints"] if value != inspected_key["primary_fingerprint"]
                            ]],
                        })
                    identity = new_identity(
                        run_id=run_id, attempt_id=attempt_id, role="dependency-preparation",
                        runtime="podman", image=image,
                    )
                    container = OwnedContainer(runtime, IdentityRegistry(registry_root), identity)
                    create_result = container.create(
                        mounts=mounts,
                        tmpfs=[
                            ("/prep", STATE_TMPFS_BYTES), ("/prep/lists", METADATA_TMPFS_BYTES),
                            ("/prep/archives", ARCHIVE_TMPFS_BYTES), ("/prep/logs", LOG_TMPFS_BYTES),
                        ],
                        policy=run["resource_limits"]["effective"], network="bridge",
                    )
                    facts, start_result = container.start(policy=run["resource_limits"]["effective"])
                    inspected_container = runtime.inspect_container(identity["container_id"])
                    assert inspected_container is not None
                    enforcement = verify_resource_enforcement(
                        identity, inspected_container, run["resource_limits"]["effective"],
                        workspace=workspace, launcher_result=start_result,
                    )
                    native = container.exec(["dpkg", "--print-architecture"], timeout=30)["stdout"].strip()
                    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", native):
                        raise DependencyPreparationError("profile_architecture_invalid", "Validation profile native architecture is invalid")
                    for artifact in (current, previous):
                        if artifact and artifact.architecture not in {"all", native}:
                            raise DependencyPreparationError("artifact_architecture_mismatch", "Artifact architecture is incompatible with the selected profile")
                    base_inventory = _installed_base_packages(container, native_architecture=native)
                    immutable_base = {(row["package"], row["version"], row["architecture"]) for row in base_inventory}
                    base_packages: list[dict] = []
                    before_status = container.exec(["sha256sum", "/var/lib/dpkg/status"], timeout=30)["stdout"].split()[0]
                    _apt_exec(container, ["apt-get", "-q=2", "--error-on=any", "update"], timeout=update_timeout)
                    expected: list[tuple[str, AptDecision]] = []
                    declared_plans: list[dict] = []
                    diagnostics = []
                    if previous:
                        previous_decisions, previous_satisfied = _solve(
                            container, "previous.deb", previous, status_path="/var/lib/dpkg/status", timeout=solve_timeout,
                        )
                        base_packages += [
                            {**row, "role": "previous"} for row in previous_satisfied
                            if (row["package"], row["version"], row["architecture"]) in immutable_base
                        ]
                        previous_dependencies = [row for row in previous_decisions if not (row.package.split(":", 1)[0] == previous.package and row.version == previous.version)]
                        declared_plans += [
                            {**row, "role": "previous"} for row in
                            _download(container, "previous.deb", status_path="/var/lib/dpkg/status", timeout=solve_timeout)
                        ]
                        expected += [("previous", row) for row in previous_dependencies]
                        _model_previous(container, previous_decisions, previous)
                        current_status = "/prep/status.previous"
                    else:
                        current_status = "/var/lib/dpkg/status"
                    current_decisions, current_satisfied = _solve(
                        container, "current.deb", current, status_path=current_status, timeout=solve_timeout,
                    )
                    base_packages += [
                        {**row, "role": "current"} for row in current_satisfied
                        if (row["package"], row["version"], row["architecture"]) in immutable_base
                    ]
                    base_packages = sorted(
                        { (row["role"], row["package"], row["architecture"]): row for row in base_packages }.values(),
                        key=lambda row: (row["role"], row["package"], row["architecture"]),
                    )
                    current_dependencies = [row for row in current_decisions if not (row.package.split(":", 1)[0] == current.package and row.version == current.version)]
                    declared_plans += [
                        {**row, "role": "current"} for row in
                        _download(container, "current.deb", status_path=current_status, timeout=solve_timeout)
                    ]
                    expected += [("current", row) for row in current_dependencies]
                    unique_expected = {(row.package.split(":", 1)[0], row.version) for _, row in expected}
                    if len(unique_expected) > MAX_PACKAGES:
                        raise DependencyPreparationError("apt_package_count_exceeded", "Dependency bundle exceeds the package-count bound")
                    unique_plans = {
                        (row["role"], row["uri"], row["filename"]): row["size"] for row in declared_plans
                    }
                    if len(unique_plans) > MAX_PACKAGES or sum(unique_plans.values()) > MAX_DECLARED_PACKAGE_BYTES:
                        raise DependencyPreparationError("apt_download_size_exceeded", "Combined APT download plan exceeds its declared bound")
                    after_status = container.exec(["sha256sum", "/var/lib/dpkg/status"], timeout=30)["stdout"].split()[0]
                    if before_status != after_status:
                        raise DependencyPreparationError("apt_installation_detected", "Preparation changed the profile dpkg status")
                    package_rows = _verify_downloads(
                        container, attempt_root / "packages", expected, provenance,
                        declared_plans,
                        native_architecture=native,
                        workspace=workspace, runner=runner, cancellation_event=event,
                    )
                    base_satisfied = sum(1 for row in base_packages if row["role"] == "current")
                    diagnostics.append({
                        "code": "base_satisfied",
                        "message": f"current phase selected {base_satisfied} exact base package satisfiers and {len(current_dependencies)} downloaded decisions",
                    })
                    if previous:
                        changed = sum(1 for role, row in expected if role == "current" and any(
                            old.package == row.package and old.version != row.version for old_role, old in expected if old_role == "previous"
                        ))
                        diagnostics.append({"code": "modeled_transition", "message": f"APT modeled previous then current; changed dependency versions={changed}"})
                    prepared = normalize_prepared_runtime_dependencies({
                        "contract_version": PREPARED_DEPENDENCIES_CONTRACT_VERSION,
                        "profile_name": profile_name, "image": image, "native_architecture": native,
                        "artifacts": {"current": current.identity(), "previous": previous.identity() if previous else None},
                        "repositories": provenance, "base_packages": base_packages, "packages": package_rows,
                        "started_at": started, "finished_at": utc_now(), "diagnostics": diagnostics,
                        "enforcement": enforcement,
                    })
                _persist(attempt_root / "prepared.json", prepared)
                container.cleanup()
                container = None
                if event.is_set():
                    raise ExecutionCancelled()
                with storage.locked_path(attempt_path):
                    attempt = normalize_validation_attempt(_load_recovery_json(attempt_path, 256 * 1024))
                    if event.is_set() or attempt["status"] == "cancelling":
                        raise ExecutionCancelled()
                    if attempt["status"] != "running":
                        raise DependencyPreparationError(
                            "validation_attempt_state_invalid",
                            "Admitted validation attempt lost execution ownership",
                        )
                return {**attempt, "prepared": prepared}
    except BaseException as exc:
        main_error = exc
        if container is not None:
            try:
                container.cleanup()
                container = None
            except BaseException as cleanup:
                cleanup_error = cleanup
        if attempt is not None:
            with storage.locked_path(attempt_path):
                attempt = normalize_validation_attempt(_load_recovery_json(attempt_path, 256 * 1024))
                if attempt["status"] in {"queued", "running", "cancelling"}:
                    if cleanup_error is not None:
                        attempt["status"] = "cancelling"
                        _persist(attempt_path, normalize_validation_attempt(attempt))
                    elif attempt["status"] == "cancelling" or isinstance(exc, ExecutionCancelled):
                        attempt = _terminal_attempt(attempt, status="cancelled", code="validation_preparation_cancelled", message="Dependency preparation was cancelled")
                        _persist(attempt_path, attempt)
                    else:
                        code = getattr(exc, "code", "dependency_preparation_failed")
                        attempt = _terminal_attempt(attempt, status="failed", code=code, message="Dependency preparation failed")
                        _persist(attempt_path, attempt)
        packages_path = attempt_root / "packages"
        prepared_path = attempt_root / "prepared.json"
        if not prepared_path.is_file() and packages_path.exists():
            resolved_packages = packages_path.resolve(strict=False)
            resolved_packages.relative_to(attempt_root.resolve())
            if not packages_path.is_symlink():
                shutil.rmtree(packages_path)
        if cleanup_error is not None:
            SUPERVISOR.block("validation container cleanup is unresolved")
            raise DependencyPreparationError(
                "validation_container_cleanup_unresolved",
                "Dependency preparation failed and owned-container cleanup remains unresolved",
                details={"workload_error": type(main_error).__name__, "cleanup_error": type(cleanup_error).__name__},
            ) from cleanup_error
        raise
    finally:
        SUPERVISOR.unregister(attempt_id)
