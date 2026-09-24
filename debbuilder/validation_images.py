"""Release-owned immutable Validation image descriptors and acquisition."""
from __future__ import annotations

import json
import platform
import re
import threading
from pathlib import Path

from .execution_cancellation import ExecutionCancelled, raise_for_cancelled_result
from .validation_oci import OciOwnershipError, PodmanRuntime, MAX_INVENTORY_BYTES
from .validation_profiles import PROFILES, resolve_profile


MANIFEST_NAME = "validation_images.json"
PRODUCTION_REPOSITORIES = {
    "bookworm": "ghcr.io/probatou/debbuilder-validation-bookworm",
    "bookworm-node22": "ghcr.io/probatou/debbuilder-validation-node22",
}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"(?:[a-z0-9][a-z0-9.-]*)(?::[0-9]{2,5})?/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*\Z")
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class ValidationImageError(OciOwnershipError):
    pass


def host_architecture(machine: str | None = None) -> str:
    value = machine if machine is not None else platform.machine()
    normalized = ARCHITECTURES.get(value.lower()) if isinstance(value, str) else None
    if normalized is None:
        raise ValidationImageError("validation_profile_architecture_unsupported", "This host architecture has no Validation image")
    return normalized


def validate_manifest(value: object, *, production: bool = True, complete: bool = False) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "images"} or value["schema_version"] != 1 or not isinstance(value["images"], list):
        raise ValidationImageError("validation_image_manifest_invalid", "Validation image manifest structure is invalid")
    seen: set[tuple[str, str]] = set()
    images = []
    for row in value["images"]:
        if not isinstance(row, dict) or set(row) != {"profile", "architecture", "repository", "digest", "oci_architecture"}:
            raise ValidationImageError("validation_image_manifest_invalid", "Validation image descriptor fields are invalid")
        profile, arch, repository, digest, oci_arch = (row[key] for key in ("profile", "architecture", "repository", "digest", "oci_architecture"))
        if not isinstance(profile, str) or profile not in PROFILES or not isinstance(arch, str) or arch not in {"amd64", "arm64"} or oci_arch != arch:
            raise ValidationImageError("validation_image_manifest_invalid", "Validation image profile or architecture is invalid")
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository) or (production and repository != PRODUCTION_REPOSITORIES[profile]):
            raise ValidationImageError("validation_image_manifest_invalid", "Validation image repository is invalid")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise ValidationImageError("validation_image_manifest_invalid", "Validation image digest must be exact SHA-256")
        key = (profile, arch)
        if key in seen:
            raise ValidationImageError("validation_image_manifest_invalid", "Duplicate Validation image descriptor")
        seen.add(key)
        images.append(dict(row))
    if complete and {(profile, "amd64") for profile in PROFILES} - seen:
        raise ValidationImageError("validation_image_manifest_incomplete", "Release lacks required amd64 Validation image descriptors")
    return {"schema_version": 1, "images": images}


def load_manifest(path: str | Path | None = None, *, production: bool = True, complete: bool = False) -> dict:
    candidate = Path(path) if path is not None else Path(__file__).with_name(MANIFEST_NAME)
    if not candidate.is_file():
        raise ValidationImageError("validation_images_unconfigured", "This installation has no release Validation image manifest")
    if candidate.stat().st_size > 16 * 1024:
        raise ValidationImageError("validation_image_manifest_invalid", "Validation image manifest is too large")
    try:
        return validate_manifest(json.loads(candidate.read_text(encoding="utf-8")), production=production, complete=complete)
    except (ValueError, UnicodeError) as exc:
        raise ValidationImageError("validation_image_manifest_invalid", "Validation image manifest cannot be parsed") from exc


def descriptor(profile: str, architecture: str, manifest: dict) -> dict:
    resolve_profile(profile)
    if architecture not in {"amd64", "arm64"}:
        raise ValidationImageError("validation_profile_architecture_unsupported", "This host architecture has no Validation image")
    for row in manifest["images"]:
        if row["profile"] == profile and row["architecture"] == architecture:
            return dict(row)
    raise ValidationImageError("validation_profile_architecture_unsupported", f"Validation profile {profile} has no {architecture} image")


