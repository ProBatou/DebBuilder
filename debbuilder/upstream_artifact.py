"""Acquire and validate opaque Debian artifacts published by GitHub releases."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from . import deb_inspector, github_client
from .recipe_schema import normalize_github_version


class UpstreamArtifactError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


ARCH_MARKERS = {
    "amd64": ("amd64", "x86_64", "x64"),
    "arm64": ("arm64", "aarch64"),
    "armhf": ("armhf", "armv7"),
    "all": ("all",),
}


def resolve_release(recipe: dict, *, token: str = "") -> dict:
    source = recipe["source"]
    if source["tracking"] != "latest_release":
        raise UpstreamArtifactError("unsupported_artifact_tracking", "Upstream Debian artifacts currently require latest_release tracking")
    try:
        release = github_client.latest_release(source["repository"], token=token)
        upstream = normalize_github_version(release["tag"])
    except (github_client.GitHubError, ValueError) as exc:
        raise UpstreamArtifactError(getattr(exc, "code", "invalid_release_version"), str(exc)) from exc
    return {**release, "repository": source["repository"], "upstream_version": upstream, "ref": release["tag"]}


def select_asset(release: dict, config: dict) -> dict:
    architecture = config["architecture"]
    pattern = config.get("name_pattern", "")
    candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".deb"):
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        lowered = name.lower()
        if architecture != "all" and not any(marker in lowered for marker in ARCH_MARKERS[architecture]):
            continue
        candidates.append(asset)
    if not candidates:
        raise UpstreamArtifactError("release_asset_not_found", "No deterministic Debian release asset matches the Recipe", details={"architecture": architecture, "name_pattern": pattern})
    if len(candidates) != 1:
        raise UpstreamArtifactError("ambiguous_release_asset", "Multiple Debian release assets match the Recipe", details={"assets": [row["name"] for row in candidates]})
    return candidates[0]


def _expected_digest(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    return digest.split(":", 1)[1].lower() if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) else ""


def acquire(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive, inspector=deb_inspector.inspect_deb) -> dict:
    root = Path(workspace).resolve()
    artifacts = (root / "artifacts").resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    release = release_resolver(recipe, token=token)
    selected = select_asset(release, recipe["artifact"])
    filename = Path(selected["name"]).name
    if filename != selected["name"] or not filename.endswith(".deb"):
        raise UpstreamArtifactError("unsafe_release_asset_name", "Release asset has an unsafe filename")
    destination = artifacts / filename
    try:
        downloaded = downloader(selected["url"], destination, token=token)
        info = inspector(destination, workspace=root)
    except github_client.GitHubError as exc:
        raise UpstreamArtifactError(exc.code, str(exc)) from exc
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise UpstreamArtifactError("artifact_inspection_failed", f"Downloaded Debian artifact could not be inspected: {exc}") from exc
    expected_sha = _expected_digest(selected)
    if expected_sha and downloaded["sha256"].lower() != expected_sha:
        destination.unlink(missing_ok=True)
        raise UpstreamArtifactError("artifact_checksum_mismatch", "Downloaded artifact does not match the upstream SHA-256")
    expected_package = recipe["package"]["name"]
    expected_arch = recipe["artifact"]["architecture"]
    upstream = release["upstream_version"]
    if recipe["artifact"]["match_package"] and info.get("package") != expected_package:
        raise UpstreamArtifactError("artifact_package_mismatch", "Artifact Package does not match the Recipe", details={"expected": expected_package, "actual": info.get("package")})
    if info.get("architecture") != expected_arch:
        raise UpstreamArtifactError("artifact_architecture_mismatch", "Artifact Architecture does not match the Recipe", details={"expected": expected_arch, "actual": info.get("architecture")})
    if recipe["artifact"]["match_version"] and not (info.get("version") == upstream or str(info.get("version", "")).startswith(upstream + "-")):
        raise UpstreamArtifactError("artifact_version_mismatch", "Artifact Version does not correspond to the GitHub release", details={"release": upstream, "actual": info.get("version")})
    return {
        "path": str(destination), "name": filename, "size": downloaded["size"],
        "sha256": downloaded["sha256"], "downloaded_sha256": downloaded["sha256"],
        "upstream_expected_sha256": expected_sha, "checksum_verified": bool(expected_sha),
        "source": "upstream_release", "release": release, "release_asset": selected,
        "inspection": deb_inspector.inspection_for_storage(info),
    }
