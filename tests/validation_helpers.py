from datetime import datetime, timedelta
from pathlib import Path

from debbuilder import storage
from debbuilder.build_models import utc_now
from debbuilder.dependency_preparation import inspect_artifact
from debbuilder import dependency_preparation, validation_service
from debbuilder.validation_contracts import (
    PREPARED_DEPENDENCIES_CONTRACT_VERSION,
    VALIDATION_RESULT_CONTRACT_VERSION,
)
from debbuilder.validation_profiles import resolve_profile


def prepare_admitted_for_test(
    run_id,
    *,
    store,
    current_artifact,
    registry_root,
    previous_artifact=None,
    profile_name="bookworm",
    repositories=None,
    test_ca_certificate=None,
):
    """Exercise preparation with an attempt created by ValidationManager."""
    run = store.load(run_id)
    current = inspect_artifact(current_artifact, workspace=Path(run["workspace"]))
    run["status"] = "success"
    run["artifact"] = {
        **(run.get("artifact") or {}),
        "path": str(current_artifact), "name": Path(current_artifact).name,
        "inspection": {
            **((run.get("artifact") or {}).get("inspection") or {}),
            "package": current.package, "version": current.version,
            "architecture": current.architecture, "depends": current.depends,
        },
    }
    store.save(run)
    manager = validation_service.ValidationManager(
        store, execute=lambda *_args: None, registry_root=registry_root,
        workspace_root=Path(run["workspace"]).parent.parent,
    )
    with store.locked_run(run_id):
        admitted = manager._admit_locked(
            run_id, store.load(run_id), profile_name=profile_name,
            previous_artifact=str(previous_artifact or ""), automatic=False,
            publish_after_success=False,
        )
    snapshot = (
        Path(run["workspace"]) / "validation" / admitted["id"] / "preparation-previous.deb"
        if previous_artifact else None
    )
    return dependency_preparation.prepare_runtime_dependencies(
        run_id, admitted["id"], store=store, current_artifact=current_artifact,
        previous_artifact=snapshot, profile_name=profile_name,
        repositories=repositories, registry_root=registry_root,
        test_ca_certificate=test_ca_certificate,
    )


def canonical_validation_inputs(
    store,
    run: dict,
    *,
    profile: str = "bookworm",
    previous_artifact: str | Path = "",
) -> dict:
    """Create empty, canonical prepared evidence for lifecycle unit tests."""
    workspace = Path(run["workspace"])
    attempt_id = store.allocate_run_id()
    (workspace / "manifests" / "validation-attempts" / attempt_id / "packages").mkdir(
        parents=True,
        mode=0o700,
    )
    current = inspect_artifact(Path(run["artifact"]["path"]), workspace=workspace)
    previous = (
        inspect_artifact(Path(previous_artifact), workspace=workspace)
        if previous_artifact
        else None
    )
    selected = resolve_profile(profile)
    timestamp = utc_now()
    prepared = {
        "contract_version": PREPARED_DEPENDENCIES_CONTRACT_VERSION,
        "profile_name": profile,
        "image": {
            "name": selected["image"],
            "id": "sha256:" + "a" * 64,
            "digest": None,
        },
        "native_architecture": "amd64",
        "artifacts": {
            "current": current.identity(),
            "previous": previous.identity() if previous else None,
        },
        "repositories": [],
        "base_packages": [],
        "packages": [],
        "started_at": timestamp,
        "finished_at": timestamp,
        "diagnostics": [],
        "enforcement": [],
    }
    return {
        "prepared_dependencies": prepared,
        "attempt_id": attempt_id,
        "registry_root": workspace / "test-validation-registry",
    }


