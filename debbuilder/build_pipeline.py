"""Build Run orchestration boundary.

Phase 2 establishes durable runs and workspaces. Pipeline stages are attached
in later phases and remain pending until they are genuinely executed.
"""
from __future__ import annotations

import time

from .build_models import utc_now
from .build_store import BuildStore
from . import build_executor, deb_inspector, debian_packaging, dependency_checker, project_detection, source_acquisition, source_changes, upstream_archive, upstream_artifact
from .recipe_schema import validate_recipe_metadata


def prepare_run(recipe: dict, *, store: BuildStore, dry_run: bool, recipe_id: str = "") -> dict:
    canonical = validate_recipe_metadata(recipe)
    run = store.create(canonical, recipe_id=recipe_id or canonical["name"], mode="dry_run" if dry_run else "build")
    started = time.monotonic()
    run["status"] = "running"
    run["started_at"] = utc_now()
    store.save(run)
    store.append_event(run, "Recipe snapshot created and isolated workspace prepared.")
    if dry_run:
        run["status"] = "prepared"
        store.append_event(run, "Pipeline stages are pending; source acquisition is connected in Phase 3.")
    else:
        run["status"] = "failed"
        run["error"] = "Build execution is unavailable until the Source stage is connected."
        store.append_event(run, run["error"], level="error")
    run["finished_at"] = utc_now()
    run["duration"] = round(time.monotonic() - started, 6)
    store.save(run)
    log = store.log_text(run["id"])
    return {
        "run_id": run["id"],
        "status": run["status"],
        "returncode": 0 if run["status"] == "prepared" else 1,
        "version": "",
        "stdout": log,
        "stderr": run.get("error") or "",
        "script": "",
        "workspace": run["workspace"],
        "steps": run["steps"],
        "publication": None,
    }


def _step(run: dict, name: str) -> dict:
    return next(step for step in run["steps"] if step["name"] == name)


def _start_step(run: dict, store: BuildStore, name: str) -> tuple[dict, float]:
    step = _step(run, name)
    step["status"] = "running"
    step["started_at"] = utc_now()
    store.append_event(run, f"{name}: running")
    return step, time.monotonic()


def _finish_step(run: dict, store: BuildStore, step: dict, started: float, *, status: str, summary: str, details: dict | None = None, error: dict | None = None) -> None:
    step.update({"status": status, "finished_at": utc_now(), "duration": round(time.monotonic() - started, 6), "summary": summary, "details": details or {}, "error": error})
    store.append_event(run, f"{step['name']}: {status} — {summary}", level="error" if status == "failed" else "info")


def _response(run: dict, store: BuildStore) -> dict:
    error = run.get("error") or {}
    return {
        "run_id": run["id"], "status": run["status"], "returncode": 1 if run["status"] == "failed" else 0,
        "version": (run.get("version") or {}).get("debian", ""), "versions": run.get("version"),
        "stdout": store.log_text(run["id"]), "stderr": error.get("message", "") if isinstance(error, dict) else str(error),
        "error": error or None, "script": "", "workspace": run["workspace"], "steps": run["steps"],
        "source": _step(run, "source").get("details") or None,
        "detection": _step(run, "detection").get("details") or None,
        "dependencies": _step(run, "dependencies").get("details") or None,
        "source_changes": _step(run, "source_changes").get("details") or None,
        "build": _step(run, "build").get("details") or None,
        "staging": _step(run, "staging").get("details") or None,
        "artifact": run.get("artifact"),
        "publication": None,
    }


def _skip_step(run: dict, store: BuildStore, name: str, reason: str = "upstream_artifact") -> None:
    step, started = _start_step(run, store, name)
    _finish_step(run, store, step, started, status="skipped", summary=f"Not applicable: {reason}", details={"reason": reason})


def _notify_lifecycle(callback, event: str, **payload) -> None:
    if not callable(callback):
        return
    try:
        callback(event, **payload)
    except Exception:
        pass


def _finish_run(run: dict, store: BuildStore, started: float, lifecycle_callback=None, recipe: dict | None = None) -> dict:
    run.update({"finished_at": utc_now(), "duration": round(time.monotonic() - started, 6)})
    store.save(run)
    if run.get("mode") == "build":
        event = "build_failed" if run.get("status") == "failed" else "build_succeeded" if run.get("status") == "success" else ""
        if event:
            _notify_lifecycle(lifecycle_callback, event, run=run, recipe=recipe or {})
    return _response(run, store)


