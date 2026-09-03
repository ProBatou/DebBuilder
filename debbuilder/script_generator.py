"""Legacy read-only Bash preview retained for API compatibility."""
from __future__ import annotations

import shlex

from .recipe_schema import normalize_steps, uses_automatic_pipeline, validate_recipe_metadata

REPO_SETTINGS_PROVIDER = None
EFFECTIVE_BUILD_PROVIDER = None


def _repo_settings() -> dict:
    if REPO_SETTINGS_PROVIDER is None:
        return {"repository": "https://repo.example.invalid", "distribution": "stable", "component": "main", "architecture": "amd64"}
    return REPO_SETTINGS_PROVIDER()


def _q(value: object) -> str:
    return shlex.quote(str(value))


def generate_script(workflow: dict, dry_run=True) -> str:
    validate_recipe_metadata(workflow)
    ignored_steps = normalize_steps(workflow)
    apt = _repo_settings()
    name = workflow.get("name") or "recipe"
    package = workflow.get("package_name") or name
    repository = workflow.get("github_repository") or ""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"DRY_RUN={1 if dry_run else 0}",
        f"RECIPE_NAME={_q(name)}",
        f"PACKAGE_NAME={_q(package)}",
        f"GITHUB_REPOSITORY={_q(repository)}",
        f"APT_REPOSITORY={_q(apt.get('repository', ''))}",
        f"APT_DISTRIBUTION={_q(apt.get('distribution', ''))}",
        f"APT_COMPONENT={_q(apt.get('component', ''))}",
        "note(){ printf '\\n== %s ==\\n' \"$*\"; }",
        "note \"Legacy read-only recipe preview\"",
        "echo \"Build, validation and publication are performed by structured Build Runs.\"",
    ]
    if uses_automatic_pipeline(workflow):
        lines += [
            "note \"Source\"",
            "echo \"GitHub: $GITHUB_REPOSITORY\"",
            f"echo \"Tracking: {_q(workflow.get('version_tracking') or 'latest_release')}\"",
            f"echo \"Version source: {_q(workflow.get('version_source') or 'tag')}\"",
        ]
    if ignored_steps:
        lines += [
            "note \"Ignored stored step payload\"",
            f"echo \"Stored steps: {len(ignored_steps)}\"",
        ]
    return "\n".join(lines) + "\n"
