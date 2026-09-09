"""Installation lifecycle validation for an already successful Build Run."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from .build_models import utc_now
from .build_store import BuildStore
from .recipe_schema import validate_recipe_metadata
from .repository_lock import RepositoryLockError, repository_lease, safe_relative_path
from .validation_backend import BackendError, OciSystemdBackend
from .validation_profiles import node_satisfies, python_satisfies, resolve_profile


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


def _execute(backend, arguments: list[str], checks: list[dict], name: str, *, accepted={0}, timeout=120) -> dict:
    result = backend.exec(arguments, timeout=timeout, accepted_exit_codes=set(accepted))
    _check(checks, name, bool(result.get("accepted")), details={"exit_code": result.get("exit_code")}, error=result.get("stderr") or result.get("stdout") or "command failed")
    return result


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


def _runtime_dependency_specs(artifact_data: dict, recipe: dict) -> list[dict]:
    """Return runtime packages from the final Debian control metadata.

    Project detection describes the source build environment. It is not a
    declaration about what the installed artifact needs. The inspected
    ``Depends`` field is authoritative; the Recipe is a compatibility fallback
    for historical runs that predate inspection data.
    """
    inspection = artifact_data.get("inspection") if isinstance(artifact_data.get("inspection"), dict) else {}
    depends = inspection.get("depends")
    if depends is None:
        depends = ", ".join(recipe.get("package", {}).get("runtime_dependencies") or [])
    specs = []
    for clause in str(depends or "").split(","):
        for alternative in clause.split("|"):
            match = re.match(r"\s*([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?(?:\s*\((>=|<=|=|<<|>>)\s*([^)]*)\))?", alternative, re.I)
            if match:
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


def validate_artifact(run_id: str, *, store: BuildStore, previous_artifact: str = "", backend_factory=None, profile: str = "bookworm", allowed_previous_roots: tuple[str | Path, ...] = ()) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise ValidationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id) as workspace_fd:
        return _validate_artifact_locked(
            run_id,
            store=store,
            previous_artifact=previous_artifact,
            backend_factory=backend_factory,
            profile=profile,
            allowed_previous_roots=allowed_previous_roots,
            workspace_fd=workspace_fd,
        )


def _validate_artifact_locked(run_id: str, *, store: BuildStore, previous_artifact: str = "", backend_factory=None, profile: str = "bookworm", allowed_previous_roots: tuple[str | Path, ...] = (), workspace_fd: int) -> dict:
    """Validate install/upgrade/remove/purge without changing the Build status."""
    run = store.load(run_id)
    if not run:
        raise ValidationError("build_run_not_found", "Build Run was not found")
    artifact_data = run.get("artifact") or {}
    if run.get("status") != "success" or not artifact_data.get("path"):
        raise ValidationError("artifact_not_available", "A successful Build Run with an artifact is required")
    workspace = Path(run["workspace"]).resolve()
    artifact = Path(artifact_data["path"]).resolve()
    if not artifact.is_file():
        raise ValidationError("artifact_not_available", "Build Run artifact no longer exists")
    validation_id = utc_now().replace(":", "").replace("+", "-").replace(".", "-") + "-" + secrets.token_hex(2)
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
    factory = backend_factory or (lambda **kwargs: OciSystemdBackend(**kwargs))
    backend = factory(workspace=workspace, image=selected_profile["image"], on_result=command_completed)
    started = time.monotonic()
    result = {
        "id": validation_id, "build_run_id": run_id, "artifact": str(artifact), "previous_artifact": "",
        "status": "running", "started_at": utc_now(), "finished_at": None, "duration": None,
        "backend": {}, "profile": selected_profile, "checks": [], "commands": commands, "error": None,
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
        configured_pools = tuple(Path(os.path.abspath(root)) for root in allowed_previous_roots)
        pool_root = next((root for root in configured_pools if previous.is_relative_to(root)), None)
        if not in_build_store and pool_root is None:
            raise ValidationError("previous_artifact_outside_build_store", "Previous artifact must belong to a DebBuilder Build Run or the configured repository pool")
        if in_build_store:
            previous = resolved_previous
        if previous.suffix != ".deb" or (in_build_store and previous.parent.name != "artifacts"):
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
    recipe = snapshot_recipe
    package = recipe["package"]["name"]
    configs = artifact_data.get("inspection", {}).get("conffiles", []) if upstream_mode else _config_paths(run)
    config_policies = {row["destination"]: row["policy"] for row in recipe["install"]["config_files"]} if not upstream_mode else {}
    marker = f"debbuilder-validation-{validation_id}"
    installed = False
    artifact_validation_state = {
        "id": validation_id, "status": "running", "started_at": result["started_at"],
        "finished_at": None, "previous_artifact": result["previous_artifact"],
    }
    run.setdefault("validations", []).append(result)
    run["artifact"].setdefault("validations", []).append(artifact_validation_state)
    store.save(run)
    try:
        result["backend"] = backend.start(validation_id)
        result["backend"]["profile"] = selected_profile["name"]
        _execute(backend, ["dpkg-deb", "--info", container_artifact], checks, "debian_metadata")
        _execute(backend, ["dpkg-deb", "--contents", container_artifact], checks, "debian_contents")
        if previous_container:
            previous_install = _execute(backend, ["dpkg", "--force-confnew", "--install", previous_container], checks, "previous_version_install", timeout=300)
            installed = bool(previous_install.get("accepted"))
            if installed:
                for path in configs:
                    _execute(backend, ["sh", "-c", 'printf "\\n%s\\n" "$1" >> "$2"', "debbuilder-config-marker", marker, path], checks, f"configuration_modified:{path}")
        conffile_option = "--force-confold" if previous_container else "--force-confnew"
        install = _execute(backend, ["dpkg", conffile_option, "--install", container_artifact], checks, "package_install", timeout=300)
        installed = bool(install.get("accepted"))
        if installed:
            _runtime_checks(_runtime_dependency_specs(artifact_data, recipe), backend, checks, selected_profile)
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
            result["error"] = {"code": "validation_checks_failed", "message": "One or more installation validation checks failed", "details": {"failed_checks": [row["name"] for row in checks if row["status"] == "failed"]}}
    except BackendError as exc:
        result["status"] = "failed"
        result["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
    except ValidationError as exc:
        result["status"] = "failed"
        result["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
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
            result["error"] = {"code": exc.code, "message": str(exc), "details": exc.details}
        result["finished_at"] = utc_now()
        result["duration"] = round(time.monotonic() - started, 6)
        artifact_validation_state.update({
            "status": result["status"], "finished_at": result["finished_at"],
            "previous_artifact": result["previous_artifact"],
        })
        store.append_event(run, f"Artifact validation {validation_id}: {result['status']}", level="error" if result["status"] == "failed" else "info")
        store.save(run)
    return result