def _run_upstream_artifact(canonical: dict, run: dict, *, store: BuildStore, dry_run: bool, github_token: str, acquirer=None, lifecycle_callback=None) -> dict:
    started = time.monotonic()
    source_step, source_started = _start_step(run, store, "source")
    try:
        release = upstream_artifact.resolve_release(canonical, token=github_token)
        source_details = {
            "repository": release["repository"], "ref": release["ref"], "tag": release["tag"],
            "release_url": release.get("url", ""), "upstream_version": release["upstream_version"],
            "assets": release.get("assets", []), "artifact_mode": "upstream_deb",
        }
        _finish_step(run, store, source_step, source_started, status="success", summary=f"Resolved {release['repository']} {release['tag']}", details=source_details)
        selection_step, selection_started = _start_step(run, store, "detection")
        selected = upstream_artifact.select_asset(release, canonical["artifact"])
        _finish_step(run, store, selection_step, selection_started, status="success", summary=f"Selected {selected['name']}", details={"project_type": "upstream_deb", "selected_asset": selected})
        for name in ("dependencies", "source_changes", "build", "staging", "debian_metadata", "systemd", "package"):
            _skip_step(run, store, name)
        artifact_step, artifact_started = _start_step(run, store, "artifact")
        if dry_run:
            _finish_step(run, store, artifact_step, artifact_started, status="skipped", summary="Dry-run: upstream artifact not downloaded", details={"reason": "dry_run", "selected_asset": selected})
            run.update({"status": "prepared", "version": {"upstream": release["upstream_version"], "debian": ""}})
        else:
            artifact = (acquirer or upstream_artifact.acquire)(canonical, run["workspace"], token=github_token)
            # Re-resolution inside the acquisition is intentional: the selected asset and
            # release are validated immediately before downloading.
            info = artifact["inspection"]
            stored_artifact = store.artifact_details_for_storage(run, artifact)
            run.update({"artifact": stored_artifact, "version": {"upstream": release["upstream_version"], "debian": info["version"]}, "status": "success"})
            _finish_step(run, store, artifact_step, artifact_started, status="success", summary=f"Registered upstream {artifact['name']} · SHA-256 {artifact['sha256']}", details=stored_artifact)
    except upstream_artifact.UpstreamArtifactError as exc:
        active = next((step for step in run["steps"] if step["status"] == "running"), source_step)
        error = {"stage": active["name"], "code": exc.code, "message": str(exc), "details": exc.details}
        _finish_step(run, store, active, source_started if active is source_step else time.monotonic(), status="failed", summary=str(exc), details=exc.details, error=error)
        run.update({"status": "failed", "error": error})
    return _finish_run(run, store, started, lifecycle_callback, canonical)


