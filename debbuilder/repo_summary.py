"""APT repository summary helpers."""
from __future__ import annotations

import urllib.request

from .recipe_schema import SAFE_ARCH


def fetch_packages(repo_url: str, suite: str, component: str, arch: str = "amd64") -> str:
    if arch not in SAFE_ARCH:
        raise ValueError("unsupported arch")
    url = f"{repo_url.rstrip('/')}/dists/{suite}/{component}/binary-{arch}/Packages"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_package_version(packages_text: str, package: str) -> str | None:
    current = None
    version = None
    for line in packages_text.splitlines() + [""]:
        if not line.strip():
            if current == package:
                return version
            current = None
            version = None
            continue
        if line.startswith("Package: "):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Version: "):
            version = line.split(":", 1)[1].strip()
    return None


def summarize(workflow: dict, apt: dict) -> dict:
    repo_url = workflow.get("repo_url") or apt["repository"]
    suite = workflow.get("suite") or apt["distribution"]
    component = workflow.get("component") or apt["component"]
    arch = workflow.get("arch") or apt["architecture"]
    packages = None
    repo_versions = {}
    try:
        packages = fetch_packages(repo_url, suite, component, arch)
    except Exception as e:
        repo_versions["_error"] = str(e)
    for step in workflow.get("steps", []):
        if step.get("type") == "compare_apt_version" and packages:
            pkg = step.get("package", "")
            repo_versions[pkg] = parse_package_version(packages, pkg)
    return {"repo_url": repo_url, "suite": suite, "component": component, "repo_versions": repo_versions}
