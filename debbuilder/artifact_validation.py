"""Installation lifecycle validation for an already successful Build Run."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from .build_models import utc_now
from .build_store import BuildStore
from .command_identity import clear_identity, persist_identity, recording_identities, update_identity
from .command_runner import run_command
from .dependency_preparation import (
    DependencyPreparationError,
    MAX_ACTUAL_PACKAGE_BYTES,
    MAX_INDIVIDUAL_PACKAGE_BYTES,
    _snapshot_artifact,
    inspect_artifact,
)
from .execution_cancellation import ExecutionCancelled
from .recipe_schema import require_safe_name, validate_recipe_metadata
from .repository_lock import RepositoryLockError, repository_lease, safe_relative_path
from .validation_backend import BackendError, OwnedOciSystemdBackend
from .validation_contracts import MAX_PACKAGES, ValidationContractError, normalize_prepared_runtime_dependencies
from .validation_profiles import node_satisfies, python_satisfies, resolve_profile


LOCAL_APT_BASE_ARGUMENTS = [
    "apt-get", "-q=2", "--no-install-recommends", "--no-remove", "--assume-yes",
    "-o", "Dir::Etc::sourcelist=/dev/null",
    "-o", "Dir::Etc::sourceparts=/dev/null",
    "-o", "Dir::State::lists=/var/lib/apt/lists",
    "-o", "Dir::Cache::archives=/var/cache/apt/archives",
]


class ValidationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _check(checks: list[dict], name: str, passed: bool, *, details=None, error: str = "") -> bool:
    checks.append({"name": name, "status": "success" if passed else "failed", "details": details or {}, "error": error if not passed else ""})
    return passed


def _compact_command_result(result: dict, limit: int = 4096) -> dict:
    compact = dict(result)
    for field in ("stdout", "stderr"):
        value = str(compact.get(field) or "")
        if len(value) > limit:
            compact[field] = value[:limit] + "\n[output truncated; full result is stored in the validation command file]"
            compact[f"{field}_truncated"] = True
            compact[f"{field}_characters"] = len(value)
    return compact


def _container_artifact(workspace: Path, artifact: Path) -> str:
    try:
        relative = artifact.resolve().relative_to(workspace)
    except ValueError as exc:
        raise ValidationError("artifact_outside_workspace", "Artifact must be inside the selected Build Run workspace") from exc
    return "/validation/" + relative.as_posix()


def snapshot_previous_for_preparation_locked(
    run_id: str,
    previous_artifact: str,
    *,
    store: BuildStore,
    run: dict,
    attempt_id: str,
    allowed_previous_roots: tuple[str | Path, ...] = (),
) -> Path:
    """Snapshot one confined previous artifact while the caller owns the Run lock."""
    require_safe_name(attempt_id, "validation attempt id")
    workspace = Path(run["workspace"]).resolve()
    previous = Path(os.path.abspath(previous_artifact))
    store_root = store.root.resolve()
    resolved_previous = previous.resolve(strict=False)
    in_build_store = resolved_previous.is_relative_to(store_root)
    configured_pools = tuple(Path(os.path.abspath(root)) for root in allowed_previous_roots)
    pool_root = next((root for root in configured_pools if previous.is_relative_to(root)), None)
    if not in_build_store and pool_root is None:
        raise ValidationError("previous_artifact_outside_build_store", "Previous artifact must belong to a DebBuilder Build Run or the configured repository pool")
    if in_build_store:
        previous = resolved_previous
    if previous.suffix != ".deb" or (in_build_store and previous.parent.name != "artifacts"):
        raise ValidationError("previous_artifact_not_available", "Previous-version artifact is missing or is not a .deb")
    target_dir = workspace / "validation" / attempt_id
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / "preparation-previous.deb"
    if pool_root is not None:
        if pool_root.name != "pool":
            raise ValidationError("previous_artifact_outside_build_store", "Configured previous-artifact root must be the repository pool directory")
        repository_root = pool_root.parent
        try:
            relative = safe_relative_path(previous.relative_to(repository_root).as_posix(), required_prefix="pool")
            with repository_lease(repository_root, operation=f"validation-preparation-snapshot:{run_id}") as lease:
                with lease.open_regular(relative, required_prefix="pool") as (source_fd, _):
                    with os.fdopen(os.dup(source_fd), "rb") as source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
        except (FileExistsError, FileNotFoundError, RepositoryLockError) as exc:
            code = exc.code if isinstance(exc, RepositoryLockError) else "previous_artifact_not_available"
            raise ValidationError(code, "Previous repository artifact could not be snapshotted safely") from exc
    else:
        if not previous.is_file():
            raise ValidationError("previous_artifact_not_available", "Previous-version artifact is missing or is not a .deb")
        try:
            with previous.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as exc:
            raise ValidationError("validation_attempt_conflict", "Validation preparation snapshot already exists") from exc
    target.chmod(0o400)
    return target


def _execute(backend, arguments: list[str], checks: list[dict], name: str, *, accepted={0}, timeout=120) -> dict:
    result = backend.exec(arguments, timeout=timeout, accepted_exit_codes=set(accepted))
    _check(checks, name, bool(result.get("accepted")), details={"exit_code": result.get("exit_code")}, error=result.get("stderr") or result.get("stdout") or "command failed")
    return result


def _identity(metadata) -> dict:
    return metadata.identity()


def _inspect_bundle_file(path: Path, *, workspace: Path, runner, cancellation_event):
    try:
        return inspect_artifact(path, workspace=workspace, runner=runner, cancellation_event=cancellation_event)
    except (OSError, DependencyPreparationError) as exc:
        raise ValidationError("prepared_bundle_mismatch", "Prepared lifecycle input failed immutable identity inspection") from exc


def _snapshot_bundle_file(metadata, destination: Path):
    try:
        return _snapshot_artifact(metadata, destination)
    except (OSError, DependencyPreparationError) as exc:
        raise ValidationError("prepared_bundle_mismatch", "Prepared lifecycle input changed during private snapshot") from exc


def _prepare_lifecycle_inputs(
    *,
    workspace: Path,
    validation_dir: Path,
    attempt_id: str,
    current_artifact: Path,
    previous_artifact: Path | None,
    prepared_dependencies: dict,
    selected_profile: dict,
    runner,
    cancellation_event,
) -> dict:
    """Re-verify and privately snapshot every lifecycle-mounted package."""
    try:
        prepared = normalize_prepared_runtime_dependencies(prepared_dependencies)
    except ValidationContractError as exc:
        raise ValidationError("prepared_bundle_mismatch", str(exc), details={"path": exc.path}) from exc
    if prepared["profile_name"] != selected_profile["name"] or prepared["image"]["name"] != selected_profile["image"]:
        raise ValidationError("prepared_bundle_mismatch", "Prepared profile/image does not match lifecycle selection")

    current = _inspect_bundle_file(
        current_artifact, workspace=workspace, runner=runner, cancellation_event=cancellation_event,
    )
    previous = _inspect_bundle_file(
        previous_artifact, workspace=workspace, runner=runner, cancellation_event=cancellation_event,
    ) if previous_artifact else None
    if _identity(current) != prepared["artifacts"]["current"]:
        raise ValidationError("prepared_bundle_mismatch", "Current artifact identity differs from dependency preparation")
    if (None if previous is None else _identity(previous)) != prepared["artifacts"]["previous"]:
        raise ValidationError("prepared_bundle_mismatch", "Previous artifact identity differs from dependency preparation")

    attempt_root = workspace / "manifests" / "validation-attempts" / attempt_id
    packages_root = attempt_root / "packages"
    try:
        attempt_root.resolve(strict=True).relative_to(workspace.resolve(strict=True))
        root_info = packages_root.lstat()
    except (OSError, ValueError) as exc:
        raise ValidationError("prepared_bundle_mismatch", "Prepared package directory is missing or outside the Build Run") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ValidationError("prepared_bundle_mismatch", "Prepared package directory is not a real directory")
    with os.scandir(packages_root) as inventory:
        entries = sorted(inventory, key=lambda row: row.name)
    if len(entries) > MAX_PACKAGES:
        raise ValidationError("prepared_bundle_mismatch", "Prepared package file count exceeds its bound")
    actual = {}
    actual_paths = {}
    total = 0
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        if (
            not entry.name.endswith(".deb") or not stat.S_ISREG(info.st_mode)
            or entry.is_symlink() or info.st_nlink != 1 or not 0 < info.st_size <= MAX_INDIVIDUAL_PACKAGE_BYTES
        ):
            raise ValidationError("prepared_bundle_mismatch", "Prepared package inventory contains an unsafe file")
        metadata = _inspect_bundle_file(
            Path(entry.path), workspace=workspace, runner=runner, cancellation_event=cancellation_event,
        )
        key = (metadata.package, metadata.version, metadata.architecture)
        if key in actual:
            raise ValidationError("prepared_bundle_mismatch", "Prepared package files contain a duplicate identity")
        actual[key], actual_paths[key] = _identity(metadata), metadata.path
        total += metadata.size
    if total > MAX_ACTUAL_PACKAGE_BYTES:
        raise ValidationError("prepared_bundle_mismatch", "Prepared package bytes exceed their hard bound")
    expected = {
        (row["package"], row["version"], row["architecture"]): {
            key: row[key] for key in ("package", "version", "architecture", "size", "sha256")
        }
        for row in prepared["packages"]
    }
    if set(actual) != set(expected) or any(actual[key] != expected[key] for key in expected):
        raise ValidationError("prepared_bundle_mismatch", "Prepared package files do not exactly match their manifest")

    inputs = validation_dir / "lifecycle-input"
    inputs.mkdir(mode=0o700)
    dependency_inputs = inputs / "dependencies"
    dependency_inputs.mkdir(mode=0o700)
    current_snapshot = _snapshot_bundle_file(current, inputs / "current.deb")
    previous_snapshot = _snapshot_bundle_file(previous, inputs / "previous.deb") if previous else None
    mounts = [(current_snapshot.path, "/debbuilder-input/current.deb", "ro")]
    if previous_snapshot:
        mounts.append((previous_snapshot.path, "/debbuilder-input/previous.deb", "ro"))
    container_paths = {}
    for index, key in enumerate(sorted(expected)):
        metadata = _inspect_bundle_file(
            actual_paths[key], workspace=workspace, runner=runner, cancellation_event=cancellation_event,
        )
        snapshot = _snapshot_bundle_file(metadata, dependency_inputs / f"dependency-{index:03d}.deb")
        container_path = f"/debbuilder-bundle/dependency-{index:03d}.deb"
        container_paths[key] = container_path
    dependency_inputs.chmod(0o500)
    mounts.append((dependency_inputs, "/debbuilder-bundle", "ro"))
    inputs.chmod(0o500)

    phase_paths = {}
    for role in ("previous", "current"):
        phase_paths[role] = [
            container_paths[(row["package"], row["version"], row["architecture"])]
            for row in prepared["packages"] if row["role"] == role
        ]
    return {
        "prepared": prepared,
        "mounts": mounts,
        "current_artifact": "/debbuilder-input/current.deb",
        "previous_artifact": "/debbuilder-input/previous.deb" if previous_snapshot else "",
        "phase_paths": phase_paths,
    }


def _local_apt_arguments(*, conffile_option: str, package_paths: list[str]) -> list[str]:
    return [
        *LOCAL_APT_BASE_ARGUMENTS[:5],
        "-o", f"Dpkg::Options::={conffile_option}",
        *LOCAL_APT_BASE_ARGUMENTS[5:],
        "install", *package_paths,
    ]


def _expected_phase_state(prepared: dict, role: str) -> list[dict]:
    rows = [dict(row) for row in prepared["base_packages"] if row["role"] == role]
    if role == "previous":
        rows += [dict(row) for row in prepared["packages"] if row["role"] == "previous"]
        artifact = prepared["artifacts"]["previous"]
    else:
        current_names = {row["package"] for row in prepared["packages"] if row["role"] == "current"}
        rows += [dict(row) for row in prepared["packages"] if row["role"] == "previous" and row["package"] not in current_names]
        rows += [dict(row) for row in prepared["packages"] if row["role"] == "current"]
        artifact = prepared["artifacts"]["current"]
    if artifact:
        rows.append(dict(artifact))
    return list({(row["package"], row["architecture"]): row for row in rows}.values())


def _verify_modeled_state(backend, prepared: dict, role: str, checks: list[dict]) -> None:
    mismatches = []
    for row in _expected_phase_state(prepared, role):
        result = backend.exec([
            "dpkg-query", "--show",
            "--showformat=${binary:Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\n",
            row["package"],
        ], accepted_exit_codes={0})
        fields = str(result.get("stdout") or "").strip().split("\t")
        actual = fields if len(fields) == 4 else []
        actual_package = actual[0].split(":", 1)[0] if actual else ""
        if (
            not result.get("accepted") or not actual
            or actual_package != row["package"] or actual[1] != row["version"]
            or actual[2] != row["architecture"] or actual[3] != "installed"
        ):
            mismatches.append({
                "package": row["package"], "expected_version": row["version"],
                "expected_architecture": row["architecture"], "actual": actual,
            })
    passed = not mismatches
    _check(
        checks, f"modeled_state_{role}", passed,
        details={"expected_count": len(_expected_phase_state(prepared, role)), "mismatches": mismatches[:20]},
        error="Installed package state differs from the prepared APT model",
    )
    if not passed:
        raise ValidationError(
            "modeled_state_drift", "Installed package state differs from the prepared APT model",
            details={"phase": role, "mismatches": mismatches[:20]},
        )


def _installed_inventory(backend) -> dict[tuple[str, str], str]:
    result = backend.exec([
        "dpkg-query", "--show",
        "--showformat=${binary:Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\n",
    ], accepted_exit_codes={0})
    if not result.get("accepted"):
        raise ValidationError("modeled_state_drift", "Lifecycle package inventory could not be read")
    rows = {}
    for line in str(result.get("stdout") or "").splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[3] != "installed":
            continue
        package = fields[0].split(":", 1)[0]
        key = (package, fields[2])
        if key in rows:
            raise ValidationError("modeled_state_drift", "Lifecycle package inventory contains duplicate identities")
        rows[key] = fields[1]
    if len(rows) > MAX_PACKAGES:
        raise ValidationError("modeled_state_drift", "Lifecycle package inventory exceeds its bound")
    return rows


def _verify_transaction_boundary(
    before: dict[tuple[str, str], str],
    after: dict[tuple[str, str], str],
    *,
    allowed: set[tuple[str, str]],
    phase: str,
    checks: list[dict],
) -> None:
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    unexpected = sorted(changed - allowed)
    removed = sorted(set(before) - set(after))
    passed = not unexpected and not removed
    _check(
        checks, f"exact_local_transaction_{phase}", passed,
        details={
            "changed_count": len(changed),
            "allowed_count": len(allowed),
            "unexpected": [f"{package}:{architecture}" for package, architecture in unexpected[:20]],
            "removed": [f"{package}:{architecture}" for package, architecture in removed[:20]],
        },
        error="Offline APT changed package state outside the prepared transaction",
    )
    if not passed:
        raise ValidationError(
            "modeled_state_drift", "Offline APT changed package state outside the prepared transaction",
            details={"phase": phase},
        )


def _raise_install_failure(result: dict, *, phase: str, target_package: str, dependency_packages: set[str]) -> None:
    output = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    script_failure = "installed " in output and "script subprocess returned error" in output
    if script_failure:
        code = "maintainer_script_failed"
    elif any(
        re.search(rf"(?<![a-z0-9+.-]){re.escape(name.lower())}(?![a-z0-9+.-])", output)
        for name in dependency_packages
    ):
        code = "dependency_install_failed"
    elif target_package.lower() in output:
        code = "previous_artifact_install_failed" if phase == "previous" else "target_artifact_install_failed"
    else:
        code = "previous_install_failed" if phase == "previous" else "package_install_failed"
    raise ValidationError(code, f"Offline {phase} package transaction failed", details={"phase": phase})


def _payload_state(backend, recipe: dict, checks: list[dict], name: str) -> list[dict]:
    destination = recipe["install"]["destination"]
    configured_only = recipe["install"]["content"]["source"] == "configured_files"
    mapped_paths = [row["destination"] if isinstance(row, dict) else row for row in recipe["install"]["config_files"]]
    arguments = ["stat", "--format=%a|%U|%G|%F|%n", *mapped_paths] if configured_only else ["find", destination, "-printf", "%m|%u|%g|%y|%p\\n"]
    result = _execute(backend, arguments, checks, name)
    rows = []
    for line in result.get("stdout", "").splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            row = dict(zip(("mode", "user", "group", "type", "path"), parts))
            if row["type"] == "directory":
                row["type"] = "d"
            elif row["type"] == "regular file":
                row["type"] = "f"
            rows.append(row)
    owner = recipe["install"]["owner"]
    expected_by_path = {row["destination"]: {"user": row.get("owner") or owner["user"], "group": row.get("group") or owner["group"]} for row in recipe["install"]["config_files"]}
    ownership_ok = bool(rows) and all(row["user"] == expected_by_path.get(row["path"], owner)["user"] and row["group"] == expected_by_path.get(row["path"], owner)["group"] for row in rows)
    inventory = {"count": len(rows), "sample": rows[:100], "sample_truncated": len(rows) > 100}
    _check(checks, f"{name}_ownership", ownership_ok, details={"default": {"user": owner["user"], "group": owner["group"]}, "overrides": expected_by_path, **inventory})
    directory_mode = recipe["install"]["directory_mode"].lstrip("0") or "0"
    file_mode = recipe["install"]["file_mode"].lstrip("0") or "0"
    mode_by_path = {row["destination"]: (row.get("mode") or recipe["install"]["file_mode"]).lstrip("0") or "0" for row in recipe["install"]["config_files"]}
    permissions_ok = bool(rows) and all(
        True if row["type"] == "l"
        else row["mode"] == directory_mode if row["type"] == "d"
        else row["mode"] == mode_by_path[row["path"]] if row["path"] in mode_by_path
        else (int(row["mode"], 8) & ~0o111) == int(file_mode, 8) and (int(row["mode"], 8) & 0o111) in {0, 0o111}
        for row in rows
    )
    _check(checks, f"{name}_permissions", permissions_ok, details={"directories": directory_mode, "regular_files_base": file_mode, "symbolic_links": "excluded (target permissions apply)", **inventory})
    return rows


def _config_paths(run: dict) -> list[str]:
    staging = next((step.get("details") or {} for step in run["steps"] if step.get("name") == "staging"), {})
    return [row["destination"] for row in staging.get("configurations", []) if row.get("destination")]


def _runtime_dependency_specs(artifact_data: dict) -> list[dict]:
    """Return runtime packages from the inspected final Debian control metadata."""
    inspection = artifact_data.get("inspection")
    if not isinstance(inspection, dict) or not isinstance(inspection.get("depends"), str):
        raise ValidationError("artifact_metadata_invalid", "Final artifact dependency inspection is missing or invalid")
    depends = inspection["depends"]
    specs = []
    if not depends.strip():
        return specs
    for clause in depends.split(","):
        for alternative in clause.split("|"):
            match = re.fullmatch(r"\s*([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?(?:\s*\((>=|<=|=|<<|>>)\s*([^)]*?)\s*\))?\s*", alternative, re.I)
            if not match or (match.group(2) and not (match.group(3) or "").strip()):
                raise ValidationError("artifact_metadata_invalid", "Final artifact dependency inspection is malformed")
            specs.append({"package": match.group(1).lower(), "operator": match.group(2) or "", "version": (match.group(3) or "").strip()})
    return specs


def _runtime_version_requirement(spec: dict) -> str:
    """Translate a simple Debian relation when it is also a Node/Python range."""
    version = str(spec.get("version") or "")
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        return ""
    operator = {"=": "==", "<<": "<", ">>": ">"}.get(str(spec.get("operator") or ""), str(spec.get("operator") or ""))
    return f"{operator}{version}" if operator else ""


def _runtime_checks(specs: list[dict], backend, checks: list[dict], profile: dict) -> None:
    """Verify interpreters explicitly required by the installed Debian package."""
    runtimes = (
        ("nodejs", "runtime_node", "Node.js", ["node", "--version"], node_satisfies),
        ("python3", "runtime_python", "Python", ["python3", "--version"], python_satisfies),
    )
    for package, check_name, display_name, command, satisfies in runtimes:
        spec = next((row for row in specs if row["package"] == package), None)
        if not spec:
            continue
        requirement = _runtime_version_requirement(spec)
        result = backend.exec(command, accepted_exit_codes={0})
        actual = (result.get("stdout") or result.get("stderr") or "").strip()
        compatible = bool(result.get("accepted")) and bool(re.search(r"\d+\.\d+", actual)) and (not requirement or satisfies(actual, requirement))
        _check(checks, check_name, compatible, details={"package": package, "required": requirement or "declared runtime package", "actual": actual, "profile": profile["name"]}, error=result.get("stderr") or f"{display_name} is missing or incompatible")
        if not compatible:
            expected = requirement or f"the declared {package} runtime"
            raise ValidationError("validation_runtime_incompatible", f"{display_name} {actual or 'missing'} does not satisfy {expected}", details={"package": package, "required": requirement, "actual": actual})


def _capture_systemd_failure(backend, checks: list[dict], service_name: str) -> None:
    """Attach systemd's service-level explanation to a failed active check."""
    status = backend.exec(["systemctl", "status", "--no-pager", "--full", service_name], accepted_exit_codes={0, 3, 4})
    output = (status.get("stdout") or status.get("stderr") or "").strip()
    if not checks or not output:
        return
    checks[-1]["details"]["systemctl_status"] = output
    checks[-1]["error"] = output


