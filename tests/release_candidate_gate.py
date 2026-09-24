"""Prove the exact candidate .deb with its packaged image manifest."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def prove(profile: str, package: Path, packaged_root: Path, fixture_root: Path) -> dict:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(packaged_root / "opt/debbuilder"))
    import debbuilder
    from debbuilder import artifact_validation
    from debbuilder.build_store import BuildStore
    from debbuilder.dependency_preparation import (
        SUPERVISOR, begin_lifecycle_attempt, complete_lifecycle_attempt,
        inspect_artifact, prepare_runtime_dependencies,
    )
    from debbuilder.validation_images import load_manifest
    from debbuilder.validation_service import ValidationManager
    from tests.test_dependency_preparation import recipe

    assert Path(debbuilder.__file__).resolve().parent == (packaged_root / "opt/debbuilder/debbuilder").resolve()
    row = next(row for row in load_manifest(complete=True)["images"] if row["profile"] == profile)
    reference = f"{row['repository']}@{row['digest']}"
    SUPERVISOR.open_admission()
    store = BuildStore(fixture_root / "builds")
    configured = recipe("debbuilder")
    configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
    configured["service"] = {"enabled": False, "name": "", "command": ""}
    run = store.create(configured, mode="build", run_id=f"candidate-{profile}")
    artifact = Path(run["workspace"]) / "artifacts" / package.name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package, artifact)
    current = inspect_artifact(artifact, workspace=Path(run["workspace"]))
    persisted = store.load(run["id"])
    persisted["status"] = "success"
    persisted["artifact"] = {
        "path": str(artifact), "name": artifact.name,
        "inspection": {
            "package": current.package, "version": current.version,
            "architecture": current.architecture, "depends": current.depends,
            "maintainer_scripts": [], "conffiles": [], "files": [], "service_units": [],
        },
    }
    store.save(persisted)
    registry = fixture_root / "validation-containers"
    manager = ValidationManager(
        store, execute=lambda *_args: None, registry_root=registry,
        workspace_root=Path(run["workspace"]).parent.parent,
    )
    with store.locked_run(run["id"]):
        admitted = manager._admit_locked(
            run["id"], store.load(run["id"]), profile_name=profile,
            previous_artifact="", automatic=False, publish_after_success=False,
        )
    assert admitted["selected_profile"]["image"] == {"name": reference, "id": None, "digest": reference}
    attempt = prepare_runtime_dependencies(
        run["id"], admitted["id"], store=store, current_artifact=artifact,
        profile_name=profile, registry_root=registry,
    )
    prepared = attempt["prepared"]
    assert prepared["image"]["name"] == reference
    begin_lifecycle_attempt(store, run["id"], admitted["id"], prepared)
    result = artifact_validation.validate_artifact(
        run["id"], store=store, profile=profile,
        prepared_dependencies=prepared, attempt_id=admitted["id"], registry_root=registry,
    )
    complete_lifecycle_attempt(store, run["id"], admitted["id"], result)
    if result["status"] != "success" or result["backend"]["network"] != "disabled" or result["backend"]["network_verified"] is not True or list(registry.glob("*.json")):
        raise RuntimeError(json.dumps({"error": result.get("error"), "failed_checks": [row for row in result.get("checks", []) if row["status"] != "success"]}))
    return {
        "profile": profile, "artifact": package.name, "reference": reference,
        "image_id": prepared["image"]["id"], "status": result["status"],
        "network": "disabled", "network_verified": True,
        "checks": len(result["checks"]), "cleanup": "pass",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("bookworm", "bookworm-node22"), required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--packaged-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prove(args.profile, args.package.resolve(), args.packaged_root.resolve(), args.fixture_root.resolve()), sort_keys=True))
