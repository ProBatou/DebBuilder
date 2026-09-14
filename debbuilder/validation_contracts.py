"""Versioned durable contracts for future runtime dependency validation.

These validators are intentionally execution-independent. CP1A does not make
artifact validation asynchronous and does not prepare or install packages.
"""
from __future__ import annotations

import copy
from datetime import datetime
import re

from .apt_repo import DEBIAN_VERSION_PATTERN
from .runtime_apt_repositories import (
    normalize_components,
    normalize_repository_id,
    normalize_repository_uri,
    normalize_suite,
)


VALIDATION_ATTEMPT_CONTRACT_VERSION = 1
PREPARED_DEPENDENCIES_CONTRACT_VERSION = 1
HISTORICAL_VALIDATION_CONTRACT_VERSION = 0
VALIDATION_STATUSES = frozenset({"queued", "running", "cancelling", "cancelled", "success", "failed"})
MAX_PACKAGES = 512
MAX_DIAGNOSTICS = 50
MAX_ENFORCEMENT_OBSERVATIONS = 32
MAX_FINGERPRINTS = 16
MAX_SAFE_INTEGER = (1 << 53) - 1

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")
SAFE_ARCHITECTURE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
OPENPGP_FINGERPRINT = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
ABSOLUTE_PATH_TEXT = re.compile(r"(?<![/A-Za-z0-9])/(?!/)(?:[^\s)]*)")


