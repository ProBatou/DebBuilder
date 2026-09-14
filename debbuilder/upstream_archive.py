"""Acquire selected files from a safely extracted GitHub release archive."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import github_client, source_acquisition, upstream_artifact
from .archive_payload import parse_archive_path, selectors_match


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


def classify_payload(name: str) -> dict:
    """Classify a selected asset without excluding raw files from selection."""
    try:
        kind = archive_format(name)
    except UpstreamArchiveError as exc:
        if exc.code != "unsupported_archive_format":
            raise
        return {"payload_kind": "raw_file", "archive_format": ""}
    return {"payload_kind": "archive", "archive_format": kind}


def resolve_release(recipe: dict, *, token: str = "") -> dict:
    source = recipe["source"]
    if source["tracking"] == "latest_release":
        try:
            return upstream_artifact.resolve_release(recipe, token=token)
        except upstream_artifact.UpstreamArtifactError as exc:
            raise UpstreamArchiveError(exc.code, str(exc), details=exc.details) from exc
    if recipe["artifact"].get("archive_source") == "release_asset":
        raise UpstreamArchiveError("unsupported_artifact_tracking", "Release assets require latest_release tracking")
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
        if exact and name != exact:
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        candidates.append(asset)
    if not candidates:
        selector = f"exact name '{exact}'" if exact else f"pattern '{pattern}'"
        raise UpstreamArchiveError(
            "release_asset_not_found", f"No GitHub Release asset matches {selector}",
            details={"asset_name": exact, "name_pattern": pattern},
        )
    if len(candidates) != 1:
        raise UpstreamArchiveError(
            "ambiguous_release_asset", "Multiple GitHub Release assets match the Recipe",
            details={"assets": sorted(str(row.get("name") or "") for row in candidates)},
        )
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
        "payload_kind": "archive",
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
        rows.append({**asset, "source": "release_asset", "payload_kind": "archive", "archive_format": kind})
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
        return {**selected, "source": "release_asset", **classify_payload(str(selected.get("name") or ""))}
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


def release_asset_options(release: dict, config: dict) -> list[dict]:
    """Return all explicit selector matches without pre-filtering raw payloads."""
    exact = str(config.get("asset_name") or "")
    pattern = str(config.get("name_pattern") or "")
    options = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        if exact and name != exact:
            continue
        if pattern and not fnmatch.fnmatchcase(name, pattern):
            continue
        options.append({
            "source": "release_asset", "name": name, "size": asset.get("size", 0),
            **classify_payload(name),
        })
    return sorted(options, key=lambda row: (row["name"], row["size"]))


def _safe_asset_name(value: str) -> str:
    name = str(value or "")
    if (
        not name or name in {".", ".."} or Path(name).name != name or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise UpstreamArchiveError("unsafe_release_asset_name", "Release asset has an unsafe filename")
    return name


def _stable_identity_url(value: str) -> str:
    """Keep only stable, credential-free GitHub URLs in durable provenance."""
    url = str(value or "")
    parsed = urlparse(url)
    if parsed.query or parsed.fragment:
        return ""
    try:
        github_client.validate_download_url(url)
    except github_client.GitHubError:
        return ""
    return url


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fd_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_download(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
    except OSError:
        pass


def _source_directory_unchanged(source: Path, descriptor: int) -> None:
    try:
        current = source.lstat()
        anchored = os.fstat(descriptor)
    except OSError as exc:
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw source workspace changed during download") from exc
    if (
        stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (anchored.st_dev, anchored.st_ino)
    ):
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw source workspace changed during download")


def _raw_file_unchanged(name: str, directory_descriptor: int, file_descriptor: int) -> None:
    try:
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        anchored = os.fstat(file_descriptor)
    except OSError as exc:
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw Release asset changed during verification") from exc
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (anchored.st_dev, anchored.st_ino):
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw Release asset changed during verification")


def _open_verified_raw_file(source: Path, name: str, directory_descriptor: int) -> int:
    _source_directory_unchanged(source, directory_descriptor)
    descriptor = None
    try:
        if os.listdir(directory_descriptor) != [name]:
            raise UpstreamArchiveError("raw_file_verification_failed", "Raw Release asset did not produce exactly one source file")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
        anchored = os.fstat(descriptor)
    except UpstreamArchiveError:
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw Release asset is not a confined regular file") from exc
    if not stat.S_ISREG(anchored.st_mode):
        os.close(descriptor)
        raise UpstreamArchiveError("raw_file_verification_failed", "Raw Release asset is not a regular file")
    try:
        _raw_file_unchanged(name, directory_descriptor, descriptor)
        _source_directory_unchanged(source, directory_descriptor)
    except UpstreamArchiveError:
        os.close(descriptor)
        raise
    return descriptor


def _remove_raw_download(name: str, directory_descriptor: int | None) -> None:
    if directory_descriptor is None:
        return
    try:
        mode = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
            os.unlink(name, dir_fd=directory_descriptor)
    except OSError:
        pass


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


def inspect_inventory(source: str | Path) -> dict:
    """Return the complete typed logical inventory of an extracted archive."""
    root = Path(source).resolve()
    entries = []
    descendant_files: dict[str, int] = {}
    file_count = 0
    directory_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        logical = relative.as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise UpstreamArchiveError("archive_inspection_failed", f"Extracted archive contains a symbolic link: {logical}")
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            raise UpstreamArchiveError("archive_inspection_failed", f"Extracted archive contains a special entry: {logical}")
        canonical = logical + "/" if stat.S_ISDIR(mode) else logical
        try:
            parse_archive_path(canonical)
        except ValueError as exc:
            raise UpstreamArchiveError("archive_inspection_failed", f"Extracted archive contains an unsafe logical path: {logical}") from exc
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise UpstreamArchiveError("archive_inspection_failed", "Extracted entry escapes archive root") from exc
        if stat.S_ISDIR(mode):
            directory_count += 1
            entries.append({"path": logical + "/", "kind": "directory"})
            descendant_files[logical + "/"] = 0
            continue
        file_count += 1
        entries.append({"path": logical, "kind": "file", "size": path.stat().st_size, "mode": f"{mode & 0o777:04o}"})
        parts = relative.parts
        for depth in range(1, len(parts)):
            descendant_files[PurePosixPath(*parts[:depth]).as_posix() + "/"] = descendant_files.get(PurePosixPath(*parts[:depth]).as_posix() + "/", 0) + 1
    for entry in entries:
        if entry["kind"] == "directory":
            entry["descendant_files"] = descendant_files.get(entry["path"], 0)
    entries.sort(key=lambda entry: entry["path"])
    return {
        "entries": entries, "file_count": file_count, "directory_count": directory_count,
        "entry_count": len(entries), "complete": True,
    }


def list_extracted_files(source: str | Path, *, limit: int | None = None) -> list[dict]:
    """Compatibility view of the complete inventory; explicit truncation is rejected."""
    inventory = inspect_inventory(source)
    if limit is not None and limit < inventory["file_count"]:
        raise UpstreamArchiveError(
            "archive_inspection_incomplete", "Archive inspection limit would produce an incomplete inventory",
            details={"limit": limit, "file_count": inventory["file_count"], "complete": False},
        )
    return [
        {"relative_path": entry["path"], "size": entry["size"], "mode": entry["mode"]}
        for entry in inventory["entries"] if entry["kind"] == "file"
    ]


def _expected_digest(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    return digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") and len(digest) == 71 else ""


def resolve_and_extract(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    root = Path(workspace).resolve()
    release = release_resolver(recipe, token=token)
    selected_asset = select_archive(release, recipe["artifact"])
    name = _safe_asset_name(selected_asset["name"])
    payload_kind = str(selected_asset.get("payload_kind") or "archive")
    kind = str(selected_asset.get("archive_format") or "")
    downloads = root / "downloads"
    source = root / "source"
    workspace_error = "raw_file_verification_failed" if payload_kind == "raw_file" else "archive_extract_failed"
    try:
        source.mkdir(parents=True, exist_ok=True)
        if source.is_symlink() or not source.is_dir() or any(source.iterdir()):
            raise UpstreamArchiveError(workspace_error, "Source destination is not an empty regular directory")
        downloads.mkdir(parents=True, exist_ok=True)
    except UpstreamArchiveError:
        raise
    except OSError as exc:
        raise UpstreamArchiveError(workspace_error, "Source workspace could not be initialized safely") from exc
    downloaded_path = source / name if payload_kind == "raw_file" else downloads / name
    raw_directory_descriptor = None
    raw_file_descriptor = None
    raw_file_identity = None
    if payload_kind == "raw_file":
        try:
            raw_directory_descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise UpstreamArchiveError("raw_file_verification_failed", "Raw source workspace is not a regular directory") from exc
    try:
        download_destination = (
            Path(f"/proc/self/fd/{raw_directory_descriptor}") / name
            if raw_directory_descriptor is not None and downloader is github_client.download_archive
            else downloaded_path
        )
        download = downloader(selected_asset["url"], download_destination, token=token)
        if payload_kind == "raw_file":
            raw_file_descriptor = _open_verified_raw_file(source, name, raw_directory_descriptor)
            os.fchmod(raw_file_descriptor, 0o644)
            actual_size = os.fstat(raw_file_descriptor).st_size
        else:
            actual_size = downloaded_path.stat().st_size
        reported_size = download.get("size")
        if type(reported_size) is not int or reported_size != actual_size:
            _remove_raw_download(name, raw_directory_descriptor) if payload_kind == "raw_file" else _remove_download(downloaded_path)
            raise UpstreamArchiveError(
                "asset_size_mismatch", "Downloaded Release asset size does not match the received file",
                details={"reported": reported_size, "actual": actual_size, "asset": name},
            )
        declared_size = selected_asset.get("size")
        if selected_asset.get("source") == "release_asset" and type(declared_size) is int and declared_size != actual_size:
            _remove_raw_download(name, raw_directory_descriptor) if payload_kind == "raw_file" else _remove_download(downloaded_path)
            raise UpstreamArchiveError(
                "asset_size_mismatch", "Downloaded Release asset size does not match GitHub metadata",
                details={"declared": declared_size, "actual": actual_size, "asset": name},
            )
        actual = _fd_sha256(raw_file_descriptor) if raw_file_descriptor is not None else _file_sha256(downloaded_path)
        reported_sha256 = str(download.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", reported_sha256) or reported_sha256 != actual:
            _remove_raw_download(name, raw_directory_descriptor) if payload_kind == "raw_file" else _remove_download(downloaded_path)
            code = "raw_file_verification_failed" if payload_kind == "raw_file" else "archive_checksum_mismatch"
            raise UpstreamArchiveError(
                code, "Downloaded source content does not match the downloader SHA-256",
                details={"reported": reported_sha256, "actual": actual, "asset": name},
            )
        if raw_directory_descriptor is not None:
            _raw_file_unchanged(name, raw_directory_descriptor, raw_file_descriptor)
            _source_directory_unchanged(source, raw_directory_descriptor)
        expected = _expected_digest(selected_asset)
        if expected and actual != expected:
            _remove_raw_download(name, raw_directory_descriptor) if payload_kind == "raw_file" else _remove_download(downloaded_path)
            code = "archive_checksum_mismatch" if payload_kind == "archive" else "asset_checksum_mismatch"
            raise UpstreamArchiveError(code, "Downloaded Release asset does not match its GitHub SHA-256", details={"expected": expected, "actual": actual, "asset": name})
        if payload_kind == "raw_file":
            anchored_file = os.fstat(raw_file_descriptor)
            anchored_directory = os.fstat(raw_directory_descriptor)
            raw_file_identity = {
                "device": anchored_file.st_dev, "inode": anchored_file.st_ino,
                "size": anchored_file.st_size, "sha256": actual,
                "directory_device": anchored_directory.st_dev,
                "directory_inode": anchored_directory.st_ino,
            }
            extraction = None
        elif kind == "zip":
            extraction = extract_zip_archive(downloaded_path, source)
        else:
            extraction = source_acquisition.extract_tar_archive(downloaded_path, source)
    except UpstreamArchiveError:
        if payload_kind == "raw_file":
            _remove_raw_download(name, raw_directory_descriptor)
        raise
    except github_client.GitHubError as exc:
        if payload_kind == "raw_file":
            _remove_raw_download(name, raw_directory_descriptor)
        raise UpstreamArchiveError(exc.code, str(exc)) from exc
    except source_acquisition.SourceError as exc:
        raise UpstreamArchiveError("archive_extract_failed", str(exc)) from exc
    except Exception:
        if payload_kind == "raw_file":
            _remove_raw_download(name, raw_directory_descriptor)
        raise
    finally:
        if raw_file_descriptor is not None:
            os.close(raw_file_descriptor)
        if raw_directory_descriptor is not None:
            os.close(raw_directory_descriptor)
    return {
        "repository": release["repository"], "strategy": recipe["source"]["tracking"], "ref": release["ref"], "tag": release["tag"],
        "release_id": release.get("release_id"),
        "release_name": release.get("name", ""), "release_url": release.get("url", ""), "upstream_version": release["upstream_version"],
        "debian_version": f"{release['upstream_version']}-{recipe['package']['version_revision']}" if recipe["package"]["version_revision"] else release["upstream_version"],
        "source_directory": str(source), "artifact_mode": "upstream_archive", "payload_kind": payload_kind,
        "file_count": extraction["files"] if extraction else 1,
        "asset": {
            "asset_id": selected_asset.get("asset_id"), "api_url": _stable_identity_url(selected_asset.get("api_url", "")),
            "name": name, "url": _stable_identity_url(selected_asset["url"]), "source": selected_asset.get("source", "release_asset"),
            "declared_size": selected_asset.get("size", 0), "download_size": actual_size,
            "content_type": selected_asset.get("content_type", ""), "sha256": actual,
            "expected_sha256": expected, "checksum_verified": bool(expected),
            "payload_kind": payload_kind, "file_count": extraction["files"] if extraction else 1,
            **({"archive_format": kind} if kind else {}),
        },
        "extraction": extraction,
        **({"_raw_file_identity": raw_file_identity} if raw_file_identity else {}),
    }


def _selection_error(code: str, role: str, path: str, expected_kind: str, message: str, **details) -> UpstreamArchiveError:
    return UpstreamArchiveError(
        code, message,
        details={"role": role, "path": path, "expected_kind": expected_kind, **details},
    )


def _selector_target(source: Path, selector: str, role: str) -> Path:
    parsed = parse_archive_path(selector)
    expected = "directory" if parsed.is_directory else "file"
    unresolved = source.joinpath(*parsed.parts)
    try:
        mode = unresolved.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _selection_error(
            "archive_selection_path_not_found", role, selector, expected,
            f"Archive {role} path was not found: {selector}",
        ) from exc
    if stat.S_ISLNK(mode):
        raise _selection_error(
            "archive_selection_unsafe_path", role, selector, expected,
            f"Archive {role} path is a symbolic link: {selector}", actual_kind="symlink",
        )
    actual = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "special"
    if actual != expected:
        raise _selection_error(
            "archive_selection_type_mismatch", role, selector, expected,
            f"Archive {role} path has the wrong type: {selector}", actual_kind=actual,
        )
    target = unresolved.resolve(strict=True)
    try:
        target.relative_to(source)
    except ValueError as exc:
        raise _selection_error(
            "archive_selection_unsafe_path", role, selector, expected,
            f"Archive {role} path escapes the extraction root: {selector}",
        ) from exc
    return target


def _archive_inventory(source: Path) -> list[tuple[str, Path]]:
    inventory = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise UpstreamArchiveError(
                "archive_selection_unsafe_path", f"Extracted archive contains a symbolic link: {relative}",
                details={"role": "inventory", "path": relative, "expected_kind": "regular entry", "actual_kind": "symlink"},
            )
        canonical = relative + "/" if stat.S_ISDIR(mode) else relative
        try:
            parse_archive_path(canonical)
        except ValueError as exc:
            raise UpstreamArchiveError(
                "archive_selection_unsafe_path", f"Extracted archive contains an unsafe logical path: {relative}",
                details={"role": "inventory", "path": relative, "expected_kind": "canonical path"},
            ) from exc
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise UpstreamArchiveError(
                "archive_selection_unsafe_path", f"Extracted archive contains a special entry: {relative}",
                details={"role": "inventory", "path": relative, "expected_kind": "file", "actual_kind": "special"},
            )
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(source)
        except ValueError as exc:
            raise UpstreamArchiveError(
                "archive_selection_unsafe_path", f"Extracted archive file escapes the extraction root: {relative}",
                details={"role": "inventory", "path": relative, "expected_kind": "file"},
            ) from exc
        inventory.append((relative, resolved))
    return inventory


def _file_record(relative: str, target: Path) -> dict:
    return {
        "relative_path": relative, "path": str(target), "size": target.stat().st_size,
        "mode": f"{target.stat().st_mode & 0o777:04o}",
    }


def resolve_raw_payload(source_directory: str | Path, filename: str, expected_identity: dict | None = None) -> dict:
    """Represent one verified raw Release asset using the existing staging plan."""
    source = Path(source_directory).resolve(strict=True)
    name = _safe_asset_name(filename)
    directory_descriptor = None
    file_descriptor = None
    try:
        directory_descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        file_descriptor = _open_verified_raw_file(source, name, directory_descriptor)
        actual_sha256 = _fd_sha256(file_descriptor)
        _raw_file_unchanged(name, directory_descriptor, file_descriptor)
        _source_directory_unchanged(source, directory_descriptor)
        file_status = os.fstat(file_descriptor)
        directory_status = os.fstat(directory_descriptor)
        identity = {
            "device": file_status.st_dev, "inode": file_status.st_ino,
            "size": file_status.st_size, "sha256": actual_sha256,
            "directory_device": directory_status.st_dev,
            "directory_inode": directory_status.st_ino,
        }
        if expected_identity and identity != expected_identity:
            raise UpstreamArchiveError(
                "raw_file_verification_failed", "Raw Release asset changed before payload planning",
            )
    except UpstreamArchiveError:
        raise
    except OSError as exc:
        raise UpstreamArchiveError(
            "raw_file_verification_failed", "Raw Release asset could not be verified for payload planning",
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    target = source / name
    return {
        "mode": "raw_file", "include": [name], "exclude": [],
        "explicit_files": 1, "selected_directories": 0, "selected_files": 1,
        "excluded_files": 0, "excluded_directories": 0,
        "excluded_resolved_files": 0, "legacy_layout": False,
        "files": [{**_file_record(name, target), "verified_identity": identity}],
    }


def payload_plan_summary(plan: dict) -> dict:
    """Return the bounded portion of a resolved payload plan safe for Run details."""
    return {key: plan[key] for key in (
        "mode", "include", "exclude", "explicit_files", "selected_directories",
        "selected_files", "excluded_files", "excluded_directories",
        "excluded_resolved_files", "legacy_layout",
    )}


def resolve_payload(recipe: dict, source_directory: str | Path) -> dict:
    """Resolve canonical selectors to deterministic regular files after root stripping."""
    source = Path(source_directory).resolve(strict=True)
    payload = recipe["artifact"]["payload"]
    include = list(payload["include"])
    exclude = list(payload["exclude"])
    for selector in include:
        _selector_target(source, selector, "include")
    for selector in exclude:
        _selector_target(source, selector, "exclude")

    inventory = _archive_inventory(source)
    included = [(relative, target) for relative, target in inventory if payload["mode"] == "entire_archive" or selectors_match(include, relative)]
    selected = [(relative, target) for relative, target in included if not selectors_match(exclude, relative)]
    if not selected:
        raise UpstreamArchiveError(
            "archive_selection_empty", "Archive payload selection resolved to zero files",
            details={"role": "payload", "path": "", "expected_kind": "file", "mode": payload["mode"]},
        )

    legacy_layout = payload.get("legacy_file_layout") == "basename"
    if legacy_layout:
        targets = {relative: target for relative, target in selected}
        selected = [(relative, targets[relative]) for relative in include if relative in targets]
    files = [_file_record(relative, target) for relative, target in selected]
    include_paths = [parse_archive_path(selector) for selector in include]
    exclude_paths = [parse_archive_path(selector) for selector in exclude]
    return {
        "mode": payload["mode"], "include": include, "exclude": exclude,
        "explicit_files": sum(not selector.is_directory for selector in include_paths),
        "selected_directories": sum(selector.is_directory for selector in include_paths),
        "selected_files": len(files),
        "excluded_files": sum(not selector.is_directory for selector in exclude_paths),
        "excluded_directories": sum(selector.is_directory for selector in exclude_paths),
        "excluded_resolved_files": len(included) - len(selected),
        "legacy_layout": legacy_layout, "files": files,
    }


def selected_file_records(recipe: dict, source_directory: str | Path) -> list[dict]:
    """Compatibility accessor for callers that only need the resolved file records."""
    return resolve_payload(recipe, source_directory)["files"]


def acquire(recipe: dict, workspace: str | Path, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    result = resolve_and_extract(recipe, workspace, token=token, release_resolver=release_resolver, downloader=downloader)
    raw_file_identity = result.pop("_raw_file_identity", None)
    payload = (
        resolve_raw_payload(result["source_directory"], result["asset"]["name"], raw_file_identity)
        if result["payload_kind"] == "raw_file"
        else resolve_payload(recipe, result["source_directory"])
    )
    return {**result, "archive_payload": payload}


def inspect(recipe: dict, *, token: str = "", release_resolver=resolve_release, downloader=github_client.download_archive) -> dict:
    with tempfile.TemporaryDirectory(prefix="debbuilder-archive-inspect-") as temporary:
        try:
            result = resolve_and_extract(recipe, temporary, token=token, release_resolver=release_resolver, downloader=downloader)
        except UpstreamArchiveError as exc:
            if exc.code in {"ambiguous_archive_source", "ambiguous_release_asset"}:
                release = release_resolver(recipe, token=token)
                options = (
                    release_asset_options(release, recipe["artifact"])
                    if exc.code == "ambiguous_release_asset"
                    else archive_source_options(release, recipe["artifact"])
                )
                exc.details.setdefault("sources", options)
            raise
        configured_payload = recipe["artifact"]["payload"]
        raw_file_identity = result.pop("_raw_file_identity", None)
        plan = resolve_raw_payload(result["source_directory"], result["asset"]["name"], raw_file_identity) if result["payload_kind"] == "raw_file" else None
        selection_error = None
        if result["payload_kind"] == "archive" and (configured_payload["mode"] == "entire_archive" or configured_payload["include"]):
            try:
                plan = resolve_payload(recipe, result["source_directory"])
            except UpstreamArchiveError as exc:
                if exc.code not in {"archive_selection_path_not_found", "archive_selection_type_mismatch", "archive_selection_empty"}:
                    raise
                selection_error = {"code": exc.code, "message": str(exc), "details": exc.details}
        return {
            "source": result["asset"],
            "release": {key: result.get(key, "") for key in ("repository", "ref", "tag", "release_id", "release_name", "release_url", "upstream_version", "debian_version")},
            "payload_kind": result["payload_kind"], "file_count": result["file_count"],
            "extraction": result["extraction"],
            "inventory": inspect_inventory(result["source_directory"]),
            "payload": payload_plan_summary(plan) if plan else None,
            "selection_error": selection_error,
        }
