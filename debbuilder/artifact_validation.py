"""Installation lifecycle validation for an already successful Build Run."""
from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path

from .build_models import utc_now
from .build_store import BuildStore
from .recipe_schema import recipe_for_storage
from .validation_backend import BackendError, OciSystemdBackend
from .validation_profiles import node_satisfies, resolve_profile


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
    ownership_ok = bool(rows) and all(row["user"] == owner["user"] and row["group"] == owner["group"] for row in rows)
    inventory = {"count": len(rows), "sample": rows[:100], "sample_truncated": len(rows) > 100}
    _check(checks, f"{name}_ownership", ownership_ok, details={"expected": {"user": owner["user"], "group": owner["group"]}, **inventory})
    directory_mode = recipe["install"]["directory_mode"].lstrip("0") or "0"
    file_mode = recipe["install"]["file_mode"].lstrip("0") or "0"
    permissions_ok = bool(rows) and all(
        True if row["type"] == "l"
        else row["mode"] == directory_mode if row["type"] == "d"
        else (int(row["mode"], 8) & ~0o111) == int(file_mode, 8) and (int(row["mode"], 8) & 0o111) in {0, 0o111}
        for row in rows
    )
    _check(checks, f"{name}_permissions", permissions_ok, details={"directories": directory_mode, "regular_files_base": file_mode, "symbolic_links": "excluded (target permissions apply)", **inventory})
    return rows


def _config_paths(run: dict) -> list[str]:
    staging = next((step.get("details") or {} for step in run["steps"] if step.get("name") == "staging"), {})
    return [row["destination"] for row in staging.get("configurations", []) if row.get("destination")]


