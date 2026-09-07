"""Read, render, and clean canonical Build Run execution data."""
from __future__ import annotations

import json
from typing import Callable

from . import build_pipeline, workspace_cleanup
from .build_store import BuildStore
from .command_runner import redact_command
from .notifications import redact
from .recipe_schema import require_safe_name


STAGE_LABELS = {
    "source": "Source", "detection": "Project detection", "dependencies": "Dependencies",
    "source_changes": "Source changes", "build": "Build", "staging": "Debian installation",
    "debian_metadata": "Debian metadata", "systemd": "Service", "package": "Debian package",
    "artifact": "Artifact",
}


def _fact(label: str, value) -> dict | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value if item is not None and item != "")
    if value == "":
        return None
    return {"label": label, "value": str(redact(str(value)))}


def _command_text(command: dict) -> str:
    value = str(command.get("command") or "")
    arguments = command.get("arguments") or []
    if not isinstance(arguments, list):
        arguments = []
    if not value and arguments:
        value = " ".join(str(item) for item in arguments)
    return str(redact(redact_command(value, [str(item) for item in arguments])))


def _output_excerpt(command: dict) -> str:
    text = str(command.get("stderr") or command.get("stdout") or "").strip()
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip()][-3:]
    excerpt = str(redact("\n".join(lines)))
    return ("…" + excerpt[-597:]) if len(excerpt) > 600 else excerpt


def _failed_command(record: dict) -> dict:
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    direct = details.get("failed_command") or details.get("command")
    if isinstance(direct, dict):
        return direct
    commands = record.get("commands") or details.get("commands") or []
    if not isinstance(commands, list):
        return {}
    return next((row for row in reversed(commands) if isinstance(row, dict) and (row.get("status") == "failed" or row.get("accepted") is False or row.get("exit_code") not in {None, 0})), {})


def _diagnostic_source(run: dict) -> tuple[str, dict] | tuple[None, None]:
    publications = run.get("publications") if isinstance(run.get("publications"), list) else []
    if publications and isinstance(publications[-1], dict) and publications[-1].get("status") == "failed":
        return "publication", publications[-1]
    validations = run.get("validations") if isinstance(run.get("validations"), list) else []
    if validations and isinstance(validations[-1], dict) and validations[-1].get("status") == "failed":
        return "validation", validations[-1]
    if run.get("status") == "failed" and run.get("error"):
        return "build", {"error": run["error"]}
    return None, None


