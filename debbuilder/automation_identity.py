"""Bounded, exact upstream identity used only by durable automation coordination."""
from __future__ import annotations

import hashlib
import json
import re


IDENTITY_SCHEMA_VERSION = 1
ATTEMPT_KEY_SCHEMA_VERSION = 1
MAX_IDENTITY_BYTES = 4096
MAX_TEXT = 200
FIELDS = {
    "schema_version", "provider", "repository", "tracking", "source_type",
    "payload_kind", "requested_ref", "release_id", "asset_id", "commit_sha",
    "resolved_ref", "resolved_version", "content_sha256", "completeness",
    "missing_fields", "asset_name", "expected_size", "source_archive_format",
    "expected_package", "expected_architecture", "ref_object_sha",
}
TRACKING = {"latest_release", "tag", "manual"}
SOURCE_TYPES = {"release_asset", "github_source_archive", "git_ref"}
PAYLOAD_KINDS = {"deb", "archive", "raw_file", "source_archive"}


class UpstreamIdentityError(ValueError):
    """The identity is not a safe canonical automation identity."""

    def __init__(self, code: str, message: str, *, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.path = path


def _text(value, field: str, *, required: bool = False, maximum: int = MAX_TEXT) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise UpstreamIdentityError("invalid_upstream_identity", f"{field} must be a string", path=f"$.{field}")
    result = str(value).strip()
    if (required and not result) or len(result) > maximum or any(ord(character) < 32 for character in result):
        raise UpstreamIdentityError("invalid_upstream_identity", f"{field} is invalid or unbounded", path=f"$.{field}")
    return result


def _sha(value, field: str, *, lengths: tuple[int, ...]) -> str:
    result = _text(value, field, maximum=max(lengths)).lower()
    if result and (len(result) not in lengths or not re.fullmatch(r"[0-9a-f]+", result)):
        raise UpstreamIdentityError("invalid_upstream_identity", f"{field} must be a hexadecimal digest", path=f"$.{field}")
    return result


def _github_ref(value, field: str) -> str:
    result = _text(value, field)
    if result and (
        result.startswith("/") or result.endswith("/") or ".." in result
        or "://" in result or "\\" in result or "@" in result or "=" in result
        or any(character in "~^:?*[" for character in result)
        or any(character.isspace() for character in result)
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", f"{field} is not a safe GitHub ref", path=f"$.{field}")
    return result


def _version(value) -> str:
    result = _text(value, "resolved_version")
    if result and (
        "/" in result or "\\" in result or "://" in result or "@" in result
        or "=" in result or any(character.isspace() for character in result)
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "resolved_version contains a URL or path", path="$.resolved_version")
    return result


def _required_fields(identity: dict) -> tuple[str, ...]:
    common = ("resolved_ref",)
    if identity["source_type"] == "release_asset":
        required = common + ("release_id", "asset_id", "asset_name")
        if identity["payload_kind"] == "deb":
            required += ("expected_package", "expected_architecture")
        return required
    if identity["source_type"] == "github_source_archive":
        return common + ("release_id", "commit_sha", "ref_object_sha", "source_archive_format")
    required = common + ("requested_ref", "commit_sha", "source_archive_format")
    return required + (("ref_object_sha",) if identity["tracking"] == "tag" else ())


def _size(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 500 * 1024 * 1024:
        raise UpstreamIdentityError(
            "invalid_upstream_identity", "expected_size must be a bounded non-negative integer",
            path="$.expected_size",
        )
    return value


def normalize_upstream_identity(value: dict) -> dict:
    """Validate and canonicalize one exact GitHub upstream identity.

    Completeness is derived from immutable identifiers, never trusted from a
    caller.  In particular a display/package version can never make an
    identity complete.
    """
    if not isinstance(value, dict):
        raise UpstreamIdentityError("invalid_upstream_identity", "Upstream identity must be an object")
    unknown = sorted(set(value) - FIELDS)
    if unknown:
        raise UpstreamIdentityError(
            "unsafe_upstream_identity_field",
            f"Unsupported upstream identity field: {unknown[0]}",
            path=f"$.{unknown[0]}",
        )
    version = value.get("schema_version", IDENTITY_SCHEMA_VERSION)
    if version != IDENTITY_SCHEMA_VERSION or isinstance(version, bool):
        code = "future_upstream_identity_version" if isinstance(version, int) and version > IDENTITY_SCHEMA_VERSION else "invalid_upstream_identity_version"
        raise UpstreamIdentityError(code, "Unsupported upstream identity schema version", path="$.schema_version")
    provider = _text(value.get("provider"), "provider", required=True, maximum=32).lower()
    if provider != "github":
        raise UpstreamIdentityError("invalid_upstream_identity", "Unsupported upstream provider", path="$.provider")
    repository = _text(value.get("repository"), "repository", required=True).lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise UpstreamIdentityError("invalid_upstream_identity", "repository must be canonical owner/name", path="$.repository")
    tracking = _text(value.get("tracking"), "tracking", required=True, maximum=32)
    source_type = _text(value.get("source_type"), "source_type", required=True, maximum=32)
    payload_kind = _text(value.get("payload_kind"), "payload_kind", required=True, maximum=32)
    if tracking not in TRACKING or source_type not in SOURCE_TYPES or payload_kind not in PAYLOAD_KINDS:
        raise UpstreamIdentityError("invalid_upstream_identity", "Unsupported tracking/source/payload identity", path="$")
    if tracking == "latest_release" and source_type not in {"release_asset", "github_source_archive"}:
        raise UpstreamIdentityError("invalid_upstream_identity", "Latest-release tracking requires a release identity", path="$.source_type")
    if tracking in {"tag", "manual"} and source_type != "git_ref":
        raise UpstreamIdentityError("invalid_upstream_identity", "Explicit ref tracking requires a Git ref identity", path="$.source_type")
    if source_type == "release_asset" and payload_kind not in {"deb", "archive", "raw_file"}:
        raise UpstreamIdentityError("invalid_upstream_identity", "Release asset payload kind must be deb, archive, or raw_file", path="$.payload_kind")
    if source_type == "github_source_archive" and payload_kind != "source_archive":
        raise UpstreamIdentityError("invalid_upstream_identity", "Generated GitHub source payload must be source_archive", path="$.payload_kind")
    if source_type == "git_ref" and payload_kind != "source_archive":
        raise UpstreamIdentityError("invalid_upstream_identity", "Git ref payload must be a source archive", path="$.payload_kind")
    normalized = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "provider": provider,
        "repository": repository,
        "tracking": tracking,
        "source_type": source_type,
        "payload_kind": payload_kind,
        "requested_ref": _github_ref(value.get("requested_ref"), "requested_ref"),
        "release_id": _text(value.get("release_id"), "release_id", maximum=40),
        "asset_id": _text(value.get("asset_id"), "asset_id", maximum=40),
        "commit_sha": _sha(value.get("commit_sha"), "commit_sha", lengths=(40, 64)),
        "ref_object_sha": _sha(value.get("ref_object_sha"), "ref_object_sha", lengths=(40, 64)),
        "resolved_ref": _github_ref(value.get("resolved_ref"), "resolved_ref"),
        "resolved_version": _version(value.get("resolved_version")),
        "content_sha256": _sha(value.get("content_sha256"), "content_sha256", lengths=(64,)),
        "asset_name": _text(value.get("asset_name"), "asset_name"),
        "expected_size": _size(value.get("expected_size")),
        "source_archive_format": _text(value.get("source_archive_format"), "source_archive_format", maximum=16),
        "expected_package": _text(value.get("expected_package"), "expected_package"),
        "expected_architecture": _text(value.get("expected_architecture"), "expected_architecture", maximum=16),
    }
    for field in ("release_id", "asset_id"):
        if normalized[field] and not normalized[field].isdigit():
            raise UpstreamIdentityError("invalid_upstream_identity", f"{field} must be a GitHub numeric ID", path=f"$.{field}")
    if normalized["source_archive_format"] and normalized["source_archive_format"] not in {"tar.gz", "zip"}:
        raise UpstreamIdentityError("invalid_upstream_identity", "Unsupported source archive format", path="$.source_archive_format")
    if normalized["expected_architecture"] and normalized["expected_architecture"] not in {"all", "amd64", "arm64", "armhf"}:
        raise UpstreamIdentityError("invalid_upstream_identity", "Unsupported expected architecture", path="$.expected_architecture")
    if source_type == "release_asset" and (
        normalized["requested_ref"] or normalized["commit_sha"] or normalized["ref_object_sha"] or normalized["source_archive_format"]
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "Release asset identity contains irrelevant Git ref fields", path="$")
    if source_type == "github_source_archive" and (
        normalized["requested_ref"] or normalized["asset_id"] or normalized["asset_name"]
        or normalized["expected_size"] is not None or normalized["expected_package"]
        or normalized["expected_architecture"]
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "Generated source archive contains irrelevant selector fields", path="$")
    if source_type == "git_ref" and (
        normalized["release_id"] or normalized["asset_id"] or normalized["asset_name"]
        or normalized["expected_size"] is not None or normalized["expected_package"]
        or normalized["expected_architecture"] or normalized["content_sha256"]
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "Git ref identities cannot carry release asset IDs", path="$")
    if source_type == "git_ref" and tracking == "manual" and normalized["ref_object_sha"]:
        raise UpstreamIdentityError("invalid_upstream_identity", "Manual Git refs cannot carry tag object identity", path="$.ref_object_sha")
    if source_type == "release_asset" and payload_kind != "deb" and (
        normalized["expected_package"] or normalized["expected_architecture"]
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "Only Debian assets carry package selectors", path="$")
    if source_type == "release_asset" and not normalized["asset_name"]:
        # Kept in missing_fields below so callers can return a stable incomplete
        # classification rather than accidentally claiming an unnamed asset.
        pass
    if normalized["asset_name"] and (
        normalized["asset_name"] in {".", ".."} or "/" in normalized["asset_name"]
        or "\\" in normalized["asset_name"]
    ):
        raise UpstreamIdentityError("invalid_upstream_identity", "asset_name is unsafe", path="$.asset_name")
    missing = tuple(field for field in _required_fields(normalized) if not normalized[field])
    completeness = "complete" if not missing else "partial"
    if "completeness" in value and value["completeness"] != completeness:
        raise UpstreamIdentityError(
            "upstream_identity_completeness_mismatch",
            "Declared identity completeness does not match its immutable identifiers",
            path="$.completeness",
        )
    if "missing_fields" in value:
        declared_missing = value["missing_fields"]
        if not isinstance(declared_missing, list) or any(not isinstance(item, str) for item in declared_missing) or tuple(declared_missing) != missing:
            raise UpstreamIdentityError(
                "upstream_identity_completeness_mismatch",
                "Declared missing_fields do not match the identity",
                path="$.missing_fields",
            )
    normalized.update({"completeness": completeness, "missing_fields": list(missing)})
    if len(canonical_identity_bytes(normalized, _normalized=True)) > MAX_IDENTITY_BYTES:
        raise UpstreamIdentityError("upstream_identity_too_large", "Upstream identity is too large")
    return normalized


def canonical_identity_bytes(value: dict, *, _normalized: bool = False) -> bytes:
    normalized = value if _normalized else normalize_upstream_identity(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_identity_json(value: dict) -> str:
    return canonical_identity_bytes(value).decode("utf-8")


def identity_is_complete(value: dict) -> bool:
    return normalize_upstream_identity(value)["completeness"] == "complete"


def verify_expected_upstream_identity(expected: dict | None, actual: dict) -> dict:
    """Fail closed when execution no longer resolves the admitted identity."""
    if expected is None:
        return normalize_upstream_identity(actual) if actual else {}
    try:
        normalized = normalize_upstream_identity(actual)
    except UpstreamIdentityError as exc:
        if exc.code == "upstream_identity_completeness_mismatch":
            raise UpstreamIdentityError(
                "upstream_identity_changed", "Upstream identity changed before execution",
                path="$.upstream_identity",
            ) from exc
        raise
    wanted = normalize_upstream_identity(expected)
    if wanted["completeness"] != "complete" or normalized["completeness"] != "complete":
        raise UpstreamIdentityError(
            "incomplete_upstream_identity", "Upstream identity is incomplete for pinned execution",
            path="$.upstream_identity",
        )
    if canonical_identity_bytes(wanted, _normalized=True) != canonical_identity_bytes(normalized, _normalized=True):
        raise UpstreamIdentityError(
            "upstream_identity_changed", "Upstream identity changed before execution",
            path="$.upstream_identity",
        )
    return normalized


def verify_acquired_payload(identity: dict, *, size: int, sha256: str) -> None:
    """Verify byte evidence without pretending it was known during detection."""
    normalized = normalize_upstream_identity(identity)
    if normalized["source_type"] != "release_asset":
        return
    if normalized["expected_size"] is not None and size != normalized["expected_size"]:
        raise UpstreamIdentityError(
            "upstream_payload_size_mismatch", "Downloaded payload size differs from detected metadata",
            path="$.expected_size",
        )
    expected = normalized["content_sha256"]
    if expected and sha256.lower() != expected:
        raise UpstreamIdentityError(
            "upstream_payload_checksum_mismatch", "Downloaded payload differs from the detected GitHub digest",
            path="$.content_sha256",
        )


def _github_digest(value) -> str:
    digest = str(value or "")
    return digest.split(":", 1)[1].lower() if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) else ""


def release_asset_identity(recipe: dict, release: dict, asset: dict, payload_kind: str) -> dict:
    """Build the sole canonical identity shape for a selected Release asset."""
    deb = payload_kind == "deb"
    return normalize_upstream_identity({
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "provider": "github",
        "repository": release.get("repository") or recipe["source"]["repository"],
        "tracking": "latest_release",
        "source_type": "release_asset",
        "payload_kind": payload_kind,
        "release_id": release.get("release_id"),
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("name"),
        "expected_size": asset.get("size") if type(asset.get("size")) is int else None,
        "resolved_ref": release.get("tag") or release.get("ref"),
        "resolved_version": release.get("upstream_version"),
        "content_sha256": _github_digest(asset.get("digest")),
        "expected_package": recipe["package"]["name"] if deb else "",
        "expected_architecture": recipe["artifact"]["architecture"] if deb else "",
    })


def source_archive_identity(recipe: dict, resolved: dict, *, generated_release: bool, archive_format: str = "tar.gz") -> dict:
    """Build the canonical generated-Release or explicit-ref source identity."""
    source = recipe["source"]
    common = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "provider": "github",
        "repository": resolved.get("repository") or source["repository"],
        "tracking": source["tracking"],
        "payload_kind": "source_archive",
        "commit_sha": resolved.get("commit"),
        "ref_object_sha": resolved.get("ref_object_sha") if generated_release or source["tracking"] == "tag" else "",
        "resolved_ref": resolved.get("tag") or resolved.get("ref"),
        "resolved_version": resolved.get("upstream_version"),
        "source_archive_format": archive_format,
    }
    if generated_release:
        common.update({
            "source_type": "github_source_archive",
            "release_id": resolved.get("release_id"),
        })
    else:
        common.update({
            "source_type": "git_ref",
            "requested_ref": source.get("ref"),
        })
    return normalize_upstream_identity(common)


def automation_attempt_key(recipe_id: str, identity: dict, recipe_sha256: str) -> str:
    """Bind exact upstream input to the canonical immutable Recipe snapshot."""
    if not isinstance(recipe_id, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", recipe_id):
        raise UpstreamIdentityError("invalid_attempt_recipe", "Recipe ID is invalid", path="$.recipe_id")
    if not isinstance(recipe_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", recipe_sha256):
        raise UpstreamIdentityError("invalid_attempt_recipe_sha256", "Recipe SHA-256 is invalid", path="$.recipe_sha256")
    normalized = normalize_upstream_identity(identity)
    if normalized["completeness"] != "complete":
        raise UpstreamIdentityError(
            "incomplete_upstream_identity",
            "A partial upstream identity cannot form a durable automation claim",
            path="$.upstream_identity",
        )
    payload = {
        "schema_version": ATTEMPT_KEY_SCHEMA_VERSION,
        "recipe_id": recipe_id,
        "recipe_sha256": recipe_sha256.lower(),
        "upstream_identity": normalized,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"automation-v{ATTEMPT_KEY_SCHEMA_VERSION}-{hashlib.sha256(encoded).hexdigest()}"
