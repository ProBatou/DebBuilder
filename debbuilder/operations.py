"""High-level package lifecycle operations.

These helpers intentionally separate dry-run/build/verify/publish so a build can
never publish implicitly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import apt_repo, deb_inspector


def publish_deb_operation(deb_path: str | Path, repo_root: str | Path, package_name: str, version: str, dry_run: bool = True, confirm: str = "", distribution: str = "stable", component: str = "main", architecture: str = "amd64") -> dict:
    deb = Path(deb_path)
    repo = Path(repo_root)
    if not deb.exists():
        raise FileNotFoundError(str(deb))
    backend = apt_repo.detect_repo_backend(repo)
    if dry_run:
        return {"status": "dry_run", "backend": backend, "deb": str(deb), "repo_root": str(repo), "package": package_name, "version": version, "distribution": distribution, "component": component}
    if confirm != f"publish:{package_name}:{version}":
        raise PermissionError("publication requires explicit confirmation")
    info = deb_inspector.inspect_deb(deb)
    if not info.get("ok"):
        raise ValueError("deb verification failed")
    if info.get("package") != package_name or info.get("version") != version:
        raise ValueError("deb metadata does not match requested publication")
    if backend == "reprepro":
        config = apt_repo.reprepro_config(repo)
        codename = config.get("codename") or distribution
        if distribution not in {codename, config.get("suite")}:
            raise ValueError(f"distribution {distribution!r} does not match reprepro codename/suite")
        if component not in config.get("components", []):
            raise ValueError(f"component {component!r} is not configured in reprepro")
        deb_arch = info.get("architecture", "")
        if deb_arch != "all" and deb_arch not in config.get("architectures", []):
            raise ValueError(f"architecture {deb_arch!r} is not configured in reprepro")
        result = apt_repo.reprepro_include_deb(repo, codename, deb, component)
        if result["command"].get("status") != "success":
            raise RuntimeError(result["command"].get("stderr") or "reprepro includedeb failed")
        return {"status": "published", "backend": "reprepro", "repository": result, "deb": info}
    if backend != "manual":
        raise ValueError("unsupported or uninitialized APT repository")
    target = apt_repo.target_pool_path(repo, info["package"], info["version"], info["architecture"])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(deb, target)
    metadata = apt_repo.regenerate_metadata(repo, distribution, component, architecture)
    return {"status": "published", "backend": "manual", "target": str(target), "metadata": metadata, "deb": info}


def remove_package_operation(repo_root: str | Path, package_name: str, distribution: str = "stable", dry_run: bool = True, confirm: str = "") -> dict:
    """Remove a package through reprepro with explicit confirmation."""
    repo = Path(repo_root)
    if apt_repo.detect_repo_backend(repo) != "reprepro":
        raise ValueError("package removal is only supported for reprepro repositories")
    config = apt_repo.reprepro_config(repo)
    codename = config.get("codename") or distribution
    if distribution not in {codename, config.get("suite")}:
        raise ValueError(f"distribution {distribution!r} does not match reprepro codename/suite")
    if dry_run:
        return {"status": "dry_run", "backend": "reprepro", "package": package_name, "distribution": codename}
    if confirm != f"remove:{package_name}":
        raise PermissionError("package removal requires explicit confirmation")
    result = apt_repo.reprepro_remove_package(repo, codename, package_name)
    cleanup = apt_repo.reprepro_delete_unreferenced(repo)
    return {"status": "removed", "backend": "reprepro", "package": package_name, "distribution": codename, "repository": result, "cleanup": cleanup}
