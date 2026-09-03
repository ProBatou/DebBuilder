"""GitHub source resolution and safe extraction inside a Build Run."""
from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from . import github_client
from .recipe_schema import normalize_github_version


class SourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


def _version_from_resolution(recipe: dict, resolved: dict) -> tuple[str, str]:
    version_config = recipe["source"]["version"]
    mode = version_config["source"]
    if mode == "build":
        raise SourceError("unable_to_determine_version", "Unable to determine version: build-provided versions are not available before build execution")
    if mode == "tag":
        raw = str(resolved.get("tag") or resolved.get("ref") or "")
    elif mode == "release_name":
        raw = str(resolved.get("name") or "")
    else:
        subject = str(resolved.get("tag") or resolved.get("name") or resolved.get("ref") or "")
        match = re.search(version_config["expression"], subject)
        raw = match.group(1) if match and match.groups() else match.group(0) if match else ""
    try:
        upstream = normalize_github_version(raw)
    except ValueError as exc:
        raise SourceError("unable_to_determine_version", "Unable to determine version from the selected GitHub source") from exc
    revision = recipe["package"]["version_revision"]
    return upstream, f"{upstream}-{revision}" if revision else upstream


def resolve_source(recipe: dict, *, token: str = "") -> dict:
    source = recipe["source"]
    repository = source["repository"]
    if not repository:
        raise SourceError("repository_not_found", "Repository not found")
    try:
        info = github_client.repo_info(repository, token=token)
        if source["tracking"] == "latest_release":
            resolved = github_client.latest_release(repository, token=token)
        elif source["tracking"] == "tag":
            resolved = github_client.resolve_ref(repository, source["ref"], kind="tag", token=token)
        else:
            resolved = github_client.resolve_ref(repository, source["ref"], kind="manual", token=token)
    except github_client.GitHubError as exc:
        raise SourceError(exc.code, str(exc)) from exc
    upstream, debian = _version_from_resolution(recipe, resolved)
    archive_url = str(resolved.get("archive_url") or "")
    try:
        github_client.validate_download_url(archive_url)
    except github_client.GitHubError as exc:
        raise SourceError(exc.code, str(exc)) from exc
    return {
        "repository": info["repository"],
        "strategy": source["tracking"],
        "ref": str(resolved.get("ref") or resolved.get("tag") or ""),
        "tag": str(resolved.get("tag") or ""),
        "release_name": str(resolved.get("name") or ""),
        "commit": str(resolved.get("commit") or ""),
        "release_url": str(resolved.get("url") or ""),
        "archive_url": archive_url,
        "upstream_version": upstream,
        "debian_version": debian,
    }


def _safe_member_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if path.is_absolute() or not parts or ".." in parts:
        raise SourceError("source_extract_failed", f"Unsafe path in source archive: {name}")
    return parts


def extract_tar_archive(archive: str | Path, destination: str | Path, *, max_members: int = 100_000, max_uncompressed_bytes: int = 1024 * 1024 * 1024) -> dict:
    target = Path(destination).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise SourceError("source_extract_failed", "Source directory is not empty")
    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            members = bundle.getmembers()
            if not members or len(members) > max_members:
                raise SourceError("source_extract_failed", "Source archive is empty or contains too many entries")
            parsed = [(member, _safe_member_parts(member.name)) for member in members]
            roots = {parts[0] for _, parts in parsed}
            strip_root = len(roots) == 1 and any(len(parts) >= 2 for _, parts in parsed)
            total = sum(member.size for member, _ in parsed if member.isfile())
            if total > max_uncompressed_bytes:
                raise SourceError("source_extract_failed", "Expanded source exceeds the configured size limit")
            extracted = 0
            for member, parts in parsed:
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise SourceError("source_extract_failed", f"Unsupported link or special file in source archive: {member.name}")
                relative = parts[1:] if strip_root else parts
                if not relative:
                    continue
                output = target.joinpath(*relative).resolve()
                try:
                    output.relative_to(target)
                except ValueError as exc:
                    raise SourceError("source_extract_failed", f"Unsafe path in source archive: {member.name}") from exc
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True, mode=0o755)
                elif member.isfile():
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise SourceError("source_extract_failed", f"Unable to read archive entry: {member.name}")
                    with source, output.open("xb") as handle:
                        shutil.copyfileobj(source, handle)
                    output.chmod(0o755 if member.mode & 0o111 else 0o644)
                    extracted += 1
            return {"files": extracted, "uncompressed_size": total, "stripped_root": next(iter(roots)) if strip_root else ""}
    except SourceError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise SourceError("source_extract_failed", "Source archive extraction failed") from exc


def acquire_source(recipe: dict, workspace: str | Path, *, token: str = "") -> dict:
    root = Path(workspace).resolve()
    source_dir = (root / "source").resolve()
    try:
        source_dir.relative_to(root)
    except ValueError as exc:
        raise SourceError("source_extract_failed", "Source destination escapes the build workspace") from exc
    resolution = resolve_source(recipe, token=token)
    archive = root / "source.tar.gz"
    try:
        download = github_client.download_archive(resolution["archive_url"], archive, token=token)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        extraction = extract_tar_archive(archive, source_dir)
    except github_client.GitHubError as exc:
        raise SourceError(exc.code, str(exc)) from exc
    return {**resolution, "archive": {"path": download["path"], "size": download["size"], "sha256": checksum}, "extraction": extraction, "source_directory": str(source_dir)}
