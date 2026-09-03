"""Filesystem persistence primitives for workflows and executions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        pass
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temporary.replace(path)


def list_workflows(
    sources: Iterable[tuple[str, Path, bool]],
    read_workflow: Callable[[Path], dict],
) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for source, folder, writable in sources:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.stem in seen:
                continue
            try:
                workflow = read_workflow(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            seen.add(path.stem)
            items.append({
                "id": path.stem,
                "name": workflow.get("name", path.stem),
                "source": source,
                "writable": writable,
                "updated": path.stat().st_mtime,
            })
    return items


def workflow_path(
    workflow_id: str,
    user_dir: Path,
    read_dirs: Iterable[Path],
    validate_name: Callable[[str, str], str],
    *,
    for_write: bool = False,
) -> Path | None:
    validate_name(workflow_id, "workflow id")
    if for_write:
        return user_dir / f"{workflow_id}.json"
    for folder in read_dirs:
        path = folder / f"{workflow_id}.json"
        if path.exists():
            return path
    return None


def list_runs(run_dirs: Iterable[Path], limit: int = 20) -> list[dict]:
    rows: list[dict] = []
    for folder in run_dirs:
        if not folder.exists():
            continue
        for path in folder.glob("*.out"):
            rows.append({
                "run_id": path.stem,
                "source": str(folder),
                "updated": path.stat().st_mtime,
                "size": path.stat().st_size,
            })
    return sorted(rows, key=lambda row: row["updated"], reverse=True)[:limit]


def infer_execution_status(output: str, metadata: dict | None = None) -> str:
    if metadata and metadata.get("status"):
        return metadata["status"]
    lowered = (output or "").lower()
    failures = ("traceback", "error", "failed", "returncode=1")
    return "failed" if any(marker in lowered for marker in failures) else "success"


def execution_steps(log: str) -> list[dict]:
    return [
        {"name": line.strip("= "), "status": "success"}
        for line in (log or "").splitlines()
        if line.startswith("== ") and line.endswith(" ==")
    ]


def list_executions(
    run_rows: Iterable[dict],
    metadata: Iterable[dict],
    *,
    limit: int = 50,
) -> list[dict]:
    metadata_by_id = {row.get("id"): row for row in metadata if row.get("id")}
    rows: list[dict] = []
    seen: set[str] = set()
    for run in run_rows:
        run_id = run["run_id"]
        output_path = Path(run["source"]) / f"{run_id}.out"
        output = output_path.read_text(errors="replace") if output_path.exists() else ""
        meta = metadata_by_id.get(run_id, {})
        rows.append({
            "id": run_id,
            "package": meta.get("package") or "",
            "action": meta.get("action") or "build",
            "version": meta.get("version") or "",
            "status": infer_execution_status(output, meta),
            "updated": run.get("updated"),
            "duration": meta.get("duration"),
            "size": run.get("size"),
        })
        seen.add(run_id)
    rows.extend(meta for run_id, meta in metadata_by_id.items() if run_id not in seen)
    return sorted(rows, key=lambda row: row.get("updated") or 0, reverse=True)[:limit]


def execution_detail(run_id: str, rows: Iterable[dict], run_dirs: Iterable[Path]) -> dict | None:
    execution = next((dict(row) for row in rows if row.get("id") == run_id), None)
    if not execution:
        return None
    output_path = script_path = None
    for folder in run_dirs:
        candidate = folder / f"{run_id}.out"
        if candidate.exists():
            output_path = candidate
            script_path = folder / f"{run_id}.sh"
            break
    execution["log"] = output_path.read_text(errors="replace") if output_path else ""
    execution["script"] = script_path.read_text(errors="replace") if script_path and script_path.exists() else ""
    execution["steps"] = execution_steps(execution["log"])
    return execution


def record_execution(path: Path, row: dict, *, history_limit: int = 200) -> None:
    rows = [stored for stored in load_json(path, []) if stored.get("id") != row["id"]]
    rows.append(row)
    save_json(path, rows[-history_limit:])