def _verified_local(runtime: PodmanRuntime, row: dict) -> dict | None:
    reference = f"{row['repository']}@{row['digest']}"
    exists = runtime.run(["podman", "image", "exists", reference], timeout=30)
    if exists.get("status") != "success":
        if exists.get("exit_code") == 1:
            return None
        raise ValidationImageError("validation_image_inspection_failed", "Podman could not check the exact Validation image")
    inspected = runtime.run(["podman", "image", "inspect", reference], timeout=30, output_limit=MAX_INVENTORY_BYTES)
    if inspected.get("status") != "success":
        raise ValidationImageError("validation_image_unverifiable", "Podman could not inspect the exact Validation image")
    try:
        rows = json.loads(inspected.get("stdout") or "")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("image inspection shape")
        image = rows[0]
        image_id = str(image["Id"]).lower()
        if re.fullmatch(r"[0-9a-f]{64}", image_id):
            image_id = "sha256:" + image_id
        if not DIGEST.fullmatch(image_id):
            raise ValueError("image ID")
        digests = image["RepoDigests"]
        if not isinstance(digests, list) or reference not in digests:
            raise ValueError("repository digest")
        if image["Architecture"] != row["oci_architecture"]:
            raise ValidationImageError("validation_image_architecture_mismatch", "Validation image architecture differs from its release descriptor")
    except ValidationImageError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationImageError("validation_image_digest_mismatch", "Podman did not prove the expected Validation image digest") from exc
    return {"name": reference, "id": image_id, "digest": reference}


def admitted_image(profile: str, *, manifest: dict | None = None, architecture: str | None = None) -> dict:
    trusted = load_manifest() if manifest is None else validate_manifest(manifest, production=False)
    row = descriptor(profile, architecture or host_architecture(), trusted)
    reference = f"{row['repository']}@{row['digest']}"
    return {"name": reference, "id": None, "digest": reference}


def provision_admitted_image(runtime: PodmanRuntime, profile: str, image: dict, *, production: bool = True, architecture: str | None = None) -> dict:
    if not isinstance(image, dict) or set(image) != {"name", "id", "digest"} or image["id"] is not None or image["name"] != image["digest"] or not isinstance(image["name"], str) or image["name"].count("@") != 1:
        raise ValidationImageError("validation_image_manifest_invalid", "Admitted Validation image reference is invalid")
    repository, digest = image["name"].split("@")
    arch = architecture or host_architecture()
    row = {"profile": profile, "architecture": arch, "oci_architecture": arch, "repository": repository, "digest": digest}
    validate_manifest({"schema_version": 1, "images": [row]}, production=production)
    return _provision_row(runtime, row)


def provision_image(runtime: PodmanRuntime, profile: str, *, manifest: dict | None = None, architecture: str | None = None) -> dict:
    trusted = load_manifest() if manifest is None else validate_manifest(manifest, production=False)
    row = descriptor(profile, architecture or host_architecture(), trusted)
    return _provision_row(runtime, row)


def _provision_row(runtime: PodmanRuntime, row: dict) -> dict:
    reference = f"{row['repository']}@{row['digest']}"
    with _locks_guard:
        lock = _locks.setdefault(reference, threading.Lock())
    with lock:
        if runtime.cancellation_event is not None and runtime.cancellation_event.is_set():
            raise ExecutionCancelled()
        found = _verified_local(runtime, row)
        if found is not None:
            return found
        pulled = runtime.run(["podman", "pull", "--quiet", reference], timeout=600, cancellable_control=True)
        raise_for_cancelled_result(pulled)
        if pulled.get("status") != "success":
            raise ValidationImageError("validation_image_provisioning_retryable", "Could not pull the exact Validation image; retry Validation when the registry is available")
        if runtime.cancellation_event is not None and runtime.cancellation_event.is_set():
            raise ExecutionCancelled()
        found = _verified_local(runtime, row)
        if found is None:
            raise ValidationImageError("validation_image_digest_mismatch", "Pulled Validation image is not locally addressable by its exact digest")
        return found