def run_pipeline(recipe: dict, *, store: BuildStore, dry_run: bool, recipe_id: str = "", github_token: str = "", acquire=None, detector=None, dependency_check=None, change_applier=None, upstream_acquirer=None, lifecycle_callback=None) -> dict:
    """Execute the connected pipeline through Dependencies and Source changes."""
    canonical = validate_recipe_metadata(recipe)
    run = store.create(canonical, recipe_id=recipe_id or canonical["name"], mode="dry_run" if dry_run else "build")
    run_started = time.monotonic()
    run.update({"status": "running", "started_at": utc_now()})
    store.save(run)
    store.append_event(run, "Recipe snapshot created and isolated workspace prepared.")
    if run.get("mode") == "build":
        _notify_lifecycle(lifecycle_callback, "build_started", run=run, recipe=canonical)
    if canonical["artifact"]["mode"] == "upstream_deb":
        return _run_upstream_artifact(canonical, run, store=store, dry_run=dry_run, github_token=github_token, acquirer=upstream_acquirer, lifecycle_callback=lifecycle_callback)
    archive_mode = canonical["artifact"]["mode"] == "upstream_archive"
    acquire = acquire or (upstream_archive.acquire if archive_mode else source_acquisition.acquire_source)
    detector = detector or project_detection.detect_project
    dependency_check = dependency_check or dependency_checker.check_dependencies
    change_applier = change_applier or source_changes.apply_changes
    source_step, source_started = _start_step(run, store, "source")
    try:
        source = acquire(canonical, run["workspace"], token=github_token)
        run["version"] = {"upstream": source["upstream_version"], "debian": source["debian_version"]}
        summary = f"{source['repository']} {source['ref'] or source['tag']} → Debian {source['debian_version']}"
        _finish_step(run, store, source_step, source_started, status="success", summary=summary, details=source)
    except (source_acquisition.SourceError, upstream_archive.UpstreamArchiveError) as exc:
        error = {"stage": "source", "code": exc.code, "message": str(exc)}
        _finish_step(run, store, source_step, source_started, status="failed", summary=str(exc), error=error)
        run.update({"status": "failed", "error": error})
    if run["status"] != "failed":
        detection_step, detection_started = _start_step(run, store, "detection")
        try:
            if archive_mode:
                detection = {
                    "project_type": "upstream_archive", "display_name": "Upstream release artifact · no source build",
                    "detected_files": [row["relative_path"] for row in source["selected_files"]], "build_dependencies": [],
                    "system_build_dependencies": [], "build_tools": [], "tool_version_requirements": {},
                    "proposed_commands": [], "warnings": [], "selected_asset": source["asset"], "selected_files": source["selected_files"],
                }
            else:
                detection = detector(source["source_directory"], working_directory=canonical["build"]["working_directory"])
            summary = f"{detection['display_name']} from {', '.join(detection['detected_files'])}"
            _finish_step(run, store, detection_step, detection_started, status="success", summary=summary, details=detection)
        except project_detection.DetectionError as exc:
            error = {"stage": "detection", "code": exc.code, "message": str(exc), "details": exc.details}
            _finish_step(run, store, detection_step, detection_started, status="failed", summary=str(exc), details=exc.details, error=error)
            run.update({"status": "failed", "error": error})
    if run["status"] != "failed":
        dependencies_step, dependencies_started = _start_step(run, store, "dependencies")
        if archive_mode:
            dependencies = {"detected": [], "manually_added": [], "requested": [], "available": [], "missing": []}
            _finish_step(run, store, dependencies_step, dependencies_started, status="skipped", summary="No build dependencies: upstream release artifact", details={**dependencies, "reason": "upstream_archive"})
        else:
            try:
                dependencies = dependency_check(
                    detection.get("system_build_dependencies", detection.get("build_dependencies", [])),
                    canonical["build"]["extra_dependencies"], tools=detection.get("build_tools", []),
                    tool_version_requirements=detection.get("tool_version_requirements", {}),
                    workspace=source["source_directory"], working_directory=canonical["build"]["working_directory"],
                    environment=canonical["build"]["environment"],
                )
                store.append_event(run, f"Build tools available: {', '.join(dependencies.get('available_tools', [])) or 'none'}")
                store.append_event(run, f"Build tools unavailable: {', '.join(dependencies.get('missing_tools', [])) or 'none'}")
                store.append_event(run, f"Dependencies detected: {', '.join(dependencies['detected']) or 'none'}")
                store.append_event(run, f"Dependencies manually added: {', '.join(dependencies['manually_added']) or 'none'}")
                store.append_event(run, f"Dependencies available: {', '.join(dependencies['available']) or 'none'}")
                store.append_event(run, f"Dependencies missing: {', '.join(dependencies['missing']) or 'none'}")
                summary = f"{len(dependencies.get('available_tools', []))} tools available; {len(dependencies['available'])} system dependencies installed, {len(dependencies['missing'])} missing"
                _finish_step(run, store, dependencies_step, dependencies_started, status="success", summary=summary, details=dependencies)
            except dependency_checker.DependencyError as exc:
                state = exc.details
                store.append_event(run, f"Build tools available: {', '.join(state.get('available_tools', [])) or 'none'}")
                store.append_event(run, f"Build tools unavailable: {', '.join(state.get('missing_tools', [])) or 'none'}", level="error" if state.get("missing_tools") else "info")
                store.append_event(run, f"Dependencies detected: {', '.join(state.get('detected', [])) or 'none'}")
                store.append_event(run, f"Dependencies manually added: {', '.join(state.get('manually_added', [])) or 'none'}")
                store.append_event(run, f"Dependencies available: {', '.join(state.get('available', [])) or 'none'}")
                store.append_event(run, f"Dependencies missing: {', '.join(state.get('missing', [])) or 'none'}", level="error")
                error = {"stage": "dependencies", "code": exc.code, "message": str(exc), "details": exc.details}
                _finish_step(run, store, dependencies_step, dependencies_started, status="failed", summary=str(exc), details=exc.details, error=error)
                run.update({"status": "failed", "error": error})
    if run["status"] != "failed":
        changes_step, changes_started = _start_step(run, store, "source_changes")
        if archive_mode:
            _finish_step(run, store, changes_step, changes_started, status="skipped", summary="No source changes: upstream release artifact", details={"requested": 0, "applied_count": 0, "applied": [], "reason": "upstream_archive"})
        else:
            try:
                changes = change_applier(
                    source["source_directory"], canonical["build"]["source_changes"],
                    on_applied=lambda item: store.append_event(run, f"Source change {item['index']}/{len(canonical['build']['source_changes'])}: {item['path']} {item['operation']} applied"),
                )
                summary = f"{changes['applied_count']}/{changes['requested']} source changes applied"
                _finish_step(run, store, changes_step, changes_started, status="success", summary=summary, details=changes)
            except source_changes.SourceChangeError as exc:
                error = {"stage": "source_changes", "code": exc.code, "message": str(exc), "change_index": exc.index, "details": exc.details}
                details = {"requested": len(canonical["build"]["source_changes"]), "failed_index": exc.index, **exc.details}
                _finish_step(run, store, changes_step, changes_started, status="failed", summary=str(exc), details=details, error=error)
                run.update({"status": "failed", "error": error})
    if run["status"] != "failed":
        build_step, build_started = _start_step(run, store, "build")
        if archive_mode:
            selected_paths = [{"path": row["path"]} for row in source["selected_files"]]
            output = {"mode": "paths", "paths": selected_paths} if len(selected_paths) > 1 else {"mode": "path", **selected_paths[0]}
            build = {"executed": False, "reason": "upstream_archive", "plan": {"commands": [], "working_directory": ".", "environment": {}, "output": output}, "commands": [], "output": output}
            _finish_step(run, store, build_step, build_started, status="skipped", summary="Upstream release artifact · no source build", details=build)
        else:
            try:
                def command_completed(result):
                    build_step.setdefault("details", {}).setdefault("commands", []).append(result)
                    store.save_command_result(run["id"], result)
                    store.append_event(run, f"Build command {result['index']}: {result['status']} (exit {result.get('exit_code')}, {result['duration']}s)")
                    if result.get("timed_out") and result.get("stderr"):
                        for line in str(result["stderr"]).splitlines():
                            store.append_log_line(run["id"], f"Build command {result['index']} timeout: {line}", level="error")
                def command_output(index, item):
                    for line in str(item.get("text") or "").splitlines():
                        store.append_log_line(run["id"], f"Build command {index} {item.get('stream', 'output')}: {line}")
                build = build_executor.execute_build(
                    canonical, detection, source["source_directory"], dry_run=dry_run,
                    on_result=command_completed, on_output=command_output,
                )
                if dry_run:
                    _finish_step(run, store, build_step, build_started, status="skipped", summary=f"Dry-run validated {len(build['plan']['commands'])} commands; none executed", details=build)
                else:
                    _finish_step(run, store, build_step, build_started, status="success", summary=f"{len(build['commands'])} build commands completed", details=build)
            except build_executor.BuildError as exc:
                error = {"stage": "build", "code": exc.code, "message": str(exc), "details": exc.details}
                _finish_step(run, store, build_step, build_started, status="failed", summary=str(exc), details=exc.details, error=error)
                run.update({"status": "failed", "error": error})
    if run["status"] != "failed":
        staging_step, staging_started = _start_step(run, store, "staging")
        try:
            staging = debian_packaging.prepare_staging(canonical, {**build, "version": run["version"]["debian"]}, run["workspace"], preview=dry_run)
            validation = debian_packaging.validate_staging(staging)
            staging["validation"] = validation
            content_file_count = len(staging["content_files"])
            stored_staging = store.staging_details_for_storage(run, staging)
            _finish_step(run, store, staging_step, staging_started, status="success", summary=f"Staging prepared with {content_file_count:,} application files", details=stored_staging)
            metadata_step, metadata_started = _start_step(run, store, "debian_metadata")
            _finish_step(run, store, metadata_step, metadata_started, status="success", summary="DEBIAN/control and package scripts generated", details={"control":staging["control"],"conffiles":staging["conffiles"],"configurations":staging["configurations"],"maintainer_scripts":staging["maintainer_scripts"]})
            systemd_step, systemd_started = _start_step(run, store, "systemd")
            if staging["systemd"]["configured"]:
                _finish_step(run, store, systemd_step, systemd_started, status="success", summary=f"Generated {staging['systemd']['path']}", details=staging["systemd"])
            else:
                _finish_step(run, store, systemd_step, systemd_started, status="skipped", summary="No systemd service configured", details=staging["systemd"])
        except debian_packaging.PackagingError as exc:
            stage = "debian_metadata" if exc.code == "invalid_debian_metadata" else "systemd" if exc.code == "invalid_systemd_service" else "staging"
            failed_step = _step(run, stage)
            if failed_step["status"] == "pending":
                failed_step["status"] = "running"
                failed_step["started_at"] = utc_now()
            error = {"stage":stage,"code":exc.code,"message":str(exc),"details":exc.details}
            _finish_step(run, store, failed_step, time.monotonic(), status="failed", summary=str(exc), details=exc.details, error=error)
            if failed_step is not staging_step and staging_step["status"] == "running":
                _finish_step(run, store, staging_step, staging_started, status="failed", summary="Staging could not be completed", error=error)
            run.update({"status":"failed","error":error})
    if run["status"] != "failed":
        package_step, package_started = _start_step(run, store, "package")
        if dry_run:
            _finish_step(run, store, package_step, package_started, status="skipped", summary="Dry-run: dpkg-deb --build not executed", details={"validated_staging":True})
            artifact_step, artifact_started = _start_step(run, store, "artifact")
            _finish_step(run, store, artifact_step, artifact_started, status="skipped", summary="Dry-run: no artifact created", details={})
            run["status"] = "prepared"
        else:
            try:
                artifact = debian_packaging.build_deb(canonical, staging, run["workspace"], inspector=deb_inspector.inspect_deb)
                stored_artifact = store.artifact_details_for_storage(run, artifact)
                _finish_step(run, store, package_step, package_started, status="success", summary=f"Built {artifact['name']}", details={"command":artifact["build_command"]})
                artifact_step, artifact_started = _start_step(run, store, "artifact")
                _finish_step(run, store, artifact_step, artifact_started, status="success", summary=f"{artifact['size']} bytes · SHA-256 {artifact['sha256']}", details=stored_artifact)
                run["artifact"] = stored_artifact
                run["status"] = "success"
            except debian_packaging.PackagingError as exc:
                stage = "artifact" if exc.code == "deb_inspection_failed" else "package"
                error = {"stage":stage,"code":exc.code,"message":str(exc),"details":exc.details}
                if stage == "artifact":
                    artifact_data = exc.details.get("artifact", {})
                    _finish_step(run, store, package_step, package_started, status="success", summary=f"Built {artifact_data.get('name', 'Debian package')}", details={"command":artifact_data.get("build_command")})
                    artifact_step, artifact_started = _start_step(run, store, "artifact")
                    _finish_step(run, store, artifact_step, artifact_started, status="failed", summary=str(exc), details=exc.details, error=error)
                else:
                    _finish_step(run, store, package_step, package_started, status="failed", summary=str(exc), details=exc.details, error=error)
                run.update({"status":"failed","error":error})
    return _finish_run(run, store, run_started, lifecycle_callback, canonical)


# Compatibility name retained for Phase 3 callers and tests.
run_source_detection = run_pipeline


def execution_summary(run: dict) -> dict:
    validations = run.get("validations") or []
    publications = run.get("publications") or []
    validation_status = validations[-1]["status"] if validations else "not_run"
    publication_status = publications[-1]["status"] if publications else "not_run"
    from .package_store import derive_lifecycle_status
    return {
        "id": run["id"], "package": run.get("recipe_id", ""),
        "action": "dry-run" if run.get("mode") == "dry_run" else "build",
        "version": (run.get("version") or {}).get("debian", ""),
        "status": run.get("status", "pending"), "build_status": run.get("status", "pending"), "updated": run.get("created_at_epoch"),
        "duration": run.get("duration"), "workspace": run.get("workspace", ""),
        "validation_count": len(validations), "validation_status": validation_status, "publication_status": publication_status,
        "lifecycle_status": derive_lifecycle_status(run.get("status", "pending"), validation_status, publication_status),
    }


def execution_detail(run: dict, store: BuildStore) -> dict:
    detail = {**execution_summary(run), **run}
    detail["log"] = store.log_text(run["id"])
    detail["script"] = ""
    return detail