class ValidationContractError(ValueError):
    def __init__(self, code: str, message: str, *, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.path = path


def _object(value, fields: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", f"{path} must be an object", path=path)
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown or missing:
        detail = f"unknown field {unknown[0]}" if unknown else f"missing field {missing[0]}"
        raise ValidationContractError("invalid_contract", f"{path} has {detail}", path=path)
    return value


def _string(value, path: str, *, maximum: int, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationContractError("invalid_contract", f"{path} must be a non-empty string", path=path)
    if len(value) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationContractError("invalid_contract", f"{path} is not a bounded printable string", path=path)
    if pattern is not None and not pattern.fullmatch(value):
        raise ValidationContractError("invalid_contract", f"{path} has an invalid format", path=path)
    return value


def _timestamp(value, path: str) -> tuple[str, datetime]:
    timestamp = _string(value, path, maximum=64)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationContractError("invalid_contract", f"{path} must be an ISO-8601 timestamp", path=path) from exc
    if parsed.tzinfo is None:
        raise ValidationContractError("invalid_contract", f"{path} must include a timezone", path=path)
    return timestamp, parsed


def _optional_timestamp(value, path: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    return _timestamp(value, path)


def _sha256(value, path: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not SHA256.fullmatch(value.lower()):
        raise ValidationContractError("invalid_contract", f"{path} must be a SHA-256 digest", path=path)
    return value.lower()


def _positive_size(value, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_SAFE_INTEGER:
        raise ValidationContractError("invalid_contract", f"{path} must be a positive JSON-safe integer", path=path)
    return value


def _safe_diagnostic_text(value, path: str, *, maximum: int) -> str:
    text = _string(value, path, maximum=maximum)
    if re.search(r"(?:PGP (?:PUBLIC|PRIVATE) KEY BLOCK|-----BEGIN PGP|-----END PGP)", text, re.IGNORECASE):
        raise ValidationContractError("sensitive_contract_data", f"{path} must not contain signing-key material", path=path)
    if ABSOLUTE_PATH_TEXT.search(text) or re.search(r"\bfile:/", text, re.IGNORECASE):
        raise ValidationContractError("ephemeral_contract_data", f"{path} must not contain an absolute filesystem path", path=path)
    return text


def _durable_identity_string(value, path: str, *, maximum: int) -> str:
    text = _string(value, path, maximum=maximum)
    if text.startswith("/") or text.lower().startswith("file:/"):
        raise ValidationContractError(
            "ephemeral_contract_data", f"{path} must not contain a local filesystem path", path=path,
        )
    if any(character.isspace() for character in text):
        raise ValidationContractError("invalid_contract", f"{path} must not contain whitespace", path=path)
    return text


def normalize_artifact_identity(value, *, path: str = "$.artifact") -> dict:
    value = _object(value, {"package", "version", "architecture", "size", "sha256"}, path)
    return {
        "package": _string(value["package"], f"{path}.package", maximum=128, pattern=SAFE_PACKAGE),
        "version": _string(
            value["version"], f"{path}.version", maximum=256, pattern=DEBIAN_VERSION_PATTERN,
        ),
        "architecture": _string(
            value["architecture"], f"{path}.architecture", maximum=32, pattern=SAFE_ARCHITECTURE,
        ),
        "size": _positive_size(value["size"], f"{path}.size"),
        "sha256": _sha256(value["sha256"], f"{path}.sha256"),
    }


def normalize_image_identity(value, *, path: str) -> dict:
    value = _object(value, {"name", "id", "digest"}, path)
    return {
        "name": _durable_identity_string(value["name"], f"{path}.name", maximum=512),
        "id": None if value["id"] is None else _durable_identity_string(
            value["id"], f"{path}.id", maximum=512,
        ),
        "digest": None if value["digest"] is None else _durable_identity_string(
            value["digest"], f"{path}.digest", maximum=512,
        ),
    }


def _normalize_prepared_image_identity(value, *, path: str) -> dict:
    image = normalize_image_identity(value, path=path)
    image_id = image["id"]
    if image_id is None or not OCI_IMAGE_ID.fullmatch(image_id.lower()):
        raise ValidationContractError(
            "invalid_contract",
            f"{path}.id must identify the exact OCI image by SHA-256",
            path=f"{path}.id",
        )
    image["id"] = image_id.lower()
    return image


def normalize_repository_provenance(value, *, path: str) -> dict:
    value = _object(
        value,
        {"id", "uri", "suite", "components", "signing_key_sha256", "signing_key_fingerprints"},
        path,
    )
    try:
        identifier = normalize_repository_id(value["id"], f"{path}.id")
        uri = normalize_repository_uri(value["uri"])
        suite = normalize_suite(value["suite"])
        components = normalize_components(value["components"])
    except ValueError as exc:
        raise ValidationContractError("invalid_contract", str(exc), path=path) from exc
    content_hash = _sha256(value["signing_key_sha256"], f"{path}.signing_key_sha256", optional=True)
    fingerprints = value["signing_key_fingerprints"]
    if not isinstance(fingerprints, list) or len(fingerprints) > MAX_FINGERPRINTS:
        raise ValidationContractError("invalid_contract", f"{path}.signing_key_fingerprints is invalid", path=path)
    normalized_fingerprints: list[str] = []
    for index, fingerprint in enumerate(fingerprints):
        if not isinstance(fingerprint, str) or not OPENPGP_FINGERPRINT.fullmatch(fingerprint.upper()):
            raise ValidationContractError(
                "invalid_contract", "OpenPGP fingerprints must contain 40 or 64 hexadecimal characters",
                path=f"{path}.signing_key_fingerprints[{index}]",
            )
        fingerprint = fingerprint.upper()
        if fingerprint in normalized_fingerprints:
            raise ValidationContractError(
                "invalid_contract", "OpenPGP fingerprints must be unique",
                path=f"{path}.signing_key_fingerprints[{index}]",
            )
        normalized_fingerprints.append(fingerprint)
    if content_hash is None:
        raise ValidationContractError(
            "invalid_contract", "Repository provenance requires the signing-key content hash",
            path=f"{path}.signing_key_sha256",
        )
    if not normalized_fingerprints:
        raise ValidationContractError(
            "invalid_contract", "Repository provenance requires at least one verified signing-key fingerprint",
            path=f"{path}.signing_key_fingerprints",
        )
    return {
        "id": identifier,
        "uri": uri,
        "suite": suite,
        "components": components,
        "signing_key_sha256": content_hash,
        "signing_key_fingerprints": normalized_fingerprints,
    }


def _normalize_package_origin(value, *, path: str) -> dict | None:
    if value is None:
        return None
    value = _object(value, {"repository_id", "suite", "component"}, path)
    try:
        return {
            "repository_id": normalize_repository_id(value["repository_id"], f"{path}.repository_id"),
            "suite": normalize_suite(value["suite"]),
            "component": normalize_components([value["component"]])[0],
        }
    except ValueError as exc:
        raise ValidationContractError("invalid_contract", str(exc), path=path) from exc


def _normalize_resolved_package(value, *, path: str) -> dict:
    value = _object(value, {"package", "version", "architecture", "role", "size", "sha256", "origin"}, path)
    role = value["role"]
    if role not in {"previous", "current"}:
        raise ValidationContractError("invalid_contract", f"{path}.role must be previous or current", path=f"{path}.role")
    return {
        "package": _string(value["package"], f"{path}.package", maximum=128, pattern=SAFE_PACKAGE),
        "version": _string(
            value["version"], f"{path}.version", maximum=256, pattern=DEBIAN_VERSION_PATTERN,
        ),
        "architecture": _string(
            value["architecture"], f"{path}.architecture", maximum=32, pattern=SAFE_ARCHITECTURE,
        ),
        "role": role,
        "size": _positive_size(value["size"], f"{path}.size"),
        "sha256": _sha256(value["sha256"], f"{path}.sha256"),
        "origin": _normalize_package_origin(value["origin"], path=f"{path}.origin"),
    }


def _normalize_base_package(value, *, path: str) -> dict:
    value = _object(value, {"package", "version", "architecture", "role"}, path)
    if value["role"] not in {"previous", "current"}:
        raise ValidationContractError("invalid_contract", f"{path}.role must be previous or current", path=f"{path}.role")
    return {
        "package": _string(value["package"], f"{path}.package", maximum=128, pattern=SAFE_PACKAGE),
        "version": _string(value["version"], f"{path}.version", maximum=256, pattern=DEBIAN_VERSION_PATTERN),
        "architecture": _string(value["architecture"], f"{path}.architecture", maximum=32, pattern=SAFE_ARCHITECTURE),
        "role": value["role"],
    }


def _normalize_diagnostics(value, *, path: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_DIAGNOSTICS:
        raise ValidationContractError("invalid_contract", f"{path} must be a bounded list", path=path)
    normalized = []
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        row = _object(row, {"code", "message"}, row_path)
        normalized.append({
            "code": _string(row["code"], f"{row_path}.code", maximum=128, pattern=SAFE_CODE),
            "message": _safe_diagnostic_text(row["message"], f"{row_path}.message", maximum=1000),
        })
    return normalized


def _normalize_enforcement(value, *, path: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_ENFORCEMENT_OBSERVATIONS:
        raise ValidationContractError("invalid_contract", f"{path} must be a bounded list", path=path)
    normalized = []
    seen: set[str] = set()
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        row = _object(row, {"control", "status", "observed"}, row_path)
        control = _string(row["control"], f"{row_path}.control", maximum=128, pattern=SAFE_CODE)
        if control in seen:
            raise ValidationContractError("invalid_contract", f"duplicate enforcement control: {control}", path=row_path)
        if row["status"] not in {"enforced", "not_requested", "unavailable"}:
            raise ValidationContractError("invalid_contract", f"{row_path}.status is invalid", path=f"{row_path}.status")
        seen.add(control)
        normalized.append({
            "control": control,
            "status": row["status"],
            "observed": _safe_diagnostic_text(row["observed"], f"{row_path}.observed", maximum=500),
        })
    return normalized


def normalize_prepared_runtime_dependencies(value) -> dict:
    path = "$.prepared_dependencies"
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", f"{path} must be an object", path=path)
    version = value.get("contract_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationContractError("invalid_contract_version", "Prepared dependency contract version must be an integer", path=f"{path}.contract_version")
    if version > PREPARED_DEPENDENCIES_CONTRACT_VERSION:
        raise ValidationContractError("future_contract_version", "Prepared dependency contract is newer than supported", path=f"{path}.contract_version")
    if version != PREPARED_DEPENDENCIES_CONTRACT_VERSION:
        raise ValidationContractError("unsupported_contract_version", "Prepared dependency contract version is unsupported", path=f"{path}.contract_version")
    # CP1B added exact APT-selected base satisfiers to the still-unreleased v1
    # contract. Accept CP1A drafts as an empty base-satisfier set.
    if "base_packages" not in value:
        value = {**value, "base_packages": []}
    value = _object(value, {
        "contract_version", "profile_name", "image", "native_architecture", "artifacts",
        "repositories", "base_packages", "packages", "started_at", "finished_at", "diagnostics", "enforcement",
    }, path)
    profile_name = _string(value["profile_name"], f"{path}.profile_name", maximum=128, pattern=SAFE_ID)
    native_architecture = _string(
        value["native_architecture"], f"{path}.native_architecture", maximum=32, pattern=SAFE_ARCHITECTURE,
    )
    if native_architecture == "all":
        raise ValidationContractError("invalid_contract", "Native architecture must not be all", path=f"{path}.native_architecture")
    artifacts = _object(value["artifacts"], {"current", "previous"}, f"{path}.artifacts")
    current = normalize_artifact_identity(artifacts["current"], path=f"{path}.artifacts.current")
    previous = None if artifacts["previous"] is None else normalize_artifact_identity(
        artifacts["previous"], path=f"{path}.artifacts.previous",
    )
    if current["architecture"] not in {"all", native_architecture}:
        raise ValidationContractError("invalid_contract", "Current artifact architecture is not native or all", path=f"{path}.artifacts.current.architecture")
    if previous is not None and (
        previous["package"] != current["package"]
        or previous["architecture"] != current["architecture"]
    ):
        raise ValidationContractError("invalid_contract", "Previous artifact identity is incompatible with current artifact", path=f"{path}.artifacts.previous")

    repositories = value["repositories"]
    if not isinstance(repositories, list) or len(repositories) > 32:
        raise ValidationContractError("invalid_contract", f"{path}.repositories must be a bounded list", path=f"{path}.repositories")
    normalized_repositories: list[dict] = []
    repository_ids: set[str] = set()
    for index, repository in enumerate(repositories):
        normalized = normalize_repository_provenance(repository, path=f"{path}.repositories[{index}]")
        if normalized["id"] in repository_ids:
            raise ValidationContractError("invalid_contract", f"duplicate repository provenance id: {normalized['id']}", path=f"{path}.repositories[{index}].id")
        repository_ids.add(normalized["id"])
        normalized_repositories.append(normalized)

    packages = value["packages"]
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise ValidationContractError("invalid_contract", f"{path}.packages must be a bounded list", path=f"{path}.packages")
    normalized_packages: list[dict] = []
    phase_packages: set[tuple[str, str]] = set()
    repositories_by_id = {repository["id"]: repository for repository in normalized_repositories}
    for index, package in enumerate(packages):
        normalized = _normalize_resolved_package(package, path=f"{path}.packages[{index}]")
        key = (normalized["role"], normalized["package"])
        if key in phase_packages:
            raise ValidationContractError(
                "invalid_contract", f"duplicate or conflicting package identity for {normalized['role']}:{normalized['package']}",
                path=f"{path}.packages[{index}]",
            )
        if normalized["architecture"] not in {"all", native_architecture}:
            raise ValidationContractError("invalid_contract", "Resolved package architecture is not native or all", path=f"{path}.packages[{index}].architecture")
        origin = normalized["origin"]
        if origin is not None:
            repository = repositories_by_id.get(origin["repository_id"])
            if repository is None:
                raise ValidationContractError(
                    "invalid_contract", "Package origin does not identify recorded repository provenance",
                    path=f"{path}.packages[{index}].origin.repository_id",
                )
            if origin["suite"] != repository["suite"] or origin["component"] not in repository["components"]:
                raise ValidationContractError(
                    "invalid_contract", "Package origin does not match recorded repository suite and component",
                    path=f"{path}.packages[{index}].origin",
                )
        phase_packages.add(key)
        normalized_packages.append(normalized)

    base_packages = value["base_packages"]
    if not isinstance(base_packages, list) or len(base_packages) > MAX_PACKAGES:
        raise ValidationContractError("invalid_contract", f"{path}.base_packages must be a bounded list", path=f"{path}.base_packages")
    normalized_base_packages: list[dict] = []
    seen_base: set[tuple[str, str]] = set()
    for index, package in enumerate(base_packages):
        normalized = _normalize_base_package(package, path=f"{path}.base_packages[{index}]")
        key = (normalized["role"], normalized["package"], normalized["architecture"])
        if key in seen_base or normalized["architecture"] not in {"all", native_architecture}:
            raise ValidationContractError("invalid_contract", "Base package identity is duplicate or foreign", path=f"{path}.base_packages[{index}]")
        seen_base.add(key)
        normalized_base_packages.append(normalized)

    started_at, started = _timestamp(value["started_at"], f"{path}.started_at")
    finished_at, finished = _timestamp(value["finished_at"], f"{path}.finished_at")
    if finished < started:
        raise ValidationContractError("invalid_contract", "Prepared dependency timestamps are out of order", path=f"{path}.finished_at")
    return {
        "contract_version": version,
        "profile_name": profile_name,
        "image": _normalize_prepared_image_identity(value["image"], path=f"{path}.image"),
        "native_architecture": native_architecture,
        "artifacts": {"current": current, "previous": previous},
        "repositories": normalized_repositories,
        "base_packages": normalized_base_packages,
        "packages": normalized_packages,
        "started_at": started_at,
        "finished_at": finished_at,
        "diagnostics": _normalize_diagnostics(value["diagnostics"], path=f"{path}.diagnostics"),
        "enforcement": _normalize_enforcement(value["enforcement"], path=f"{path}.enforcement"),
    }


def _normalize_attempt_error(value, *, path: str) -> dict | None:
    if value is None:
        return None
    value = _object(value, {"code", "message"}, path)
    return {
        "code": _string(value["code"], f"{path}.code", maximum=128, pattern=SAFE_CODE),
        "message": _safe_diagnostic_text(value["message"], f"{path}.message", maximum=1000),
    }


def _normalize_attempt_result(value, *, path: str) -> dict | None:
    if value is None:
        return None
    value = _object(value, {"status", "reference"}, path)
    if value["status"] not in {"success", "failed"}:
        raise ValidationContractError("invalid_contract", f"{path}.status is invalid", path=f"{path}.status")
    reference = value["reference"]
    if reference is not None:
        reference = _string(reference, f"{path}.reference", maximum=256)
        segments = reference.split("/")
        if (
            reference.startswith("/") or "\\" in reference or "?" in reference or "#" in reference
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise ValidationContractError("ephemeral_contract_data", "Validation result reference must be a safe relative path", path=f"{path}.reference")
    return {"status": value["status"], "reference": reference}


def normalize_validation_attempt(value) -> dict:
    """Normalize a v1 attempt or tag an unversioned historical record in memory."""
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", "Validation attempt must be an object")
    if "contract_version" not in value:
        if not isinstance(value.get("id"), str) or not value["id"] or not isinstance(value.get("status"), str):
            raise ValidationContractError("invalid_historical_validation", "Historical validation record lacks identity or status")
        normalized = copy.deepcopy(value)
        normalized["contract_version"] = HISTORICAL_VALIDATION_CONTRACT_VERSION
        return normalized
    version = value["contract_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationContractError("invalid_contract_version", "Validation attempt contract version must be an integer", path="$.contract_version")
    if version == HISTORICAL_VALIDATION_CONTRACT_VERSION:
        normalized = copy.deepcopy(value)
        if not isinstance(normalized.get("id"), str) or not normalized["id"] or not isinstance(normalized.get("status"), str):
            raise ValidationContractError("invalid_historical_validation", "Historical validation record lacks identity or status")
        return normalized
    if version > VALIDATION_ATTEMPT_CONTRACT_VERSION:
        raise ValidationContractError("future_contract_version", "Validation attempt contract is newer than supported", path="$.contract_version")
    if version != VALIDATION_ATTEMPT_CONTRACT_VERSION:
        raise ValidationContractError("unsupported_contract_version", "Validation attempt contract version is unsupported", path="$.contract_version")

    value = _object(value, {
        "contract_version", "id", "build_run_id", "inputs", "selected_profile", "created_at",
        "started_at", "finished_at", "status", "prepared_dependencies", "result", "error",
    }, "$")
    identifier = _string(value["id"], "$.id", maximum=128, pattern=SAFE_ID)
    build_run_id = _string(value["build_run_id"], "$.build_run_id", maximum=128, pattern=SAFE_ID)
    inputs = _object(value["inputs"], {"profile", "artifact", "previous_artifact"}, "$.inputs")
    profile = _string(inputs["profile"], "$.inputs.profile", maximum=128, pattern=SAFE_ID)
    artifact = normalize_artifact_identity(inputs["artifact"], path="$.inputs.artifact")
    previous_artifact = None if inputs["previous_artifact"] is None else normalize_artifact_identity(
        inputs["previous_artifact"], path="$.inputs.previous_artifact",
    )
    if previous_artifact is not None and (
        previous_artifact["package"] != artifact["package"]
        or previous_artifact["architecture"] != artifact["architecture"]
    ):
        raise ValidationContractError(
            "invalid_contract", "Previous immutable artifact identity is incompatible with current artifact",
            path="$.inputs.previous_artifact",
        )
    selected_profile = _object(value["selected_profile"], {"name", "image"}, "$.selected_profile")
    selected_profile_name = _string(
        selected_profile["name"], "$.selected_profile.name", maximum=128, pattern=SAFE_ID,
    )
    if selected_profile_name != profile:
        raise ValidationContractError("invalid_contract", "Selected profile does not match immutable inputs", path="$.selected_profile.name")
    selected_image = normalize_image_identity(selected_profile["image"], path="$.selected_profile.image")
    created_at, created = _timestamp(value["created_at"], "$.created_at")
    started_at, started = _optional_timestamp(value["started_at"], "$.started_at")
    finished_at, finished = _optional_timestamp(value["finished_at"], "$.finished_at")
    status = value["status"]
    if status not in VALIDATION_STATUSES:
        raise ValidationContractError("invalid_contract", "Validation attempt status is invalid", path="$.status")
    result = _normalize_attempt_result(value["result"], path="$.result")
    error = _normalize_attempt_error(value["error"], path="$.error")
    prepared = None if value["prepared_dependencies"] is None else normalize_prepared_runtime_dependencies(
        value["prepared_dependencies"],
    )

    if status == "queued" and (
        started is not None or finished is not None or prepared is not None or result is not None or error is not None
    ):
        raise ValidationContractError("invalid_contract", "Queued validation attempt has terminal or started fields", path="$.status")
    if status in {"running", "cancelling"} and (started is None or finished is not None or result is not None or error is not None):
        raise ValidationContractError("invalid_contract", "Active validation attempt has inconsistent fields", path="$.status")
    if status == "cancelled" and (finished is None or result is not None or error is None):
        raise ValidationContractError("invalid_contract", "Cancelled validation attempt has inconsistent terminal fields", path="$.status")
    if status in {"success", "failed"} and (
        started is None or finished is None or result is None or result["status"] != status
    ):
        raise ValidationContractError("invalid_contract", "Terminal validation attempt has inconsistent result fields", path="$.status")
    if status == "success" and error is not None:
        raise ValidationContractError("invalid_contract", "Successful validation attempt must not contain an error", path="$.error")
    if status == "success" and prepared is None:
        raise ValidationContractError(
            "invalid_contract", "Successful validation attempt requires prepared dependency provenance",
            path="$.prepared_dependencies",
        )
    if status == "failed" and error is None:
        raise ValidationContractError("invalid_contract", "Failed validation attempt requires an error", path="$.error")
    if started is not None and started < created:
        raise ValidationContractError("invalid_contract", "Validation attempt started before creation", path="$.started_at")
    if finished is not None and finished < (started or created):
        raise ValidationContractError("invalid_contract", "Validation attempt finished before it started", path="$.finished_at")
    if prepared is not None and prepared["profile_name"] != profile:
        raise ValidationContractError("invalid_contract", "Prepared dependencies use a different profile", path="$.prepared_dependencies.profile_name")
    if prepared is not None:
        if prepared["artifacts"] != {"current": artifact, "previous": previous_artifact}:
            raise ValidationContractError(
                "invalid_contract", "Prepared dependencies do not match immutable artifact inputs",
                path="$.prepared_dependencies.artifacts",
            )
        if prepared["image"] != selected_image:
            raise ValidationContractError(
                "invalid_contract", "Prepared dependencies do not match the selected image identity",
                path="$.prepared_dependencies.image",
            )
        _, prepared_started = _timestamp(prepared["started_at"], "$.prepared_dependencies.started_at")
        _, prepared_finished = _timestamp(prepared["finished_at"], "$.prepared_dependencies.finished_at")
        if started is None or prepared_started < started or (finished is not None and prepared_finished > finished):
            raise ValidationContractError(
                "invalid_contract", "Prepared dependency timestamps fall outside the validation attempt",
                path="$.prepared_dependencies",
            )

    return {
        "contract_version": version,
        "id": identifier,
        "build_run_id": build_run_id,
        "inputs": {"profile": profile, "artifact": artifact, "previous_artifact": previous_artifact},
        "selected_profile": {
            "name": selected_profile_name,
            "image": selected_image,
        },
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "prepared_dependencies": prepared,
        "result": result,
        "error": error,
    }
