"""Read, render, and clean canonical Build Run execution data."""
from __future__ import annotations

import json
from typing import Callable

from . import build_pipeline, workspace_cleanup
from .build_store import BuildStore
from .recipe_schema import require_safe_name


def list_executions(
    store: BuildStore,
    package_resolver: Callable[[dict], str],
    *,
    limit: int = 50,
    runs: list[dict] | None = None,
) -> list[dict]:
    candidates = store.list(limit=1_000_000) if runs is None else runs
    selected = [
        run for run in candidates
        if not store.execution_history_deleted(str(run["id"]), run)
    ][:limit]
    return [
        {
            **build_pipeline.execution_summary(run),
            "package": package_resolver(run),
            "recipe": run.get("recipe_id", ""),
        }
        for run in selected
    ]


def get_execution(store: BuildStore, run_id: str) -> dict | None:
    require_safe_name(run_id, "execution")
    run = store.load(run_id)
    if not run or store.execution_history_deleted(run_id, run):
        return None
    return build_pipeline.execution_detail(run)


def _error_lines(run: dict) -> list[str]:
    lines = []
    if run.get("error"):
        error = run["error"]
        lines.append(error.get("message", str(error)) if isinstance(error, dict) else str(error))
    for step in run.get("steps", []):
        if step.get("error"):
            error = step["error"]
            lines.append(f"{step.get('name')}: {error.get('message', str(error)) if isinstance(error, dict) else str(error)}")
    return lines


def format_log(run: dict, *, verbosity: str = "normal") -> str:
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    if verbosity == "compact":
        rows = [f"{step['name']}: {step['status']}" for step in run.get("steps", []) if step.get("status") != "pending"]
        rows.extend(f"error: {line}" for line in _error_lines(run))
        return "\n".join(rows) + ("\n" if rows else "")
    if verbosity == "normal":
        rows = [f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}" for step in run.get("steps", []) if step.get("status") != "pending"]
        events = [str(event.get("message") or "") for event in run.get("events", []) if any(marker in str(event.get("message") or "") for marker in ("Build tools", "Dependencies", "Build command", "validation", "publication"))]
        rows.extend(events)
        rows.extend(f"error: {line}" for line in _error_lines(run))
        return "\n".join(row for row in rows if row) + ("\n" if rows else "")
    rows = []
    for step in run.get("steps", []):
        if step.get("status") == "pending":
            continue
        rows.append(f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}")
        details = step.get("details") or {}
        for command in details.get("commands") or []:
            rows.append(f"Build command {command.get('index')}: {command.get('status')} cwd={command.get('working_directory', '')} duration={command.get('duration', '')}s")
            if command.get("stdout"):
                rows.append("stdout:\n" + command["stdout"].rstrip())
            if command.get("stderr"):
                rows.append("stderr:\n" + command["stderr"].rstrip())
    for validation in run.get("validations") or []:
        rows.append(f"validation {validation.get('id', '')}: {validation.get('status', '')}")
        if validation.get("error"):
            rows.append(json.dumps(validation["error"], indent=2, ensure_ascii=False))
    for publication in run.get("publications") or []:
        rows.append(f"publication {publication.get('id', '')}: {publication.get('status', '')}")
    rows.extend(f"error: {line}" for line in _error_lines(run))
    return "\n".join(row for row in rows if row) + ("\n" if rows else "")


def get_log(store: BuildStore, run_id: str, *, verbosity: str = "normal", after: int = 0) -> dict | None:
    require_safe_name(run_id, "execution")
    run = store.load(run_id)
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    if not run or store.execution_history_deleted(run_id, run):
        return None
    lifecycle_complete = not build_pipeline.execution_summary(run)["lifecycle_active"]
    if verbosity == "raw":
        chunk = store.log_slice(run_id, after)
        return {**chunk, "complete": lifecycle_complete, "verbosity": verbosity}
    rendered = format_log(run, verbosity=verbosity)
    start = max(0, min(int(after or 0), len(rendered)))
    return {"text": rendered[start:], "offset": len(rendered), "size": len(rendered), "complete": lifecycle_complete, "verbosity": verbosity}


def delete_log(store: BuildStore, run_id: str) -> dict:
    require_safe_name(run_id, "execution")
    return {**store.clear_log_history(run_id), "history_deleted": True, "visible": False}


def delete_logs(store: BuildStore, run_ids: list[str] | None = None, *, all_runs: bool = False, dry_run: bool = False) -> dict:
    selected = workspace_cleanup.completed_history_ids(store) if all_runs else list(run_ids or [])
    if dry_run:
        return {"count": len(selected), "ids": selected}
    deleted, errors = [], []
    for run_id in selected:
        try:
            deleted.append(delete_log(store, str(run_id)))
        except Exception as exc:
            errors.append({"id": str(run_id), "error": str(exc)})
    return {"deleted": deleted, "errors": errors}
