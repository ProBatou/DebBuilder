"""Small stdlib GitHub API client for DebBuilder package sources."""

from __future__ import annotations

import json
import hashlib
import re
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

ALLOWED_DOWNLOAD_HOSTS = {"api.github.com", "github.com", "codeload.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RELEASE_ASSET_PAGES = 10


class GitHubError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None, retry_after_seconds: int | None = None, rate_limit_reset: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_reset = rate_limit_reset

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "status": self.status}


def _retry_metadata(headers) -> tuple[int | None, int | None]:
    retry_after = None
    raw_retry = str(headers.get("Retry-After", "") or "").strip()
    if raw_retry.isdigit() and len(raw_retry) <= 20:
        retry_after = int(raw_retry)
    elif raw_retry and len(raw_retry) <= 128:
        try:
            parsed = parsedate_to_datetime(raw_retry)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            retry_after = max(0, int((parsed - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    raw_reset = str(headers.get("X-RateLimit-Reset", "") or "").strip()
    reset = int(raw_reset) if raw_reset.isdigit() and len(raw_reset) <= 20 else None
    return retry_after, reset


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
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.username or parsed.password:
        raise GitHubError("github_api_error", "GitHub API URL is not allowed")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "debbuilder"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            final = urlparse(final_url)
            if final.scheme != "https" or final.hostname != "api.github.com" or final.username or final.password:
                raise GitHubError("github_api_error", "GitHub API redirected to a disallowed URL")
            raw = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(raw) > MAX_API_RESPONSE_BYTES:
                raise GitHubError("github_api_error", "GitHub API response exceeds the configured limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        headers = exc.headers or {}
        rate_limited = (
            exc.code == 429
            or str(headers.get("X-RateLimit-Remaining", "")) == "0"
            or bool(headers.get("Retry-After"))
        )
        code = "github_rate_limited" if rate_limited else "github_unavailable" if exc.code in {408, 500, 502, 503, 504} else "github_api_error"
        retry_after, reset = _retry_metadata(headers)
        raise GitHubError(code, f"GitHub API request failed with HTTP {exc.code}", status=exc.code, retry_after_seconds=retry_after, rate_limit_reset=reset) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
        raise GitHubError("github_unavailable", "GitHub API request failed") from exc


def repo_info(repository: str, token: str = "") -> dict:
    repo = parse_github_url(repository)
    try:
        data = request_json(f"/repos/{repo}", token=token)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError("repository_not_found", "Repository not found", status=404) from exc
        raise
    if not isinstance(data, dict):
        raise GitHubError("github_api_error", "GitHub repository response is malformed")
    full_name = data.get("full_name", repo)
    if not isinstance(full_name, str):
        raise GitHubError("github_api_error", "GitHub repository identity is malformed")
    try:
        canonical = parse_github_url(full_name)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GitHubError("github_api_error", "GitHub repository identity is malformed") from exc
    return {"repository": canonical, "default_branch": data.get("default_branch", ""), "description": data.get("description", ""), "language": data.get("language", ""), "archived": bool(data.get("archived"))}


def latest_release(repository: str, token: str = "") -> dict:
    repo = parse_github_url(repository)
    try:
        row = request_json(f"/repos/{repo}/releases/latest", token=token)
    except GitHubError as exc:
        if exc.status == 404:
            raise GitHubError("release_not_found", "Release not found", status=404) from exc
        raise
    if not isinstance(row, dict) or not isinstance(row.get("assets", []), list) or any(not isinstance(asset, dict) for asset in row.get("assets", [])):
        raise GitHubError("github_api_error", "GitHub Release response is malformed")
    return {
        "release_id": row.get("id"),
        "tag": row.get("tag_name", ""),
        "name": row.get("name", ""),
        "url": row.get("html_url", ""),
        "archive_url": row.get("tarball_url", ""),
        "tarball_url": row.get("tarball_url", ""),
        "zipball_url": row.get("zipball_url", ""),
        "assets": [{
            "asset_id": a.get("id"), "api_url": a.get("url", ""),
            "name": a.get("name", ""), "url": a.get("browser_download_url", ""),
            "size": a.get("size", 0), "content_type": a.get("content_type", ""),
            "digest": a.get("digest", ""),
        } for a in row.get("assets", [])],
    }


def release_assets(repository: str, release_id: int | str, *, token: str = "", max_pages: int = MAX_RELEASE_ASSET_PAGES) -> list[dict]:
    """Enumerate every Release asset with an explicit bounded page limit."""
    repo = parse_github_url(repository)
    identifier = str(release_id or "")
    if not identifier.isdigit():
        raise GitHubError("github_api_error", "GitHub Release ID is missing or malformed")
    rows = []
    for page in range(1, min(max(1, max_pages), MAX_RELEASE_ASSET_PAGES) + 1):
        data = request_json(
            f"/repos/{repo}/releases/{identifier}/assets?per_page=100&page={page}", token=token,
        )
        if not isinstance(data, list):
            raise GitHubError("github_api_error", "GitHub Release assets response is malformed")
        for asset in data:
            if not isinstance(asset, dict):
                raise GitHubError("github_api_error", "GitHub Release asset response is malformed")
            rows.append({
                "asset_id": asset.get("id"), "api_url": asset.get("url", ""),
                "name": asset.get("name", ""), "url": asset.get("browser_download_url", ""),
                "size": asset.get("size"), "content_type": asset.get("content_type", ""),
                "digest": asset.get("digest", ""),
            })
        if len(data) < 100:
            return rows
    raise GitHubError("github_api_error", "GitHub Release asset inventory exceeds the configured page limit")


def latest_release_exact(repository: str, token: str = "") -> dict:
    """Resolve latest Release metadata plus the complete bounded asset inventory."""
    release = latest_release(repository, token=token)
    return {
        **release,
        "assets": release_assets(repository, release.get("release_id"), token=token),
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
    if not isinstance(row, dict):
        raise GitHubError("github_api_error", "GitHub ref response is malformed")
    ref_object = ""
    if kind == "tag":
        target = row.get("object")
        if not isinstance(target, dict):
            raise GitHubError("github_api_error", "GitHub tag target is malformed")
        ref_object = str(target.get("sha") or "").lower()
        target_type = str(target.get("type") or "")
        target_sha = ref_object
        for _depth in range(5):
            if target_type == "commit":
                break
            if target_type != "tag" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target_sha):
                raise GitHubError("github_api_error", "GitHub tag does not resolve to a commit")
            try:
                tag_object = request_json(f"/repos/{repo}/git/tags/{target_sha}", token=token)
            except GitHubError as exc:
                if exc.status == 404:
                    raise GitHubError("release_not_found", "Release or ref not found", status=404) from exc
                raise
            nested = tag_object.get("object") if isinstance(tag_object, dict) else None
            if not isinstance(nested, dict):
                raise GitHubError("github_api_error", "GitHub annotated tag response is malformed")
            target_type = str(nested.get("type") or "")
            target_sha = str(nested.get("sha") or "").lower()
        else:
            raise GitHubError("github_api_error", "GitHub annotated tag chain exceeds the configured limit")
        sha = target_sha
    else:
        sha = str(row.get("sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise GitHubError("github_api_error", "GitHub ref did not resolve to an immutable commit")
    if kind == "tag" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref_object):
        raise GitHubError("github_api_error", "GitHub tag object identity is malformed")
    tarball_url = f"https://api.github.com/repos/{repo}/tarball/{sha}"
    zipball_url = f"https://api.github.com/repos/{repo}/zipball/{sha}"
    return {
        "tag": value if kind == "tag" else "",
        "name": value,
        "ref": value,
        "commit": sha,
        "ref_object_sha": ref_object.lower(),
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


def release_asset_download_url(repository: str, asset_id: int | str) -> str:
    """Return the immutable GitHub API endpoint for one Release asset ID."""
    repo = parse_github_url(repository)
    identifier = str(asset_id or "")
    if not identifier.isdigit():
        raise GitHubError("github_api_error", "GitHub Release asset ID is missing or malformed")
    return f"https://api.github.com/repos/{repo}/releases/assets/{identifier}"


def _download(url: str, destination: str | Path, *, accept: str, token: str = "", timeout: int = 60, max_bytes: int = 500 * 1024 * 1024, urlopen=urllib.request.urlopen) -> dict:
    validate_download_url(url)
    headers = {"Accept": accept, "User-Agent": "debbuilder"}
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


def download_archive(url: str, destination: str | Path, *, token: str = "", timeout: int = 60, max_bytes: int = 500 * 1024 * 1024, urlopen=urllib.request.urlopen) -> dict:
    return _download(url, destination, accept="application/vnd.github+json", token=token, timeout=timeout, max_bytes=max_bytes, urlopen=urlopen)


def download_release_asset(url: str, destination: str | Path, *, token: str = "", timeout: int = 60, max_bytes: int = 500 * 1024 * 1024, urlopen=urllib.request.urlopen) -> dict:
    return _download(url, destination, accept="application/octet-stream", token=token, timeout=timeout, max_bytes=max_bytes, urlopen=urlopen)
