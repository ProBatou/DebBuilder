"""Explicit reprepro publication for validated Build Run artifacts."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import apt_repo, deb_inspector
from .build_models import utc_now
from .build_store import BuildStore


class PublicationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def publication_readiness(run: dict) -> dict:
    artifact = run.get("artifact") or {}
    validations = [row for row in run.get("validations", []) if row.get("artifact") == artifact.get("path")]
    successful = [row for row in validations if row.get("status") == "success"]
    reasons = []
    if run.get("status") != "success":
        reasons.append("build_not_successful")
    if not artifact.get("path") or not Path(artifact["path"]).is_file():
        reasons.append("artifact_unavailable")
    if not successful:
        reasons.append("validation_not_successful")
    return {"ready": not reasons, "reasons": reasons, "validation_id": successful[-1]["id"] if successful else ""}


def publish_artifact(run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str, component: str, confirm: str, runner=None) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise PublicationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id):
        return _publish_artifact_locked(
            run_id,
            store=store,
            repo_root=repo_root,
            distribution=distribution,
            component=component,
            confirm=confirm,
            runner=runner,
        )


def _publish_artifact_locked(run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str, component: str, confirm: str, runner=None) -> dict:
    run = store.load(run_id)
    if not run:
        raise PublicationError("build_run_not_found", "Build Run was not found")
    artifact = run.get("artifact") or {}
    info = artifact.get("inspection") or {}
    package, version = info.get("package", ""), info.get("version", "")
    expected = f"publish:{package}:{version}"
    root = Path(repo_root).resolve()
    started = time.monotonic()
    attempt = {
        "id": utc_now().replace(":", "").replace("+", "-").replace(".", "-"),
        "build_run_id": run_id, "artifact": artifact.get("path", ""), "package": package,
        "version": version, "architecture": info.get("architecture", ""), "status": "running",
        "requested_at": utc_now(), "finished_at": None, "duration": None,
        "repository": {"root": str(root), "distribution": distribution, "component": component},
        "readiness": publication_readiness(run), "preflight": {}, "command": None,
        "published_version": "", "error": None,
    }
    artifact_publication_state = {
        key: attempt[key] for key in ("id", "status", "requested_at", "finished_at", "published_version")
    }
    run.setdefault("publications", []).append(attempt)
    artifact.setdefault("publications", []).append(artifact_publication_state)
    store.save(run)
    try:
        if confirm != expected:
            raise PublicationError("publication_confirmation_required", f"Publication requires explicit confirmation: {expected}")
        if not attempt["readiness"]["ready"]:
            raise PublicationError("artifact_not_ready", "Artifact requires a successful Build and Validation", details=attempt["readiness"])
        if apt_repo.detect_repo_backend(root) != "reprepro":
            raise PublicationError("unsupported_repository", "Configured repository is not managed by reprepro")
        config = apt_repo.reprepro_config(root)
        attempt["preflight"]["config"] = config
        codename = config.get("codename") or distribution
        if distribution not in {codename, config.get("suite")}:
            raise PublicationError("distribution_mismatch", f"Distribution {distribution!r} does not match reprepro codename or suite")
        if component not in config.get("components", []):
            raise PublicationError("component_not_configured", f"Component {component!r} is not configured in reprepro")
        architecture = info.get("architecture", "")
        if architecture == "all":
            if not config.get("architectures"):
                raise PublicationError("architecture_not_configured", "Architecture all requires at least one configured binary architecture")
            architecture_policy = "all accepted for configured binary architectures; conf/distributions unchanged"
        elif architecture not in config.get("architectures", []):
            raise PublicationError("architecture_not_configured", f"Architecture {architecture!r} is not configured in reprepro")
        else:
            architecture_policy = "explicitly configured"
        attempt["preflight"]["architecture_policy"] = architecture_policy
        inspected = deb_inspector.inspect_deb(artifact["path"])
        if not inspected.get("ok") or any(inspected.get(key) != info.get(key) for key in ("package", "version", "architecture")):
            raise PublicationError("artifact_inspection_failed", "Artifact metadata no longer matches the successful Build inspection", details={"inspection": inspected})
        before = apt_repo.reprepro_list(root, codename, runner=runner) if runner else apt_repo.reprepro_list(root, codename)
        if before["command"].get("status") != "success":
            raise PublicationError("repository_query_failed", before["command"].get("stderr") or "Unable to query reprepro")
        attempt["preflight"]["repository_before"] = before["packages"]
        exported = apt_repo.local_packages_index(root, codename, component, config["architectures"][0])
        if any(row["package"] == package and row["version"] == version for row in before["packages"]):
            visible = any(row.get("Package") == package and row.get("Version") == version for row in exported)
            if not visible:
                raise PublicationError("publication_export_incomplete", f"{package} {version} is present in reprepro but absent from the exported Packages index")
            attempt["command"] = {"status": "success", "command": "repository verification only", "arguments": [], "working_directory": str(root), "exit_code": 0, "stdout": "Version already imported and exported", "stderr": "", "duration": 0, "timed_out": False}
            attempt["repository"]["packages_after"] = before["packages"]
            attempt.update({"status": "success", "published_version": version})
            return attempt
        current = next((row.get("Version", "") for row in exported if row.get("Package") == package), "")
        if current:
            comparison = apt_repo.debian_version_relation(version, current, workspace=root, **({"runner": runner} if runner else {}))
            attempt["preflight"]["version_comparison"] = {"candidate": version, "published": current, "relation": comparison["relation"], "command": comparison["command"]}
            if comparison["relation"] == "older":
                raise PublicationError("downgrade_refused", f"Candidate {version} is older than published version {current}", details=attempt["preflight"]["version_comparison"])
        include = apt_repo.reprepro_include_deb(root, codename, Path(artifact["path"]), component, **({"runner": runner} if runner else {}))
        attempt["command"] = include["command"]
        if include["command"].get("status") != "success":
            raise PublicationError("reprepro_include_failed", include["command"].get("stderr") or "reprepro includedeb failed", details={"command": include["command"]})
        after = apt_repo.reprepro_list(root, codename, runner=runner) if runner else apt_repo.reprepro_list(root, codename)
        if after["command"].get("status") != "success":
            raise PublicationError("repository_query_failed", after["command"].get("stderr") or "Unable to verify reprepro")
        attempt["repository"]["packages_after"] = after["packages"]
        published = next((row for row in after["packages"] if row["package"] == package and row["version"] == version), None)
        if not published:
            raise PublicationError("publication_verification_failed", "reprepro did not report the requested package version after publication")
        attempt.update({"status": "success", "published_version": version})
    except PublicationError as exc:
        attempt.update({"status": "failed", "error": {"code": exc.code, "message": str(exc), "details": exc.details}})
    except Exception as exc:
        attempt.update({"status": "failed", "error": {"code": "publication_execution_failed", "message": str(exc), "details": {}}})
    finally:
        attempt["finished_at"] = utc_now()
        attempt["duration"] = round(time.monotonic() - started, 6)
        artifact_publication_state.update({
            key: attempt[key] for key in ("status", "finished_at", "published_version")
        })
        store.append_event(run, f"Artifact publication {attempt['id']}: {attempt['status']}", level="error" if attempt["status"] == "failed" else "info")
        store.save(run)
    return attempt


def reconcile_publication(run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str, component: str, runner=None) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise PublicationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id):
        return _reconcile_publication_locked(
            run_id,
            store=store,
            repo_root=repo_root,
            distribution=distribution,
            component=component,
            runner=runner,
        )


def _reconcile_publication_locked(run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str, component: str, runner=None) -> dict:
    """Record external publication truth without importing or rewriting an old attempt."""
    run = store.load(run_id)
    if not run:
        raise PublicationError("build_run_not_found", "Build Run was not found")
    artifact = run.get("artifact") or {}
    info = artifact.get("inspection") or {}
    package, version, architecture = (info.get(key, "") for key in ("package", "version", "architecture"))
    root = Path(repo_root).resolve()
    started = time.monotonic()
    attempt = {
        "id": utc_now().replace(":", "").replace("+", "-").replace(".", "-"), "type": "publication_reconciled",
        "build_run_id": run_id, "artifact": artifact.get("path", ""), "artifact_sha256": artifact.get("sha256", ""),
        "package": package, "version": version, "architecture": architecture, "status": "running",
        "requested_at": utc_now(), "finished_at": None, "duration": None,
        "repository": {"root": str(root), "distribution": distribution, "component": component},
        "preflight": {}, "command": None, "published_version": "", "error": None,
    }
    artifact_publication_state = {
        key: attempt[key] for key in ("id", "status", "requested_at", "finished_at", "published_version")
    }
    run.setdefault("publications", []).append(attempt)
    artifact.setdefault("publications", []).append(artifact_publication_state)
    store.save(run)
    try:
        path = Path(artifact.get("path", ""))
        if run.get("status") != "success" or not path.is_file():
            raise PublicationError("artifact_not_available", "A successful Build Run with an artifact is required")
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        attempt["preflight"]["artifact_sha256"] = {"recorded": artifact.get("sha256", ""), "actual": actual_sha}
        inspected = deb_inspector.inspect_deb(path)
        attempt["preflight"]["artifact_inspection"] = {key: inspected.get(key) for key in ("ok", "package", "version", "architecture", "size")}
        if actual_sha != artifact.get("sha256") or not inspected.get("ok") or any(inspected.get(key) != info.get(key) for key in ("package", "version", "architecture")):
            raise PublicationError("artifact_identity_mismatch", "Artifact SHA or package metadata no longer matches the Build Run")
        config = apt_repo.reprepro_config(root)
        codename = config.get("codename") or distribution
        database = apt_repo.reprepro_list(root, codename, **({"runner": runner} if runner else {}))
        db_match = any(row["package"] == package and row["version"] == version for row in database["packages"])
        index_rows = apt_repo.local_packages_index(root, codename, component, config["architectures"][0])
        index_match = any(row.get("Package") == package and row.get("Version") == version for row in index_rows)
        attempt["preflight"].update({"reprepro_database_match": db_match, "apt_index_match": index_match})
        if database["command"].get("status") != "success" or not db_match or not index_match:
            raise PublicationError("publication_not_exported", "Candidate must exist in both the reprepro database and exported APT index", details={"database": db_match, "index": index_match})
        attempt["command"] = {"status": "success", "command": "repository reconciliation only", "arguments": [], "working_directory": str(root), "exit_code": 0, "stdout": "Database and exported index agree", "stderr": "", "duration": 0, "timed_out": False}
        attempt.update({"status": "success", "published_version": version})
    except PublicationError as exc:
        attempt.update({"status": "failed", "error": {"code": exc.code, "message": str(exc), "details": exc.details}})
    except Exception as exc:
        attempt.update({"status": "failed", "error": {"code": "publication_reconciliation_failed", "message": str(exc), "details": {}}})
    finally:
        attempt["finished_at"] = utc_now()
        attempt["duration"] = round(time.monotonic() - started, 6)
        artifact_publication_state.update({
            key: attempt[key] for key in ("status", "finished_at", "published_version")
        })
        store.append_event(run, f"Publication reconciliation {attempt['id']}: {attempt['status']}", level="error" if attempt["status"] == "failed" else "info")
        store.save(run)
    return attempt
