"""Data contracts for isolated DebBuilder build runs."""
from __future__ import annotations

from datetime import datetime, timezone
import time

STEP_NAMES = (
    "source", "detection", "dependencies", "source_changes", "build",
    "staging", "debian_metadata", "systemd", "package", "artifact",
)
STEP_STATUSES = frozenset({"pending", "running", "success", "failed", "skipped"})
RUN_STATUSES = frozenset({"pending", "running", "prepared", "success", "failed", "cancelled"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_step(name: str) -> dict:
    if name not in STEP_NAMES:
        raise ValueError(f"unknown build step: {name}")
    return {
        "name": name,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "summary": "",
        "error": None,
        "details": {},
    }


def new_run(run_id: str, recipe_id: str, mode: str, workspace: str, recipe_sha256: str) -> dict:
    if mode not in {"dry_run", "build"}:
        raise ValueError("build mode must be dry_run or build")
    now = utc_now()
    return {
        "schema_version": 1,
        "id": run_id,
        "recipe_id": recipe_id,
        "recipe_sha256": recipe_sha256,
        "mode": mode,
        "status": "pending",
        "created_at": now,
        "created_at_epoch": time.time(),
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "workspace": workspace,
        "version": {"upstream": "", "debian": ""},
        "steps": [new_step(name) for name in STEP_NAMES],
        "artifact": None,
        "error": None,
        "events": [],
    }


def validate_run(run: dict) -> None:
    if run.get("status") not in RUN_STATUSES:
        raise ValueError(f"invalid run status: {run.get('status')}")
    steps = run.get("steps")
    if not isinstance(steps, list) or [step.get("name") for step in steps] != list(STEP_NAMES):
        raise ValueError("build run has an invalid step sequence")
    for step in steps:
        if step.get("status") not in STEP_STATUSES:
            raise ValueError(f"invalid status for step {step.get('name')}")
