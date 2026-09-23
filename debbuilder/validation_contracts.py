"""Strict current-v1 contracts for Validation admission, preparation, and results."""
from __future__ import annotations

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
VALIDATION_RESULT_CONTRACT_VERSION = 1
VALIDATION_STATUSES = frozenset({"queued", "running", "cancelling", "cancelled", "success", "failed"})
REQUIRED_SUCCESS_CHECKS = frozenset({
    "lifecycle_network_disabled",
    "package_install",
    "package_status_installed",
    "package_remove",
    "package_purge",
    "package_absent_after_purge",
})
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
    path = "$"
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", f"{path} must be an object", path=path)
    version = value.get("contract_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationContractError("invalid_contract_version", "Prepared dependency contract version must be an integer", path=f"{path}.contract_version")
    if version > PREPARED_DEPENDENCIES_CONTRACT_VERSION:
        raise ValidationContractError("future_contract_version", "Prepared dependency contract is newer than supported", path=f"{path}.contract_version")
    if version != PREPARED_DEPENDENCIES_CONTRACT_VERSION:
        raise ValidationContractError("unsupported_contract_version", "Prepared dependency contract version is unsupported", path=f"{path}.contract_version")
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
    if value["status"] not in {"success", "failed", "cancelled"}:
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
    """Normalize the sole supported current-v1 Validation attempt contract."""
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", "Validation attempt must be an object")
    if "contract_version" not in value:
        raise ValidationContractError(
            "invalid_contract_version", "Validation attempt contract_version is required",
            path="$.contract_version",
        )
    version = value["contract_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationContractError("invalid_contract_version", "Validation attempt contract version must be an integer", path="$.contract_version")
    if version > VALIDATION_ATTEMPT_CONTRACT_VERSION:
        raise ValidationContractError("future_contract_version", "Validation attempt contract is newer than supported", path="$.contract_version")
    if version != VALIDATION_ATTEMPT_CONTRACT_VERSION:
        raise ValidationContractError("unsupported_contract_version", "Validation attempt contract version is unsupported", path="$.contract_version")

    value = _object(value, {
        "contract_version", "id", "build_run_id", "inputs", "selected_profile", "created_at",
        "started_at", "finished_at", "status", "result", "error",
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

    if status == "queued" and (
        started is not None or finished is not None or result is not None or error is not None
    ):
        raise ValidationContractError("invalid_contract", "Queued validation attempt has terminal or started fields", path="$.status")
    if status in {"running", "cancelling"} and (started is None or finished is not None or result is not None or error is not None):
        raise ValidationContractError("invalid_contract", "Active validation attempt has inconsistent fields", path="$.status")
    if status == "cancelled" and (finished is None or error is None):
        raise ValidationContractError("invalid_contract", "Cancelled validation attempt has inconsistent terminal fields", path="$.status")
    if status == "success" and (
        started is None or finished is None or result is None or result["status"] != status
    ):
        raise ValidationContractError("invalid_contract", "Terminal validation attempt has inconsistent result fields", path="$.status")
    if status in {"failed", "cancelled"} and (
        finished is None or (result is not None and result["status"] != status)
    ):
        raise ValidationContractError("invalid_contract", "Terminal validation attempt has inconsistent result fields", path="$.status")
    if status == "success" and error is not None:
        raise ValidationContractError("invalid_contract", "Successful validation attempt must not contain an error", path="$.error")
    if status == "success" and (result is None or result["reference"] != "result.json"):
        raise ValidationContractError("invalid_contract", "Successful validation attempt requires result.json", path="$.result")
    if result is not None and result["reference"] != "result.json":
        raise ValidationContractError("invalid_contract", "Validation result reference must be result.json", path="$.result.reference")
    if status == "failed" and error is None:
        raise ValidationContractError("invalid_contract", "Failed validation attempt requires an error", path="$.error")
    if started is not None and started < created:
        raise ValidationContractError("invalid_contract", "Validation attempt started before creation", path="$.started_at")
    if finished is not None and finished < (started or created):
        raise ValidationContractError("invalid_contract", "Validation attempt finished before it started", path="$.finished_at")
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
        "result": result,
        "error": error,
    }


def _optional_string(value, path: str, *, maximum: int) -> str:
    if value is None or value == "":
        return ""
    return _safe_diagnostic_text(value, path, maximum=maximum)


def _optional_printable(value, path: str, *, maximum: int) -> str:
    if value is None or value == "":
        return ""
    value = str(value)
    if len(value) > maximum:
        raise ValidationContractError("invalid_contract", f"{path} is too long", path=path)
    if any(ord(character) < 32 and character not in "\t\r\n" or ord(character) == 127 for character in value):
        raise ValidationContractError("invalid_contract", f"{path} contains control characters", path=path)
    return value


def _normalize_result_error(value, *, path: str) -> dict | None:
    if value is None:
        return None
    value = _object(value, {"code", "message", "failed_checks", "cleanup_code"}, path)
    failed_checks = value["failed_checks"]
    if not isinstance(failed_checks, list) or len(failed_checks) > MAX_DIAGNOSTICS:
        raise ValidationContractError("invalid_contract", f"{path}.failed_checks must be a bounded list", path=f"{path}.failed_checks")
    return {
        "code": _string(value["code"], f"{path}.code", maximum=128, pattern=SAFE_CODE),
        "message": _safe_diagnostic_text(value["message"], f"{path}.message", maximum=1000),
        "failed_checks": [
            _string(item, f"{path}.failed_checks[{index}]", maximum=128)
            for index, item in enumerate(failed_checks)
        ],
        "cleanup_code": _optional_string(value["cleanup_code"], f"{path}.cleanup_code", maximum=128),
    }


def _normalize_result_checks(value, *, path: str) -> list[dict]:
    if not isinstance(value, list) or not value or len(value) > 256:
        raise ValidationContractError("invalid_contract", f"{path} must be a non-empty bounded list", path=path)
    normalized = []
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        row = _object(row, {"name", "status", "error"}, row_path)
        if row["status"] not in {"success", "failed"}:
            raise ValidationContractError("invalid_contract", f"{row_path}.status is invalid", path=f"{row_path}.status")
        normalized.append({
            "name": _string(row["name"], f"{row_path}.name", maximum=256),
            "status": row["status"],
            "error": _optional_string(row["error"], f"{row_path}.error", maximum=1000),
        })
    return normalized


def _normalize_result_commands(value, *, path: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationContractError("invalid_contract", f"{path} must be a bounded list", path=path)
    normalized = []
    for index, row in enumerate(value):
        row_path = f"{path}[{index}]"
        row = _object(row, {"command", "arguments", "status", "exit_code", "accepted", "stdout", "stderr"}, row_path)
        arguments = row["arguments"]
        if not isinstance(arguments, list) or len(arguments) > 128:
            raise ValidationContractError("invalid_contract", f"{row_path}.arguments must be a bounded list", path=f"{row_path}.arguments")
        if row["status"] not in {"success", "failed", "cancelled"}:
            raise ValidationContractError("invalid_contract", f"{row_path}.status is invalid", path=f"{row_path}.status")
        exit_code = row["exit_code"]
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int) or abs(exit_code) > MAX_SAFE_INTEGER):
            raise ValidationContractError("invalid_contract", f"{row_path}.exit_code is invalid", path=f"{row_path}.exit_code")
        if row["accepted"] is not None and not isinstance(row["accepted"], bool):
            raise ValidationContractError("invalid_contract", f"{row_path}.accepted is invalid", path=f"{row_path}.accepted")
        normalized.append({
            "command": _optional_printable(row["command"], f"{row_path}.command", maximum=1000),
            "arguments": [_optional_printable(item, f"{row_path}.arguments[{offset}]", maximum=1000) for offset, item in enumerate(arguments)],
            "status": row["status"],
            "exit_code": exit_code,
            "accepted": row["accepted"],
            "stdout": _optional_printable(row["stdout"], f"{row_path}.stdout", maximum=4096),
            "stderr": _optional_printable(row["stderr"], f"{row_path}.stderr", maximum=4096),
        })
    return normalized


def normalize_validation_result(value) -> dict:
    """Normalize the strict current-v1 detailed lifecycle result."""
    path = "$"
    if not isinstance(value, dict):
        raise ValidationContractError("invalid_contract", "Validation result must be an object")
    version = value.get("contract_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationContractError("invalid_contract_version", "Validation result contract version must be an integer", path="$.contract_version")
    if version > VALIDATION_RESULT_CONTRACT_VERSION:
        raise ValidationContractError("future_contract_version", "Validation result contract is newer than supported", path="$.contract_version")
    if version != VALIDATION_RESULT_CONTRACT_VERSION:
        raise ValidationContractError("unsupported_contract_version", "Validation result contract version is unsupported", path="$.contract_version")
    value = _object(value, {
        "contract_version", "attempt_id", "build_run_id", "artifact", "profile", "status",
        "started_at", "finished_at", "checks", "execution", "commands", "error",
    }, path)
    attempt_id = _string(value["attempt_id"], "$.attempt_id", maximum=128, pattern=SAFE_ID)
    build_run_id = _string(value["build_run_id"], "$.build_run_id", maximum=128, pattern=SAFE_ID)
    artifact = normalize_artifact_identity(value["artifact"], path="$.artifact")
    profile = _object(value["profile"], {"name", "image"}, "$.profile")
    profile_name = _string(profile["name"], "$.profile.name", maximum=128, pattern=SAFE_ID)
    image = _normalize_prepared_image_identity(profile["image"], path="$.profile.image")
    status = value["status"]
    if status not in {"success", "failed", "cancelled"}:
        raise ValidationContractError("invalid_contract", "Validation result status is invalid", path="$.status")
    started_at, started = _timestamp(value["started_at"], "$.started_at")
    finished_at, finished = _timestamp(value["finished_at"], "$.finished_at")
    if finished < started:
        raise ValidationContractError("invalid_contract", "Validation result timestamps are out of order", path="$.finished_at")
    checks = _normalize_result_checks(value["checks"], path="$.checks")
    execution = _object(value["execution"], {"network", "network_verified", "cleanup"}, "$.execution")
    if execution["network"] not in {"disabled", "unverified"} or not isinstance(execution["network_verified"], bool):
        raise ValidationContractError("invalid_contract", "Validation network evidence is invalid", path="$.execution")
    cleanup = _object(execution["cleanup"], {"status", "absence_proved"}, "$.execution.cleanup")
    if cleanup["status"] not in {"success", "unresolved"} or not isinstance(cleanup["absence_proved"], bool):
        raise ValidationContractError("invalid_contract", "Validation cleanup evidence is invalid", path="$.execution.cleanup")
    error = _normalize_result_error(value["error"], path="$.error")
    successful_check_counts = {
        name: sum(row["name"] == name and row["status"] == "success" for row in checks)
        for name in REQUIRED_SUCCESS_CHECKS
    }
    if status == "success" and (
        error is not None or execution["network"] != "disabled" or execution["network_verified"] is not True
        or cleanup != {"status": "success", "absence_proved": True}
        or any(row["status"] != "success" for row in checks)
        or any(count != 1 for count in successful_check_counts.values())
    ):
        raise ValidationContractError("invalid_contract", "Successful Validation lacks complete lifecycle proof", path="$.status")
    if status in {"failed", "cancelled"} and error is None:
        raise ValidationContractError("invalid_contract", "Unsuccessful Validation requires a bounded error", path="$.error")
    if cleanup["absence_proved"] is not True:
        raise ValidationContractError("invalid_contract", "Terminal Validation result requires OCI absence proof", path="$.execution.cleanup")
    return {
        "contract_version": version,
        "attempt_id": attempt_id,
        "build_run_id": build_run_id,
        "artifact": artifact,
        "profile": {"name": profile_name, "image": image},
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "checks": checks,
        "execution": {
            "network": execution["network"],
            "network_verified": execution["network_verified"],
            "cleanup": {"status": cleanup["status"], "absence_proved": cleanup["absence_proved"]},
        },
        "commands": _normalize_result_commands(value["commands"], path="$.commands"),
        "error": error,
    }
