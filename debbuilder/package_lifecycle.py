"""Package lifecycle actions independent from HTTP routing."""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def package_lifecycle_operation(
    name: str,
    action: str,
    payload: dict,
    *,
    get_package: Callable[[str], dict | None],
    github_client,
    package_store,
    deb_inspector,
    operations,
    effective_build: Callable[[], dict],
    repo_settings: Callable[[], dict],
) -> dict:
    pkg = get_package(name)
    if not pkg:
        raise KeyError("package not found")
    if action == "refresh-source":
        source = dict(pkg.get("source") or {})
        repo = source.get("repository") or github_client.parse_github_url(source.get("url", ""))
        info = github_client.repo_info(repo)
        branches = github_client.list_branches(repo)
        tags = github_client.list_tags(repo)
        releases = github_client.list_releases(repo)
        latest_release = releases[0].get("tag", "") if releases else ""
        source.update({
            "repository": info.get("repository", repo),
            "default_branch": info.get("default_branch", ""),
            "description": info.get("description", ""),
            "language": info.get("language", ""),
            "archived": info.get("archived", False),
            "branches": branches,
            "tags": tags,
            "releases": releases,
            "latest_release": latest_release,
        })
        return {"ok": True, "package": name, "source": source}
    if action == "check-updates":
        source_version = str(payload.get("source_version") or pkg.get("version", {}).get("source") or pkg.get("upstream_version") or "")
        published = str(payload.get("published_version") or pkg.get("version", {}).get("published") or pkg.get("apt_version") or "")
        built = str(payload.get("built_version") or pkg.get("version", {}).get("candidate") or "")
        verified = bool(payload.get("has_verified_build", False))
        state = package_store.compute_package_state(
            source_version=source_version,
            built_version=built,
            published_version=published,
            has_verified_build=verified,
        )
        return {
            "ok": True,
            "package": name,
            "source_version": source_version,
            "published_version": published,
            "built_version": built,
            "state": state,
        }
    if action == "verify-deb":
        deb = payload.get("deb") or pkg.get("build", {}).get("last_artifact")
        if not deb:
            raise ValueError("deb path required")
        verification = deb_inspector.inspect_deb(Path(deb))
        return {"ok": True, "package": name, "verification": verification}
    if action == "publish":
        dry_run = bool(payload.get("dry_run", True))
        if not dry_run and not effective_build()["allow_real_run"]:
            raise PermissionError("real publication disabled; enable real run and provide explicit confirmation")
        deb = payload.get("deb") or pkg.get("build", {}).get("last_artifact")
        if not deb:
            raise ValueError("deb path required")
        apt = repo_settings()
        publication = operations.publish_deb_operation(
            Path(deb),
            repo_root=Path(payload.get("repo_root") or "/var/www/html"),
            package_name=name,
            version=str(payload.get("version") or pkg.get("version", {}).get("candidate") or pkg.get("apt_version") or ""),
            dry_run=dry_run,
            confirm=str(payload.get("confirm") or ""),
            distribution=apt["distribution"],
            component=apt["component"],
            architecture=apt["architecture"],
        )
        return {"ok": True, "package": name, "publication": publication}
    raise ValueError("unknown package lifecycle action")