def validate_artifact(
    run_id: str,
    *,
    store: BuildStore,
    previous_artifact: str = "",
    backend_factory=None,
    profile: str = "bookworm",
    allowed_previous_roots: tuple[str | Path, ...] = (),
    prepared_dependencies: dict,
    attempt_id: str,
    registry_root: str | Path,
    cancellation_event=None,
    runner=run_command,
) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise ValidationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id) as workspace_fd:
        run = store.load(run_id)
        if not run:
            raise ValidationError("build_run_not_found", "Build Run was not found")
        policy = (run.get("resource_limits") or {}).get("effective") or {}
        with recording_identities(
            run_id,
            record=lambda identity: persist_identity(workspace_fd, identity),
            clear=lambda identity: clear_identity(workspace_fd, identity),
            update=lambda expected, updated: update_identity(workspace_fd, expected, updated),
            resource_policy=policy,
        ):
            return _validate_artifact_locked(
                run_id,
                store=store,
                previous_artifact=previous_artifact,
                backend_factory=backend_factory,
                profile=profile,
                allowed_previous_roots=allowed_previous_roots,
                prepared_dependencies=prepared_dependencies,
                attempt_id=attempt_id,
                registry_root=registry_root,
                cancellation_event=cancellation_event,
                runner=runner,
                workspace_fd=workspace_fd,
            )