def validate_artifact(run_id: str, *, store: BuildStore, previous_artifact: str = "", backend_factory=None, profile: str = "bookworm", allowed_previous_roots: tuple[str | Path, ...] = ()) -> dict:
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
    snapshot_recipe = recipe_for_storage(json.loads((workspace / "recipe.json").read_text()))
    upstream_mode = snapshot_recipe.get("artifact", {}).get("mode") == "upstream_deb"
    expected_scripts = sorted(next((step.get("details", {}).get("maintainer_scripts", {}) for step in run["steps"] if step.get("name") == "debian_metadata"), {}))
    inspected_scripts = sorted(artifact_data.get("inspection", {}).get("maintainer_scripts", []))
    if upstream_mode:
        expected_scripts = inspected_scripts
    _check(checks, "maintainer_scripts_present", inspected_scripts == expected_scripts, details={"expected": expected_scripts, "inspected": inspected_scripts})
    container_artifact = _container_artifact(workspace, artifact)
    previous_container = ""
    if previous_artifact:
        previous = Path(previous_artifact).resolve()
        allowed_roots = (store.root.resolve(), *(Path(root).resolve() for root in allowed_previous_roots))
        try:
            next(root for root in allowed_roots if previous.is_relative_to(root))
        except StopIteration as exc:
            raise ValidationError("previous_artifact_outside_build_store", "Previous artifact must belong to a DebBuilder Build Run") from exc
        in_build_store = previous.is_relative_to(store.root.resolve())
        if not previous.is_file() or previous.suffix != ".deb" or (in_build_store and previous.parent.name != "artifacts"):
            raise ValidationError("previous_artifact_not_available", "Previous-version artifact is missing or is not a .deb")
        previous_copy = validation_dir / "previous.deb"
        shutil.copyfile(previous, previous_copy)
        previous_copy.chmod(0o400)
        previous_container = "/validation/" + previous_copy.relative_to(workspace).as_posix()
        result["previous_artifact"] = str(previous)
    recipe = snapshot_recipe
    package = recipe["package"]["name"]
    configs = artifact_data.get("inspection", {}).get("conffiles", []) if upstream_mode else _config_paths(run)
    marker = f"debbuilder-validation-{validation_id}"
    installed = False
    try:
        result["backend"] = backend.start(validation_id)
        result["backend"]["profile"] = selected_profile["name"]
        detection = next((step.get("details") or {} for step in run.get("steps", []) if step.get("name") == "detection"), {})
        node_requirement = str(detection.get("node_version") or "")
        if node_requirement:
            node = backend.exec(["node", "--version"], accepted_exit_codes={0})
            compatible = bool(node.get("accepted")) and node_satisfies(node.get("stdout", "").strip(), node_requirement)
            _check(checks, "toolchain_node", compatible, details={"required": node_requirement, "actual": node.get("stdout", "").strip(), "profile": selected_profile["name"]}, error=node.get("stderr") or "Node.js is missing or incompatible")
            if not compatible:
                raise ValidationError("validation_toolchain_incompatible", f"Node.js {node.get('stdout', '').strip() or 'missing'} does not satisfy {node_requirement}", details={"required": node_requirement, "actual": node.get("stdout", "").strip()})
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
            package_status = backend.exec(["dpkg-query", "--show", "--showformat=${Status}\\n", package], accepted_exit_codes={0})
            status_ok = bool(package_status.get("accepted")) and package_status.get("stdout", "").strip() == "install ok installed"
            _check(checks, "package_status_installed", status_ok, details={"status": package_status.get("stdout", "").strip()}, error=package_status.get("stderr") or "Package is not fully installed")
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
                    if recipe["install"]["config_policy"] == "replace":
                        _execute(backend, ["grep", "--fixed-strings", "--quiet", marker, path], checks, f"configuration_replaced:{path}", accepted={1})
                    else:
                        _execute(backend, ["grep", "--fixed-strings", "--quiet", marker, path], checks, f"configuration_preserved:{path}")
            service = recipe["service"]
            if upstream_mode and unit_paths:
                _execute(backend, ["systemctl", "daemon-reload"], checks, "systemd_daemon_reload")
                for unit_path in unit_paths:
                    _execute(backend, ["systemctl", "cat", Path(unit_path).name], checks, f"systemd_unit_present:{Path(unit_path).name}")
            elif service["enabled"]:
                _execute(backend, ["systemctl", "daemon-reload"], checks, "systemd_daemon_reload")
                _execute(backend, ["systemctl", "is-enabled", "--quiet", service["name"]], checks, "systemd_enabled")
                _execute(backend, ["systemctl", "is-active", "--quiet", service["name"]], checks, "systemd_active")
            remove = _execute(backend, ["dpkg", "--remove", package], checks, "package_remove", timeout=300)
            if remove.get("accepted"):
                if not upstream_mode and recipe["install"]["content"]["source"] != "configured_files":
                    _execute(backend, ["test", "!", "-e", recipe["install"]["destination"]], checks, "payload_absent_after_remove")
                if not upstream_mode and service["enabled"]:
                    inactive = backend.exec(["systemctl", "is-active", "--quiet", service["name"]], accepted_exit_codes={3, 4})
                    _check(checks, "systemd_inactive_after_remove", bool(inactive.get("accepted")), details={"exit_code": inactive.get("exit_code")})
                for path in configs:
                    expected = recipe["install"]["config_policy"] in {"dpkg_conffile", "create_if_missing"}
                    arguments = ["test", "-e", path] if expected else ["test", "!", "-e", path]
                    check = backend.exec(arguments, accepted_exit_codes={0})
                    _check(checks, f"configuration_after_remove:{path}", bool(check.get("accepted")), details={"expected_present": expected})
            purge = _execute(backend, ["dpkg", "--purge", package], checks, "package_purge", timeout=300)
            if purge.get("accepted"):
                for path in configs:
                    _execute(backend, ["test", "!", "-e", path], checks, f"configuration_absent_after_purge:{path}")
                if not upstream_mode and service["enabled"]:
                    _execute(backend, ["test", "!", "-e", f"/usr/lib/systemd/system/{service['name']}"], checks, "systemd_unit_absent_after_purge")
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
        run.setdefault("validations", []).append(result)
        run["artifact"].setdefault("validations", []).append({
            "id": validation_id, "status": result["status"], "started_at": result["started_at"],
            "finished_at": result["finished_at"], "previous_artifact": result["previous_artifact"],
        })
        store.append_event(run, f"Artifact validation {validation_id}: {result['status']}", level="error" if result["status"] == "failed" else "info")
        store.save(run)
    return result
