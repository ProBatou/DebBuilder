"""Acquire selected files from a safely extracted GitHub release archive."""
from __future__ import annotations

import fnmatch
import hashlib
import re
import shutil
import stat
import tempfile
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


def resolve_release(recipe: dict, *, token: str = "") -> dict:
    source = recipe["source"]
    if source["tracking"] == "latest_release":
        return upstream_artifact.resolve_release(recipe, token=token)
    if recipe["artifact"].get("archive_source") == "release_asset":
        raise UpstreamArchiveError("unsupported_artifact_tracking", "Release asset archives require latest_release tracking")
    try:
        resolved = source_acquisition.resolve_source(recipe, token=token)
    except source_acquisition.SourceError as exc:
        raise UpstreamArchiveError(exc.code, str(exc)) from exc
    return {
        "repository": resolved["repository"],
        "tag": resolved["tag"],
        "ref": resolved["ref"],
        "name": resolved["release_name"],
        "url": resolved["release_url"],
        "upstream_version": resolved["upstream_version"],
        "archive_url": resolved["archive_url"],
        "tarball_url": resolved["archive_url"],
        "zipball_url": resolved["archive_url"].replace("/tarball/", "/zipball/"),
        "assets": [],
    }


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


def source_archive_candidate(release: dict, archive_format_name: str = "tar.gz") -> dict:
    kind = "zip" if archive_format_name == "zip" else "tar.gz"
    url = str(release.get("zipball_url") if kind == "zip" else release.get("tarball_url") or release.get("archive_url") or "")
    if not url:
        raise UpstreamArchiveError("source_archive_not_found", "GitHub source archive URL is not available")
    try:
        github_client.validate_download_url(url)
    except github_client.GitHubError as exc:
        raise UpstreamArchiveError(exc.code, str(exc)) from exc
    suffix = "zip" if kind == "zip" else "tar.gz"
    ref = re.sub(r"[^A-Za-z0-9_.+-]", "-", str(release.get("tag") or release.get("ref") or "source")).strip("-") or "source"
    return {
        "source": "github_source",
        "name": f"source-{ref}.{suffix}",
        "url": url,
        "size": 0,
        "digest": "",
        "archive_format": kind,
    }


def release_asset_candidates(release: dict, config: dict) -> list[dict]:
    exact = str(config.get("asset_name") or "")
    pattern = str(config.get("name_pattern") or "")
    rows = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        try:
            kind = archive_format(name)
        except UpstreamArchiveError:
            continue
        if exact and name != exact:
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        rows.append({**asset, "source": "release_asset", "archive_format": kind})
    return rows


def available_archive_sources(release: dict, config: dict) -> list[dict]:
    sources = [source_archive_candidate(release, str(config.get("archive_format") or "tar.gz"))]
    sources.extend(release_asset_candidates(release, config))
    return sources


def select_archive(release: dict, config: dict) -> dict:
    source = str(config.get("archive_source") or ("release_asset" if config.get("asset_name") or config.get("name_pattern") else "auto"))
    if source == "github_source":
        return source_archive_candidate(release, str(config.get("archive_format") or "tar.gz"))
    if source == "release_asset":
        selected = select_asset(release, config)
        return {**selected, "source": "release_asset", "archive_format": archive_format(str(selected.get("name") or ""))}
    candidates = available_archive_sources(release, config)
    asset_selectors = bool(config.get("asset_name") or config.get("name_pattern"))
    if asset_selectors:
        assets = [row for row in candidates if row["source"] == "release_asset"]
        if len(assets) == 1:
            return assets[0]
    asset_count = sum(1 for row in candidates if row["source"] == "release_asset")
    if asset_count:
        raise UpstreamArchiveError("ambiguous_archive_source", "Release has both GitHub source archives and release assets; choose an archive source explicitly", details={"sources": archive_source_options(release, config)})
    return candidates[0]


def archive_source_options(release: dict, config: dict) -> list[dict]:
    options = [source_archive_candidate(release, "tar.gz"), source_archive_candidate(release, "zip")]
    options.extend(release_asset_candidates(release, config))
    return [{"source": row["source"], "name": row["name"], "archive_format": row["archive_format"], "size": row.get("size", 0)} for row in options]


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


def list_extracted_files(source: str | Path, *, limit: int = 2000) -> list[dict]:
    root = Path(source).resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        if len(rows) >= limit:
            break
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise UpstreamArchiveError("archive_inspection_failed", "Extracted file escapes archive root") from exc
        rows.append({"relative_path": relative, "size": path.stat().st_size, "mode": f"{path.stat().st_mode & 0o777:04o}"})
    return rows


def _expected_digest(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    return digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") and len(digest) == 71 else ""


def resolve_and_extract(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    root = Path(workspace).resolve()
    release = release_resolver(recipe, token=token)
    selected_asset = select_archive(release, recipe["artifact"])
    name = selected_asset["name"]
    kind = selected_asset.get("archive_format") or archive_format(name)
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
    return {
        "repository": release["repository"], "strategy": recipe["source"]["tracking"], "ref": release["ref"], "tag": release["tag"],
        "release_name": release.get("name", ""), "release_url": release.get("url", ""), "upstream_version": release["upstream_version"],
        "debian_version": f"{release['upstream_version']}-{recipe['package']['version_revision']}" if recipe["package"]["version_revision"] else release["upstream_version"],
        "source_directory": str(source), "artifact_mode": "upstream_archive", "asset": {"name": name, "url": selected_asset["url"], "source": selected_asset.get("source", "release_asset"), "declared_size": selected_asset.get("size", 0), "download_size": download["size"], "sha256": actual, "expected_sha256": expected, "checksum_verified": bool(expected), "archive_format": kind},
        "extraction": extraction,
    }


def selected_file_records(recipe: dict, source_directory: str | Path) -> list[dict]:
    source = Path(source_directory).resolve()
    selected_files = []
    for relative in recipe["artifact"]["selected_files"]:
        target = (source / relative).resolve(strict=False)
        try:
            target.relative_to(source)
        except ValueError as exc:
            raise UpstreamArchiveError("unsafe_selected_file", f"Selected archive file escapes extraction root: {relative}") from exc
        if target.is_symlink() or not target.is_file():
            raise UpstreamArchiveError("selected_file_not_found", f"Selected file is missing from release archive: {relative}")
        selected_files.append({"relative_path": relative, "path": str(target), "size": target.stat().st_size, "mode": f"{target.stat().st_mode & 0o777:04o}"})
    return selected_files


def acquire(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    result = resolve_and_extract(recipe, workspace, token=token, release_resolver=release_resolver, downloader=downloader)
    return {**result, "selected_files": selected_file_records(recipe, result["source_directory"])}


def inspect(recipe: dict, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    with tempfile.TemporaryDirectory(prefix="debbuilder-archive-inspect-") as temporary:
        try:
            result = resolve_and_extract(recipe, temporary, token=token, release_resolver=release_resolver, downloader=downloader)
        except UpstreamArchiveError as exc:
            if exc.code in {"ambiguous_archive_source", "ambiguous_release_asset"}:
                release = release_resolver(recipe, token=token)
                exc.details.setdefault("sources", archive_source_options(release, recipe["artifact"]))
            raise
        selected = selected_file_records(recipe, result["source_directory"]) if recipe["artifact"].get("selected_files") else []
        return {
            "source": result["asset"],
            "release": {key: result.get(key, "") for key in ("repository", "ref", "tag", "release_name", "release_url", "upstream_version", "debian_version")},
            "extraction": result["extraction"],
            "files": list_extracted_files(result["source_directory"]),
            "selected_files": selected,
        }
