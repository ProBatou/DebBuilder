"""Canonical Build Run → Validation → Publication automation."""
from __future__ import annotations

from collections.abc import Callable

from .build_store import BuildStore


def publication_confirmation(run: dict) -> str:
    artifact = run.get("artifact") or {}
    inspection = artifact.get("inspection") or {}
    package = inspection.get("package") or run.get("package") or run.get("recipe_id") or ""
    run_version = run.get("version") or {}
    version = inspection.get("version") or (run_version.get("debian") if isinstance(run_version, dict) else run_version)
    return f"publish:{package}:{version}"


def run_post_build(
    run_id: str,
    *,
    dry_run: bool,
    settings: dict,
    store: BuildStore,
    validate: Callable[[str, dict], dict],
    publish: Callable[[str, dict], dict],
) -> dict:
    """Apply configured automation to the exact successful Build Run."""
    automation = settings.get("automation") or {}
    summary = {
        "auto_validate_after_successful_build": bool(automation.get("auto_validate_after_successful_build", False)),
        "auto_publish_after_successful_validation": bool(automation.get("auto_publish_after_successful_validation", False)),
        "validation": None,
        "publication": None,
    }
    if dry_run or not summary["auto_validate_after_successful_build"]:
        return summary
    run = store.load(run_id)
    if not run or run.get("mode") != "build" or run.get("status") != "success" or not (run.get("artifact") or {}).get("path"):
        return summary
    validation = validate(run_id, {})
    summary["validation"] = validation
    if validation.get("status") != "success" or not summary["auto_publish_after_successful_validation"]:
        return summary
    current = store.load(run_id) or run
    summary["publication"] = publish(run_id, {"confirm": publication_confirmation(current)})
    return summary


def run_with_automation(
    workflow: dict,
    *,
    dry_run: bool,
    pipeline: Callable[..., dict],
    automate: Callable[..., dict],
    notify_completion: Callable[[dict], object],
) -> dict:
    result = pipeline(workflow, dry_run=dry_run)
    run_id = str(result.get("run_id") or "")
    if run_id and result.get("status") == "success":
        automation = automate(run_id, dry_run=dry_run)
        if automation.get("validation"):
            result["validation"] = automation["validation"]
        if automation.get("publication"):
            result["publication"] = automation["publication"]
        result["automation"] = automation
    if not dry_run:
        notify_completion(result)
    return result