def record_canonical_validation(
    store,
    run: dict,
    *,
    attempt_id: str = "canonical-validation",
    status: str = "success",
    lifecycle: bool = True,
    artifact_identity: dict | None = None,
    created_at: str = "2026-09-14T08:00:00+00:00",
) -> dict:
    """Persist compact canonical Validation evidence for publication tests."""
    if artifact_identity is not None:
        identity = artifact_identity
    else:
        artifact = run["artifact"]
        inspection = artifact["inspection"]
        identity = {
            "package": inspection["package"],
            "version": inspection["version"],
            "architecture": inspection["architecture"],
            "size": artifact["size"],
            "sha256": artifact["sha256"],
        }
    image = {
        "name": resolve_profile("bookworm")["image"],
        "id": "sha256:" + "a" * 64,
        "digest": None,
    }
    created = datetime.fromisoformat(created_at)
    started = (created + timedelta(seconds=1)).isoformat()
    prepared_finished = (created + timedelta(seconds=2)).isoformat()
    lifecycle_started = prepared_finished
    finished = (created + timedelta(seconds=3)).isoformat()
    prepared = {
        "contract_version": PREPARED_DEPENDENCIES_CONTRACT_VERSION,
        "profile_name": "bookworm",
        "image": image,
        "native_architecture": identity["architecture"] if identity["architecture"] != "all" else "amd64",
        "artifacts": {"current": identity, "previous": None},
        "repositories": [],
        "base_packages": [],
        "packages": [],
        "started_at": started,
        "finished_at": prepared_finished,
        "diagnostics": [],
        "enforcement": [],
    }
    started_at = None if status == "queued" else started
    finished_at = (
        finished
        if status in {"success", "failed", "cancelled"} else None
    )
    if status in {"queued", "running", "cancelling"}:
        result = None
        error = None
    elif status == "cancelled" and not lifecycle:
        result = None
        error = {"code": "validation_lifecycle_cancelled", "message": "Validation was cancelled"}
    else:
        result = {
            "status": status,
            "reference": "result.json",
        }
        error = None if status == "success" else ({
            "code": "validation_lifecycle_cancelled", "message": "Validation was cancelled",
        } if status == "cancelled" else {
            "code": "validation_checks_failed", "message": "Validation failed",
        })
    attempt = {
        "contract_version": 1,
        "id": attempt_id,
        "build_run_id": run["id"],
        "inputs": {"profile": "bookworm", "artifact": identity, "previous_artifact": None},
        "selected_profile": {"name": "bookworm", "image": image},
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "result": result,
        "error": error,
    }
    root = store.run_dir(run["id"]) / "manifests/validation-attempts" / attempt_id
    root.mkdir(parents=True, exist_ok=True)
    storage.save_json(root / "automation.json", {
        "automatic": False,
        "publish_after_success": False,
        "publication_state": "not_requested",
    })
    if status != "queued":
        storage.save_json(root / "prepared.json", prepared)
    storage.save_json(root / "attempt.json", attempt)
    if lifecycle and status in {"success", "failed", "cancelled"}:
        checks = [{"name": name, "status": "success", "error": ""} for name in (
            "lifecycle_network_disabled", "package_install", "package_status_installed",
            "package_remove", "package_purge", "package_absent_after_purge",
        )]
        if status == "failed":
            checks[1].update({"status": "failed", "error": "Package installation failed"})
        lifecycle_result = {
            "contract_version": VALIDATION_RESULT_CONTRACT_VERSION,
            "attempt_id": attempt_id,
            "build_run_id": run["id"],
            "artifact": identity,
            "status": status,
            "started_at": lifecycle_started,
            "finished_at": finished_at,
            "execution": {
                "network": "disabled",
                "network_verified": True,
                "cleanup": {"status": "success", "absence_proved": True},
            },
            "profile": {"name": "bookworm", "image": image},
            "checks": checks,
            "commands": [],
            "error": (None if status == "success" else {
                "code": error["code"],
                "message": error["message"],
                "failed_checks": ["package_install"] if status == "failed" else [],
                "cleanup_code": "",
            }),
        }
        storage.save_json(root / "result.json", lifecycle_result)
    return attempt