def execution_diagnostic(run: dict) -> dict | None:
    """Build a compact presentation contract from canonical lifecycle records.

    The result is intentionally derived at read time so historical run.json files
    need no migration and missing legacy fields degrade to a useful fallback.
    """
    phase, record = _diagnostic_source(run)
    if not record:
        return None
    error = record.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = str(error.get("code") or f"{phase}_failed")
    stage = str(error.get("stage") or ("validation" if phase == "validation" else "publication" if phase == "publication" else "build"))
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    facts: list[dict] = []
    where: list[dict] = []
    title = {"build": "Build failed", "validation": "Validation failed", "publication": "Publication failed"}[phase]
    next_action = "Review the structured details and the raw log before retrying."
    recipe_step = {"source": "source", "detection": "build", "dependencies": "build", "source_changes": "build", "build": "build", "staging": "install", "debian_metadata": "install", "systemd": "service", "package": "install", "artifact": "install"}.get(stage, "")
    where.append(_fact("Step", STAGE_LABELS.get(stage, stage.replace("_", " ").title())))

    if code == "missing_build_tools":
        title = "Required build tool unavailable"
        tool_checks = details.get("tool_checks") if isinstance(details.get("tool_checks"), list) else []
        rows = [row for row in tool_checks if isinstance(row, dict) and not row.get("available")]
        facts += filter(None, [
            _fact("Tools", [row.get("tool") for row in rows]),
            _fact("Status", [f"{row.get('tool')}: {row.get('status')}" for row in rows]),
            _fact("Required version", [f"{row.get('tool')} {row.get('requirement')}" for row in rows if row.get("requirement")]),
            _fact("Searched from", next((row.get("working_directory") for row in rows if row.get("working_directory")), "")),
            _fact("PATH", next((row.get("search_path") for row in rows if row.get("search_path")), "")),
        ])
        next_action = "Install or expose the required tool in the build PATH, then run Test again."
    elif code == "missing_build_dependencies":
        title = "Required Debian build dependency missing"
        facts += filter(None, [
            _fact("Missing packages", details.get("missing")),
            _fact("Detected packages", details.get("detected")),
            _fact("Manually configured", details.get("manually_added")),
        ])
        next_action = "Add the missing package to Build dependencies and install it on the builder."
    elif code in {"build_command_failed", "build_command_timeout"}:
        direct_command = details.get("failed_command")
        command = direct_command if isinstance(direct_command, dict) else _failed_command(details)
        plan = details.get("plan") if isinstance(details.get("plan"), dict) else {}
        title = "Build command timed out" if code == "build_command_timeout" else "Build command failed"
        where += filter(None, [_fact("Command index", command.get("index")), _fact("Working directory", command.get("working_directory"))])
        facts += filter(None, [
            _fact("Command", _command_text(command)), _fact("Exit code", command.get("exit_code")),
            _fact("Timeout reason", command.get("timeout_reason")),
            _fact("Configured inactivity", plan.get("inactivity_timeout")),
            _fact("Configured maximum runtime", plan.get("maximum_runtime")),
            _fact("Last command output", _output_excerpt(command)),
        ])
        if command.get("timeout_reason") == "inactivity":
            next_action = "Fix the command if it is hanging, or increase the inactivity timeout if silence is expected."
        elif command.get("timeout_reason") == "maximum_runtime":
            next_action = "Reduce the command runtime or increase the configured maximum runtime."
        else:
            next_action = "Run the displayed command in the same working directory and fix its reported error."
    elif code == "expected_output_missing":
        output = details.get("output") if isinstance(details.get("output"), dict) else {}
        title = "Expected build output is missing"
        where += filter(None, [_fact("Configured path", output.get("configured_path")), _fact("Resolved path", output.get("path"))])
        facts.append(_fact("Observed", output.get("kind") or "missing"))
        next_action = "Update the expected output path or make the build command create it."
    elif stage == "source_changes":
        failed = details.get("failed") if isinstance(details.get("failed"), dict) else details
        title = "Source change could not be applied"
        where += filter(None, [_fact("Change", error.get("change_index") or failed.get("index")), _fact("File", failed.get("path"))])
        facts += filter(None, [_fact("Operation", failed.get("operation")), _fact("Matches", failed.get("matches")), _fact("Anchor", failed.get("anchor"))])
        next_action = f"Review Source change {error.get('change_index') or failed.get('index') or ''} against the resolved upstream source.".rstrip()
    elif code in {"configuration_source_missing", "unsafe_configuration_source", "unsafe_install_path"}:
        title = "Debian install mapping failed"
        where += filter(None, [_fact("Mapping", details.get("mapping_index")), _fact("Source", details.get("source")), _fact("Destination", details.get("destination"))])
        facts += filter(None, [_fact("Resolved source", details.get("resolved_source")), _fact("Cause", details.get("cause"))])
        next_action = "Correct the source or destination in Debian installation mappings, then run Test again."
    elif stage == "systemd":
        title = "Systemd service definition is invalid"
        next_action = "Review the Service settings and generated unit before retrying."
    elif phase == "validation":
        checks = record.get("checks") if isinstance(record.get("checks"), list) else []
        failed_checks = [row for row in checks if isinstance(row, dict) and row.get("status") == "failed"]
        command = _failed_command(record)
        profile = record.get("profile") if isinstance(record.get("profile"), dict) else {}
        title = "Package validation failed"
        where += filter(None, [_fact("Profile", profile.get("name")), _fact("Failed checks", [row.get("name") for row in failed_checks])])
        facts += filter(None, [_fact("Command", _command_text(command)), _fact("Exit code", command.get("exit_code")), _fact("Reason", next((row.get("error") or row.get("summary") for row in failed_checks if row.get("error") or row.get("summary")), "")), _fact("Last command output", _output_excerpt(command))])
        next_action = "Review the failed validation check in the selected profile, then revalidate the same artifact."
        recipe_step = ""
    elif phase == "publication":
        repository = record.get("repository") if isinstance(record.get("repository"), dict) else {}
        command = record.get("command") or details.get("command") or {}
        command = command if isinstance(command, dict) else {}
        readiness = record.get("readiness") if isinstance(record.get("readiness"), dict) else {}
        title = "APT publication failed"
        where += filter(None, [_fact("Distribution", repository.get("distribution")), _fact("Component", repository.get("component"))])
        facts += filter(None, [_fact("Preflight", code), _fact("Readiness", readiness.get("reasons")), _fact("Command", _command_text(command)), _fact("Exit code", command.get("exit_code")), _fact("Last command output", _output_excerpt(command))])
        next_action = "Correct the reported repository preflight or command failure before retrying publication."
        recipe_step = ""

    return {
        "phase": phase, "stage": stage, "code": code, "title": title,
        "reason": str(redact(str(error.get("message") or "The operation failed without a detailed message."))),
        "where": [row for row in where if row], "facts": [row for row in facts if row],
        "next_action": next_action, "recipe_step": recipe_step,
    }


def cancellation_projection(run: dict) -> dict | None:
    """Return neutral, presentation-safe cancellation detail without a failure diagnostic."""
    metadata = run.get("cancellation")
    if not isinstance(metadata, dict) or not metadata:
        return None
    status = str(run.get("status") or "")
    allowed = ("code", "reason", "phase", "stage", "requested_at", "completed_at")
    if status == "cancelled":
        kind, title, message = "cancelled", "Run cancelled", "Cancelled by user"
    elif status == "cancelling":
        kind, title, message = "cancelling", "Cancellation requested", "Cancellation is in progress"
    else:
        kind, title, message = "cancellation_requested", "Cancellation requested", "Cancellation was requested"
    return {
        **{key: metadata[key] for key in allowed if metadata.get(key) is not None},
        "kind": kind,
        "status": status,
        "title": title,
        "message": message,
    }


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
    detail = build_pipeline.execution_detail(run)
    detail["diagnostic"] = execution_diagnostic(run)
    detail["cancellation"] = cancellation_projection(run)
    return detail


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
