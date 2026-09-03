"""Acquire selected files from a safely extracted GitHub release archive."""
from __future__ import annotations

import fnmatch
import hashlib
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from . import github_client, source_acquisition, upstream_artifact


class UpstreamArchiveError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def archive_format(name: str) -> str:
    lowered = name.lower()
    for suffix, kind in ((".tar.gz", "tar.gz"), (".tgz", "tgz"), (".tar.xz", "tar.xz"), (".zip", "zip")):
        if lowered.endswith(suffix):
            return kind
    raise UpstreamArchiveError("unsupported_archive_format", f"Unsupported release archive format: {name}")


def select_asset(release: dict, config: dict) -> dict:
    exact = str(config.get("asset_name") or "")
    pattern = str(config.get("name_pattern") or "")
    candidates = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        try:
            archive_format(name)
        except UpstreamArchiveError:
            continue
        if exact and name != exact:
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        candidates.append(asset)
    if not candidates:
        raise UpstreamArchiveError("release_asset_not_found", "No release archive matches the Recipe", details={"asset_name": exact, "name_pattern": pattern})
    if len(candidates) != 1:
        raise UpstreamArchiveError("ambiguous_release_asset", "Multiple release archives match the Recipe", details={"assets": [row["name"] for row in candidates]})
    return candidates[0]


def _safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if path.is_absolute() or not parts or ".." in parts:
        raise UpstreamArchiveError("archive_extract_failed", f"Unsafe path in release archive: {name}")
    return parts


def extract_zip_archive(archive: str | Path, destination: str | Path, *, max_members: int = 100_000, max_uncompressed_bytes: int = 1024 * 1024 * 1024) -> dict:
    target = Path(destination).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise UpstreamArchiveError("archive_extract_failed", "Archive destination is not empty")
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or len(members) > max_members:
                raise UpstreamArchiveError("archive_extract_failed", "Release archive is empty or contains too many entries")
            parsed = [(member, _safe_parts(member.filename)) for member in members]
            total = sum(member.file_size for member, _ in parsed if not member.is_dir())
            if total > max_uncompressed_bytes:
                raise UpstreamArchiveError("archive_extract_failed", "Expanded release archive exceeds the configured size limit")
            roots = {parts[0] for _, parts in parsed}
            strip_root = len(roots) == 1 and any(len(parts) >= 2 for _, parts in parsed)
            extracted = 0
            for member, parts in parsed:
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if member.flag_bits & 1 or stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise UpstreamArchiveError("archive_extract_failed", f"Unsupported encrypted, link, or special entry in release archive: {member.filename}")
                relative = parts[1:] if strip_root else parts
                if not relative:
                    continue
                output = target.joinpath(*relative).resolve()
                try:
                    output.relative_to(target)
                except ValueError as exc:
                    raise UpstreamArchiveError("archive_extract_failed", f"Unsafe path in release archive: {member.filename}") from exc
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, output.open("xb") as handle:
                        shutil.copyfileobj(source, handle)
                    output.chmod(0o755 if mode & 0o111 else 0o644)
                    extracted += 1
            return {"files": extracted, "uncompressed_size": total, "stripped_root": next(iter(roots)) if strip_root else ""}
    except UpstreamArchiveError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpstreamArchiveError("archive_extract_failed", "Release ZIP extraction failed") from exc


def _expected_digest(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    return digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") and len(digest) == 71 else ""


def acquire(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=upstream_artifact.resolve_release, downloader=github_client.download_archive) -> dict:
    root = Path(workspace).resolve()
    release = release_resolver(recipe, token=token)
    selected_asset = select_asset(release, recipe["artifact"])
    name = selected_asset["name"]
    kind = archive_format(name)
    downloads = root / "downloads"
    source = root / "source"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / Path(name).name
    if archive.name != name:
        raise UpstreamArchiveError("unsafe_release_asset_name", "Release archive has an unsafe filename")
    try:
        download = downloader(selected_asset["url"], archive, token=token)
        actual = str(download.get("sha256") or hashlib.sha256(archive.read_bytes()).hexdigest()).lower()
        expected = _expected_digest(selected_asset)
        if expected and actual != expected:
            archive.unlink(missing_ok=True)
            raise UpstreamArchiveError("archive_checksum_mismatch", "Downloaded release archive does not match its GitHub SHA-256", details={"expected": expected, "actual": actual})
        if kind == "zip":
            extraction = extract_zip_archive(archive, source)
        else:
            extraction = source_acquisition.extract_tar_archive(archive, source)
    except github_client.GitHubError as exc:
        raise UpstreamArchiveError(exc.code, str(exc)) from exc
    except source_acquisition.SourceError as exc:
        raise UpstreamArchiveError("archive_extract_failed", str(exc)) from exc
    selected_files = []
    for relative in recipe["artifact"]["selected_files"]:
        target = (source / relative).resolve(strict=False)
        try:
            target.relative_to(source.resolve())
        except ValueError as exc:
            raise UpstreamArchiveError("unsafe_selected_file", f"Selected archive file escapes extraction root: {relative}") from exc
        if target.is_symlink() or not target.is_file():
            raise UpstreamArchiveError("selected_file_not_found", f"Selected file is missing from release archive: {relative}")
        selected_files.append({"relative_path": relative, "path": str(target), "size": target.stat().st_size, "mode": f"{target.stat().st_mode & 0o777:04o}"})
    return {
        "repository": release["repository"], "strategy": recipe["source"]["tracking"], "ref": release["ref"], "tag": release["tag"],
        "release_name": release.get("name", ""), "release_url": release.get("url", ""), "upstream_version": release["upstream_version"],
        "debian_version": f"{release['upstream_version']}-{recipe['package']['version_revision']}" if recipe["package"]["version_revision"] else release["upstream_version"],
        "source_directory": str(source), "artifact_mode": "upstream_archive", "asset": {"name": name, "url": selected_asset["url"], "declared_size": selected_asset.get("size", 0), "download_size": download["size"], "sha256": actual, "expected_sha256": expected, "checksum_verified": bool(expected), "archive_format": kind},
        "extraction": extraction, "selected_files": selected_files,
    }
