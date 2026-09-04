"""Small stdlib GitHub API client for DebBuilder package sources."""

from __future__ import annotations

import json
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

ALLOWED_DOWNLOAD_HOSTS = {"api.github.com", "github.com", "codeload.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


class GitHubError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "status": self.status}


def parse_github_url(value: str) -> str:
    value = (value or "").strip().removesuffix(".git")
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", value):
        return value
    parsed = urlparse(value)
    if parsed.netloc.lower() == "github.com":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and re.match(r"^[A-Za-z0-9_.-]+$", parts[0]) and re.match(r"^[A-Za-z0-9_.-]+$", parts[1]):
            return f"{parts[0]}/{parts[1]}"
    raise ValueError("not a GitHub repository")


def request_json(path_or_url: str, token: str = "", timeout: int = 20):
    url = path_or_url if path_or_url.startswith("https://") else "https://api.github.com" + path_or_url
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "debbuilder"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise GitHubError("github_api_error", f"GitHub API request failed with HTTP {exc.code}", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GitHubError("github_api_error", "GitHub API request failed") from exc


def repo_info(repository: str, token: str = "") -> dict:
    repo = parse_github_url(repository)
    try:
        data = request_json(f"/repos/{repo}", token=token)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError("repository_not_found", "Repository not found", status=404) from exc
        raise
    return {"repository": data.get("full_name", repo), "default_branch": data.get("default_branch", ""), "description": data.get("description", ""), "language": data.get("language", ""), "archived": bool(data.get("archived"))}


def list_branches(repository: str, token: str = "", limit: int = 50) -> list[str]:
    repo = parse_github_url(repository)
    data = request_json(f"/repos/{repo}/branches?per_page={limit}", token=token)
    return [row.get("name", "") for row in data if row.get("name")]


def list_tags(repository: str, token: str = "", limit: int = 50) -> list[str]:
    repo = parse_github_url(repository)
    data = request_json(f"/repos/{repo}/tags?per_page={limit}", token=token)
    return [row.get("name", "") for row in data if row.get("name")]


def list_releases(repository: str, token: str = "", limit: int = 30) -> list[dict]:
    repo = parse_github_url(repository)
    data = request_json(f"/repos/{repo}/releases?per_page={limit}", token=token)
    return [{"tag": row.get("tag_name", ""), "name": row.get("name", ""), "prerelease": bool(row.get("prerelease")), "assets": [a.get("name", "") for a in row.get("assets", [])]} for row in data]


def latest_release(repository: str, token: str = "") -> dict:
    repo = parse_github_url(repository)
    try:
        row = request_json(f"/repos/{repo}/releases/latest", token=token)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError("release_not_found", "Release not found", status=404) from exc
        raise
    return {
        "tag": row.get("tag_name", ""),
        "name": row.get("name", ""),
        "url": row.get("html_url", ""),
        "archive_url": row.get("tarball_url", ""),
        "tarball_url": row.get("tarball_url", ""),
        "zipball_url": row.get("zipball_url", ""),
        "assets": [{
            "name": a.get("name", ""), "url": a.get("browser_download_url", ""),
            "size": a.get("size", 0), "content_type": a.get("content_type", ""),
            "digest": a.get("digest", ""),
        } for a in row.get("assets", [])],
    }


def resolve_ref(repository: str, ref: str, *, kind: str = "manual", token: str = "") -> dict:
    repo = parse_github_url(repository)
    value = str(ref or "").strip()
    if not value or len(value) > 200 or any(char.isspace() for char in value):
        raise GitHubError("release_not_found", "A safe explicit GitHub ref is required")
    encoded = quote(value, safe="")
    path = f"/repos/{repo}/git/ref/tags/{encoded}" if kind == "tag" else f"/repos/{repo}/commits/{encoded}"
    try:
        row = request_json(path, token=token)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError("release_not_found", "Release or ref not found", status=404) from exc
        raise
    sha = str((row.get("object") or {}).get("sha") if kind == "tag" else row.get("sha") or "")
    tarball_url = f"https://api.github.com/repos/{repo}/tarball/{encoded}"
    zipball_url = f"https://api.github.com/repos/{repo}/zipball/{encoded}"
    return {
        "tag": value if kind == "tag" else "",
        "name": value,
        "ref": value,
        "commit": sha,
        "url": f"https://github.com/{repo}/tree/{quote(value, safe='')}",
        "archive_url": tarball_url,
        "tarball_url": tarball_url,
        "zipball_url": zipball_url,
        "assets": [],
    }


def validate_download_url(url: str) -> None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS or parsed.username or parsed.password:
        raise GitHubError("source_download_failed", "Source download URL is not an allowed GitHub HTTPS URL")


def download_archive(url: str, destination: str | Path, *, token: str = "", timeout: int = 60, max_bytes: int = 500 * 1024 * 1024, urlopen=urllib.request.urlopen) -> dict:
    validate_download_url(url)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "debbuilder"}
    if token:
        headers["Authorization"] = "Bearer " + token
    target = Path(destination)
    total = 0
    digest = hashlib.sha256()
    try:
        with urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            validate_download_url(final_url)
            with target.open("xb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise GitHubError("source_download_failed", "Source download exceeds the configured size limit")
                    handle.write(chunk)
                    digest.update(chunk)
    except GitHubError:
        target.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        target.unlink(missing_ok=True)
        raise GitHubError("source_download_failed", "Source download failed") from exc
    return {"path": str(target), "size": total, "sha256": digest.hexdigest()}
