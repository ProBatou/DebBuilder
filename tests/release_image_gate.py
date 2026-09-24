"""Real canonical lifecycle gate for a just-built release Validation image."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from debbuilder import artifact_validation
from debbuilder.build_store import BuildStore
from debbuilder.dependency_preparation import SUPERVISOR, begin_lifecycle_attempt, complete_lifecycle_attempt
from debbuilder.validation_oci import PodmanRuntime
from tests.test_dependency_preparation import build_deb, recipe
from tests.validation_helpers import prepare_admitted_for_test


def prove(profile: str, image_reference: str) -> dict:
    image = PodmanRuntime(Path.cwd()).inspect_image(image_reference)
    SUPERVISOR.open_admission()
    with tempfile.TemporaryDirectory(prefix="debbuilder-release-image-") as temporary:
        root = Path(temporary)
        configured = recipe("release-image-gate")
        configured["artifact"] = {"mode": "upstream_deb", "architecture": "all"}
        configured["service"] = {"enabled": False, "name": "", "command": ""}
        store = BuildStore(root / "builds")
        run = store.create(configured, mode="build", run_id="release-image-gate")
        dependency = "nodejs (>= 22)" if profile == "bookworm-node22" else ""
        artifact = build_deb(
            root, Path(run["workspace"]) / "artifacts/release-image-gate_1.0-1_all.deb",
            package="release-image-gate", version="1.0-1", depends=dependency,
        )
        persisted = store.load(run["id"])
        persisted["status"] = "success"
        persisted["artifact"] = {
            "path": str(artifact), "name": artifact.name,
            "inspection": {"maintainer_scripts": [], "conffiles": [], "files": [], "service_units": [], "depends": dependency},
        }
        store.save(persisted)
        registry = root / "validation-containers"
        attempt = prepare_admitted_for_test(
            run["id"], store=store, current_artifact=artifact,
            profile_name=profile, registry_root=registry, image_override=image,
        )
        prepared = attempt["prepared"]
        begin_lifecycle_attempt(store, run["id"], attempt["id"], prepared)
        result = artifact_validation.validate_artifact(
            run["id"], store=store, profile=profile,
            prepared_dependencies=prepared, attempt_id=attempt["id"], registry_root=registry,
        )
        complete_lifecycle_attempt(store, run["id"], attempt["id"], result)
        if result["status"] != "success" or result["backend"]["network"] != "disabled" or result["backend"]["network_verified"] is not True or list(registry.glob("*.json")):
            raise RuntimeError(f"Canonical release image lifecycle failed: {result.get('error')}")
        return {"profile": profile, "image_id": image["id"], "status": result["status"], "network": "disabled"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("bookworm", "bookworm-node22"), required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    print(prove(args.profile, args.image))