def _validate_artifact_locked(
    run_id: str,
    *,
    store: BuildStore,
    previous_artifact: str = "",
    backend_factory=None,
    profile: str = "bookworm",
    allowed_previous_roots: tuple[str | Path, ...] = (),
    prepared_dependencies: dict,
    attempt_id: str,
    registry_root: str | Path,
    cancellation_event=None,
    runner=run_command,
    workspace_fd: int,
) -> dict:
    """Validate install/upgrade/remove/purge without changing the Build status."""
    run = store.load(run_id)
    if not run:
        raise ValidationError("build_run_not_found", "Build Run was not found")
    artifact_data = run.get("artifact") or {}
    if (
        run.get("status") != "success"
        or not artifact_data.get("path")
        or artifact_data.get("pruning") is not None
    ):
        raise ValidationError("artifact_not_available", "A successful Build Run with an artifact is required")
    workspace = Path(run["workspace"]).resolve()
    artifact = Path(artifact_data["path"]).resolve()
    if not artifact.is_file():
        raise ValidationError("artifact_not_available", "Build Run artifact no longer exists")
    runtime_specs = _runtime_dependency_specs(artifact_data)
    if not isinstance(prepared_dependencies, dict):
        raise ValidationError(
            "prepared_dependencies_required",
            "Canonical prepared dependency evidence is required for lifecycle validation",
        )
    if not attempt_id or registry_root is None:
        raise ValidationError(
            "prepared_bundle_mismatch",
            "Prepared lifecycle requires an attempt identity and OCI registry",
        )
    validation_id = attempt_id
    require_safe_name(validation_id, "validation attempt id")
    validation_dir = workspace / "validation" / validation_id
    commands_dir = validation_dir / "commands"
    commands_dir.mkdir(parents=True, mode=0o700)
    commands = []

    def command_completed(result):
        path = commands_dir / f"{int(result['index']):03d}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        path.chmod(0o600)
        compact = _compact_command_result(result)
        compact["result_file"] = path.relative_to(workspace).as_posix()
        commands.append(compact)

    try:
        selected_profile = resolve_profile(profile)
    except ValueError as exc:
        raise ValidationError("validation_profile_unknown", str(exc)) from exc
    started = time.monotonic()
    result = {
        "id": validation_id, "build_run_id": run_id, "artifact": str(artifact), "previous_artifact": "",
        "status": "running", "started_at": utc_now(), "finished_at": None, "duration": None,
        "backend": {
            "network": "unverified", "network_verified": False,
            "stop": {"status": "success", "absence_proved": True},
        },
        "profile": selected_profile, "checks": [], "commands": commands, "error": None,
    }
    checks = result["checks"]
    snapshot_recipe = validate_recipe_metadata(json.loads((workspace / "recipe.json").read_text()))
    upstream_mode = snapshot_recipe.get("artifact", {}).get("mode") == "upstream_deb"
    expected_scripts = sorted(next((step.get("details", {}).get("maintainer_scripts", {}) for step in run["steps"] if step.get("name") == "debian_metadata"), {}))
    inspected_scripts = sorted(artifact_data.get("inspection", {}).get("maintainer_scripts", []))
    if upstream_mode:
        expected_scripts = inspected_scripts
    _check(checks, "maintainer_scripts_present", inspected_scripts == expected_scripts, details={"expected": expected_scripts, "inspected": inspected_scripts})
    container_artifact = _container_artifact(workspace, artifact)
    previous_container = ""
    if previous_artifact:
        previous = Path(os.path.abspath(previous_artifact))
        store_root = store.root.resolve()
        resolved_previous = previous.resolve(strict=False)
        in_build_store = resolved_previous.is_relative_to(store_root)
        admitted_snapshot = bool(attempt_id) and resolved_previous == (
            workspace / "validation" / attempt_id / "preparation-previous.deb"
        ).resolve(strict=False)
        configured_pools = tuple(Path(os.path.abspath(root)) for root in allowed_previous_roots)
        pool_root = next((root for root in configured_pools if previous.is_relative_to(root)), None)
        if not in_build_store and pool_root is None and not admitted_snapshot:
            raise ValidationError("previous_artifact_outside_build_store", "Previous artifact must belong to a DebBuilder Build Run or the configured repository pool")
        if in_build_store:
            previous = resolved_previous
        if previous.suffix != ".deb" or (in_build_store and previous.parent.name != "artifacts" and not admitted_snapshot):
            raise ValidationError("previous_artifact_not_available", "Previous-version artifact is missing or is not a .deb")
        previous_copy = validation_dir / "previous.deb"
        if pool_root is not None:
            if pool_root.name != "pool":
                raise ValidationError("previous_artifact_outside_build_store", "Configured previous-artifact root must be the repository pool directory")
            repository_root = pool_root.parent
            try:
                relative = safe_relative_path(previous.relative_to(repository_root).as_posix(), required_prefix="pool")
                with repository_lease(repository_root, operation=f"validation-snapshot:{run_id}") as lease:
                    with lease.open_regular(relative, required_prefix="pool") as (source_fd, _):
                        with os.fdopen(os.dup(source_fd), "rb") as source, previous_copy.open("wb") as destination:
                            shutil.copyfileobj(source, destination)
                            destination.flush()
                            os.fsync(destination.fileno())
            except (FileNotFoundError, RepositoryLockError) as exc:
                code = exc.code if isinstance(exc, RepositoryLockError) else "previous_artifact_not_available"
                raise ValidationError(code, "Previous repository artifact could not be snapshotted safely") from exc
        else:
            if not previous.is_file():
                raise ValidationError("previous_artifact_not_available", "Previous-version artifact is missing or is not a .deb")
            shutil.copyfile(previous, previous_copy)
        previous_copy.chmod(0o400)
        previous_container = "/validation/" + previous_copy.relative_to(workspace).as_posix()
        result["previous_artifact"] = str(previous)
    try:
        bundle = _prepare_lifecycle_inputs(
            workspace=workspace,
            validation_dir=validation_dir,
            attempt_id=attempt_id,
            current_artifact=artifact,
            previous_artifact=previous_copy if previous_container else None,
            prepared_dependencies=prepared_dependencies,
            selected_profile=selected_profile,
            runner=runner,
            cancellation_event=cancellation_event,
        )
    except ExecutionCancelled:
        result["status"] = "cancelled"
        result["error"] = {
            "code": "validation_lifecycle_cancelled",
            "message": "Offline lifecycle validation was cancelled",
            "details": {},
        }
        result["finished_at"] = utc_now()
        result["duration"] = round(time.monotonic() - started, 6)
        store.append_event(run, f"Artifact validation {validation_id}: cancelled", level="info")
        return result
    container_artifact = bundle["current_artifact"]
    previous_container = bundle["previous_artifact"]
    factory = backend_factory or (lambda **kwargs: OwnedOciSystemdBackend(**kwargs))
    backend = factory(
        workspace=workspace,
        image=selected_profile["image"],
        on_result=command_completed,
        run_id=run_id,
        attempt_id=attempt_id,
        registry_root=registry_root,
        resource_policy=run["resource_limits"]["effective"],
        mounts=bundle["mounts"],
        expected_image=bundle["prepared"]["image"],
        cancellation_event=cancellation_event,
        runner=runner,
    )
    recipe = snapshot_recipe
    package = recipe["package"]["name"]
    configs = artifact_data.get("inspection", {}).get("conffiles", []) if upstream_mode else _config_paths(run)
    config_policies = {row["destination"]: row["policy"] for row in recipe["install"]["config_files"]} if not upstream_mode else {}
    marker = f"debbuilder-validation-{validation_id}"
    installed = False
    try:
        result["backend"] = backend.start(validation_id)
        result["backend"]["profile"] = selected_profile["name"]
        network_ok = result["backend"].get("network") == "disabled" and result["backend"].get("network_verified") is True
        _check(checks, "lifecycle_network_disabled", network_ok, details={"proof": "create-time OCI inspection plus in-container interface/route probe"})
        if not network_ok:
            raise ValidationError("validation_network_isolation_unverified", "Lifecycle network isolation was not proved before installation")
        native = backend.exec(["dpkg", "--print-architecture"], accepted_exit_codes={0})
        architecture_ok = native.get("accepted") and str(native.get("stdout") or "").strip() == bundle["prepared"]["native_architecture"]
        _check(
            checks, "lifecycle_architecture",
            bool(architecture_ok),
            details={"expected": bundle["prepared"]["native_architecture"], "actual": str(native.get("stdout") or "").strip()},
        )
        if not architecture_ok:
            raise ValidationError("prepared_bundle_mismatch", "Lifecycle native architecture differs from dependency preparation")
        inventory_before = _installed_inventory(backend)
        _execute(backend, ["dpkg-deb", "--info", container_artifact], checks, "debian_metadata")
        _execute(backend, ["dpkg-deb", "--contents", container_artifact], checks, "debian_contents")
        if previous_container:
            previous_arguments = _local_apt_arguments(
                conffile_option="--force-confnew",
                package_paths=[*bundle["phase_paths"]["previous"], previous_container],
            )
            previous_install = _execute(backend, previous_arguments, checks, "previous_version_install", timeout=300)
            installed = bool(previous_install.get("accepted"))
            if not installed:
                dependencies = {row["package"] for row in bundle["prepared"]["packages"] if row["role"] == "previous"}
                _raise_install_failure(previous_install, phase="previous", target_package=package, dependency_packages=dependencies)
            inventory_after_previous = _installed_inventory(backend)
            allowed_previous = {
                (row["package"], row["architecture"])
                for row in bundle["prepared"]["packages"] if row["role"] == "previous"
            }
            previous_identity = bundle["prepared"]["artifacts"]["previous"]
            allowed_previous.add((previous_identity["package"], previous_identity["architecture"]))
            _verify_transaction_boundary(
                inventory_before, inventory_after_previous,
                allowed=allowed_previous, phase="previous", checks=checks,
            )
            _verify_modeled_state(backend, bundle["prepared"], "previous", checks)
            inventory_before = inventory_after_previous
            for path in configs:
                _execute(backend, ["sh", "-c", 'printf "\\n%s\\n" "$1" >> "$2"', "debbuilder-config-marker", marker, path], checks, f"configuration_modified:{path}")
        conffile_option = "--force-confold" if previous_container else "--force-confnew"
        install_arguments = _local_apt_arguments(
            conffile_option=conffile_option,
            package_paths=[*bundle["phase_paths"]["current"], container_artifact],
        )
        install = _execute(backend, install_arguments, checks, "package_install", timeout=300)
        installed = bool(install.get("accepted"))
        if not installed:
            dependencies = {row["package"] for row in bundle["prepared"]["packages"] if row["role"] == "current"}
            _raise_install_failure(install, phase="current", target_package=package, dependency_packages=dependencies)
        if installed:
            inventory_after_current = _installed_inventory(backend)
            allowed_current = {
                (row["package"], row["architecture"])
                for row in bundle["prepared"]["packages"] if row["role"] == "current"
            }
            current_identity = bundle["prepared"]["artifacts"]["current"]
            allowed_current.add((current_identity["package"], current_identity["architecture"]))
            _verify_transaction_boundary(
                inventory_before, inventory_after_current,
                allowed=allowed_current, phase="current", checks=checks,
            )
            _verify_modeled_state(backend, bundle["prepared"], "current", checks)
            _runtime_checks(runtime_specs, backend, checks, selected_profile)
            package_status = backend.exec(["dpkg-query", "--show", "--showformat=${Status}\\n", package], accepted_exit_codes={0})
            status_ok = bool(package_status.get("accepted")) and package_status.get("stdout", "").strip() == "install ok installed"
            _check(checks, "package_status_installed", status_ok, details={"status": package_status.get("stdout", "").strip()}, error=package_status.get("stderr") or "Package is not fully installed")
            account = recipe["install"].get("account") or recipe["install"]["owner"]
            if account.get("create_group") and account["group"] != "root":
                _execute(backend, ["getent", "group", account["group"]], checks, "service_group_present")
            if account.get("create_user") and account["user"] != "root":
                _execute(backend, ["getent", "passwd", account["user"]], checks, "service_user_present")
            for directory in recipe["install"].get("directories") or []:
                state = _execute(backend, ["stat", "--format=%a|%U|%G", directory["path"]], checks, f"persistent_directory_present:{directory['path']}")
                expected = f"{directory['mode'].lstrip('0')}|{directory['owner']}|{directory['group']}"
                _check(checks, f"persistent_directory_metadata:{directory['path']}", state.get("stdout", "").strip() == expected, details={"expected": expected, "actual": state.get("stdout", "").strip()})
                writer = directory["owner"] if directory["owner"] != "root" else "root"
                _execute(backend, ["runuser", "-u", writer, "--", "touch", f"{directory['path']}/.debbuilder-validation-marker"], checks, f"persistent_directory_writable:{directory['path']}")
            if upstream_mode:
                inspected_files = store.artifact_files(run_id, artifact_data.get("inspection", {}))
                payload_paths = ["/" + row["path"].lstrip("./") for row in inspected_files if row.get("path") and not row["path"].endswith("/")]
                representative = payload_paths[:20]
                if representative:
                    _execute(backend, ["test", "-e", representative[0]], checks, "installed_payload_present")
                unit_paths = [path for path in payload_paths if path.endswith(".service") and "/systemd/" in path]
                unit_paths.extend("/" + row["path"].lstrip("./") for row in artifact_data.get("inspection", {}).get("service_units", []) if row.get("path"))
                unit_paths = sorted(set(unit_paths))
            else:
                _payload_state(backend, recipe, checks, "installed_payload")
                unit_paths = []
            for path in configs:
                _execute(backend, ["test", "-f", path], checks, f"configuration_present:{path}")
                if previous_container:
                    if config_policies.get(path, "dpkg_conffile") == "replace":
                        _execute(backend, ["grep", "--fixed-strings", "--quiet", marker, path], checks, f"configuration_replaced:{path}", accepted={1})
                    else:
                        _execute(backend, ["grep", "--fixed-strings", "--quiet", marker, path], checks, f"configuration_preserved:{path}")
            service = recipe["service"]
            if upstream_mode and unit_paths:
                _execute(backend, ["systemctl", "daemon-reload"], checks, "systemd_daemon_reload")
                for unit_path in unit_paths:
                    _execute(backend, ["systemctl", "cat", Path(unit_path).name], checks, f"systemd_unit_present:{Path(unit_path).name}")
            elif service["configured"]:
                _execute(backend, ["systemctl", "daemon-reload"], checks, "systemd_daemon_reload")
                _execute(backend, ["systemctl", "cat", service["name"]], checks, f"systemd_unit_present:{service['name']}")
                if service["enabled"]:
                    _execute(backend, ["systemctl", "is-enabled", "--quiet", service["name"]], checks, "systemd_enabled")
                    active = _execute(backend, ["systemctl", "is-active", "--quiet", service["name"]], checks, "systemd_active")
                    if not active.get("accepted"):
                        _capture_systemd_failure(backend, checks, service["name"])
                    _execute(backend, ["sleep", "2"], checks, "systemd_startup_grace", timeout=10)
                    active_after_grace = _execute(backend, ["systemctl", "is-active", "--quiet", service["name"]], checks, "systemd_active_after_grace")
                    if not active_after_grace.get("accepted"):
                        _capture_systemd_failure(backend, checks, service["name"])
            remove = _execute(backend, ["dpkg", "--remove", package], checks, "package_remove", timeout=300)
            if remove.get("accepted"):
                if not upstream_mode and recipe["install"]["content"]["source"] != "configured_files":
                    _execute(backend, ["test", "!", "-e", recipe["install"]["destination"]], checks, "payload_absent_after_remove")
                if not upstream_mode and service["enabled"]:
                    inactive = backend.exec(["systemctl", "is-active", "--quiet", service["name"]], accepted_exit_codes={3, 4})
                    _check(checks, "systemd_inactive_after_remove", bool(inactive.get("accepted")), details={"exit_code": inactive.get("exit_code")})
                for path in configs:
                    expected = config_policies.get(path, "dpkg_conffile") in {"dpkg_conffile", "create_if_missing"}
                    arguments = ["test", "-e", path] if expected else ["test", "!", "-e", path]
                    check = backend.exec(arguments, accepted_exit_codes={0})
                    _check(checks, f"configuration_after_remove:{path}", bool(check.get("accepted")), details={"expected_present": expected})
            purge = _execute(backend, ["dpkg", "--purge", package], checks, "package_purge", timeout=300)
            if purge.get("accepted"):
                for path in configs:
                    _execute(backend, ["test", "!", "-e", path], checks, f"configuration_absent_after_purge:{path}")
                if not upstream_mode and service["configured"]:
                    _execute(backend, ["test", "!", "-e", f"/usr/lib/systemd/system/{service['name']}"], checks, "systemd_unit_absent_after_purge")
                for directory in recipe["install"].get("directories") or []:
                    _execute(backend, ["test", "-f", f"{directory['path']}/.debbuilder-validation-marker"], checks, f"persistent_directory_preserved_after_purge:{directory['path']}")
                absent = backend.exec(["dpkg-query", "--show", package], accepted_exit_codes={1})
                _check(checks, "package_absent_after_purge", bool(absent.get("accepted")), details={"exit_code": absent.get("exit_code")})
        result["status"] = "success" if checks and all(row["status"] == "success" for row in checks) else "failed"
        if result["status"] == "failed":
            failed_checks = [row["name"] for row in checks if row["status"] == "failed"]
            if "package_remove" in failed_checks:
                code, message = "package_remove_failed", "Target package removal failed"
            elif "package_purge" in failed_checks:
                code, message = "package_purge_failed", "Target package purge failed"
            elif any(name.startswith("systemd_") for name in failed_checks):
                code, message = "systemd_check_failed", "One or more systemd lifecycle checks failed"
            else:
                code, message = "validation_checks_failed", "One or more installation validation checks failed"
            result["error"] = {"code": code, "message": message, "details": {"failed_checks": failed_checks}}
    except BackendError as exc:
        result["status"] = "failed"
        result["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
    except ValidationError as exc:
        result["status"] = "failed"
        result["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
    except ExecutionCancelled:
        result["status"] = "cancelled"
        result["error"] = {
            "code": "validation_lifecycle_cancelled",
            "message": "Offline lifecycle validation was cancelled",
            "details": {},
        }
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = {"code": "validation_execution_failed", "message": str(exc), "details": {}}
    finally:
        try:
            stopped = backend.stop()
            if stopped:
                result["backend"]["stop"] = stopped
        except BackendError as exc:
            result["status"] = "failed"
            result["error"] = {
                "code": "validation_container_cleanup_unresolved",
                "message": "Validation container cleanup could not be proved",
                "details": {"cleanup_code": exc.code},
            }
        result["finished_at"] = utc_now()
        result["duration"] = round(time.monotonic() - started, 6)
        store.append_event(run, f"Artifact validation {validation_id}: {result['status']}", level="error" if result["status"] == "failed" else "info")
    return result
