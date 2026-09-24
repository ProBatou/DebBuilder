"""Fail-closed release check for qualified Validation image inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from debbuilder.validation_images import ValidationImageError, load_manifest


EFFECTIVE_INPUTS = {
    "bookworm": ("validation/Dockerfile",),
    "bookworm-node22": ("validation/Dockerfile.node22",),
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ValidationReleaseInputError(RuntimeError):
    def __init__(self, code: str, message: str, *, profile: str = ""):
        super().__init__(message)
        self.code = code
        self.profile = profile


def _load_lock(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024:
        raise ValidationReleaseInputError("validation_input_lock_invalid", "Validation image input lock is missing or invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValidationReleaseInputError("validation_input_lock_invalid", "Validation image input lock cannot be parsed") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "profiles"} or document["schema_version"] != 1:
        raise ValidationReleaseInputError("validation_input_lock_invalid", "Validation image input lock structure is invalid")
    profiles = document["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(EFFECTIVE_INPUTS):
        raise ValidationReleaseInputError("validation_input_lock_invalid", "Validation image input lock profiles are incomplete")
    for profile, expected_inputs in EFFECTIVE_INPUTS.items():
        row = profiles[profile]
        if (
            not isinstance(row, dict)
            or set(row) != {"inputs", "input_sha256", "digest"}
            or row.get("inputs") != list(expected_inputs)
            or not isinstance(row.get("input_sha256"), str)
            or SHA256.fullmatch(row["input_sha256"]) is None
            or not isinstance(row.get("digest"), str)
            or DIGEST.fullmatch(row["digest"]) is None
        ):
            raise ValidationReleaseInputError(
                "validation_input_lock_invalid",
                f"Validation image input lock is invalid for {profile}",
                profile=profile,
            )
    return document


def input_fingerprint(repository_root: Path, profile: str) -> str:
    root = repository_root.resolve(strict=True)
    rows = []
    for relative in EFFECTIVE_INPUTS[profile]:
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ValidationReleaseInputError(
                "validation_image_input_missing",
                f"Validation image input is missing or unsafe for {profile}: {relative}",
                profile=profile,
            )
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ValidationReleaseInputError(
                "validation_image_input_missing",
                f"Validation image input escapes the repository for {profile}: {relative}",
                profile=profile,
            ) from exc
        rows.append({"path": relative, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_release_inputs(repository_root: Path, lock_path: Path, manifest_path: Path) -> dict:
    lock = _load_lock(lock_path)
    manifest = load_manifest(manifest_path, complete=True)
    descriptors = {row["profile"]: row for row in manifest["images"]}
    checked = []
    for profile in EFFECTIVE_INPUTS:
        expected = lock["profiles"][profile]
        actual_fingerprint = input_fingerprint(repository_root, profile)
        if actual_fingerprint != expected["input_sha256"]:
            raise ValidationReleaseInputError(
                "validation_image_inputs_changed",
                f"Validation image inputs changed for {profile}. Qualify and update the immutable Validation descriptor before releasing.",
                profile=profile,
            )
        descriptor = descriptors.get(profile)
        if descriptor is None or descriptor["digest"] != expected["digest"]:
            raise ValidationReleaseInputError(
                "validation_image_descriptor_mismatch",
                f"Validation image descriptor does not match the qualified input lock for {profile}",
                profile=profile,
            )
        checked.append({
            "profile": profile,
            "inputs": list(EFFECTIVE_INPUTS[profile]),
            "input_sha256": actual_fingerprint,
            "reference": f"{descriptor['repository']}@{descriptor['digest']}",
            "oci_architecture": descriptor["oci_architecture"],
        })
    return {"schema_version": 1, "profiles": checked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--lock", type=Path, default=Path("validation/validation_images.lock.json"))
    parser.add_argument("--manifest", type=Path, default=Path("debbuilder/validation_images.json"))
    arguments = parser.parse_args(argv)
    try:
        result = validate_release_inputs(
            arguments.repository_root, arguments.lock, arguments.manifest,
        )
    except (ValidationReleaseInputError, ValidationImageError, OSError, ValueError) as exc:
        parser.exit(1, f"release validation input check failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
