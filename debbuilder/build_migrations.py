"""Targeted, representation-only migrations for persisted Build Runs."""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from .build_store import BuildStore

OUTPUT_PREVIEW_LIMIT = 4096


def migrate_staging_manifest(store: BuildStore, run_id: str) -> dict:
    """Move a legacy inline staging file list to its Run manifest.

    When an older inline inventory was itself truncated, reconstruct it from the
    already-created staging tree. This does not execute any lifecycle operation.
    """
    run = store.load(run_id)
    if not run:
        raise ValueError(f"Build Run not found: {run_id}")
    before = deepcopy(run)
    step = next((row for row in run.get("steps", []) if row.get("name") == "staging"), None)
    if not step:
        raise ValueError("Build Run has no staging step")
    details = step.get("details") or {}
    if "content_files" not in details:
        return {"changed": False, "run": run}
    files = list(details.get("content_files") or [])
    match = re.search(r"with ([\d, ]+) application files", str(step.get("summary") or ""))
    expected = int(re.sub(r"\D", "", match.group(1))) if match else len(files)
    if expected != len(files):
        destination = str(details.get("install_destination") or "").lstrip("/")
        root = store.run_dir(run_id) / "staging" / destination
        if not root.is_dir():
            raise ValueError("legacy staging inventory is incomplete and its staging tree is unavailable")
        files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink())
        if len(files) != expected:
            raise ValueError(f"staging inventory count mismatch: expected {expected}, found {len(files)}")
    migrated_details = {**details, "content_files": files}
    step["details"] = store.staging_details_for_storage(run, migrated_details)
    # Explicit invariant: migration may only alter staging detail representation.
    check = deepcopy(run)
    old_step = next(row for row in before["steps"] if row.get("name") == "staging")
    new_step = next(row for row in check["steps"] if row.get("name") == "staging")
    old_step["details"] = {}
    new_step["details"] = {}
    if before != check:
        raise AssertionError("migration changed Build Run lifecycle metadata")
    store.save(run)
    return {"changed": True, "run": run, "content_file_count": len(files), "manifest": step["details"]["content_manifest"]}


def compact_large_run_payloads(store: BuildStore, run_id: str) -> dict:
    """Externalize artifact inventories and bound validation command previews."""
    run = store.load(run_id)
    if not run:
        raise ValueError(f"Build Run not found: {run_id}")
    changed = False
    before = deepcopy(run)
    artifact_step = next((row for row in run.get("steps", []) if row.get("name") == "artifact"), None)
    artifact = run.get("artifact") or {}
    step_artifact = (artifact_step or {}).get("details") or {}
    inspection = artifact.get("inspection") or {}
    step_inspection = step_artifact.get("inspection") or {}
    files = inspection.get("files")
    if files is None:
        files = step_inspection.get("files")
    if files is not None:
        manifest = "manifests/artifact-files.json"
        store.save_manifest(run_id, manifest, list(files))
        for target in (artifact, step_artifact):
            target_inspection = target.get("inspection") or {}
            if target_inspection:
                target_inspection.pop("files", None)
                target_inspection["files_manifest"] = manifest
                target_inspection["file_count"] = int(target_inspection.get("file_count") or len(files))
        changed = True
    for validation in run.get("validations") or []:
        validation_id = validation.get("id")
        for command in validation.get("commands") or []:
            index = int(command.get("index") or 0)
            if validation_id and index:
                result_file = f"validation/{validation_id}/commands/{index:03d}.json"
                if (store.run_dir(run_id) / result_file).is_file():
                    command["result_file"] = result_file
            for field in ("stdout", "stderr"):
                value = str(command.get(field) or "")
                if len(value) > OUTPUT_PREVIEW_LIMIT and command.get("result_file"):
                    command[field] = value[:OUTPUT_PREVIEW_LIMIT] + "\n[output truncated; full result is stored in the validation command file]"
                    command[f"{field}_truncated"] = True
                    command[f"{field}_characters"] = int(command.get(f"{field}_characters") or len(value))
                    changed = True
    if changed:
        # Only representation fields may change. Statuses, timestamps, hashes,
        # lifecycle records and all other Build data remain byte-logically equal.
        comparable_before = deepcopy(before)
        comparable_after = deepcopy(run)
        for comparable in (comparable_before, comparable_after):
            for target in (comparable.get("artifact") or {}, next((row.get("details") or {} for row in comparable.get("steps", []) if row.get("name") == "artifact"), {})):
                target_inspection = target.get("inspection") or {}
                target_inspection.pop("files", None)
                target_inspection.pop("files_manifest", None)
            for validation in comparable.get("validations") or []:
                for command in validation.get("commands") or []:
                    command.pop("result_file", None)
                    for field in ("stdout", "stderr"):
                        if command.pop(f"{field}_truncated", False):
                            command[field] = next(
                                old.get(field, "") for old_validation in before.get("validations") or []
                                if old_validation.get("id") == validation.get("id")
                                for old in old_validation.get("commands") or [] if old.get("index") == command.get("index")
                            )
                            command.pop(f"{field}_characters", None)
        if comparable_before != comparable_after:
            raise AssertionError("migration changed Build Run data outside compacted representations")
        store.save(run)
    return {"changed": changed, "run": run}
