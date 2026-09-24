"""Read, render, and clean canonical Build Run execution data."""
from __future__ import annotations

import json
from typing import Callable

from . import artifact_publication, execution_projection, workspace_cleanup
from .build_store import BuildStore
from .command_runner import redact_command
from .execution_projection import public_error, safe_text
from .recipe_schema import require_safe_name


STAGE_LABELS = {
    "source": "Source", "detection": "Project detection", "dependencies": "Dependencies",
    "source_changes": "Source changes", "build": "Build", "staging": "Debian installation",
    "debian_metadata": "Debian metadata", "systemd": "Service", "package": "Debian package",
    "artifact": "Artifact",
}


def _private_environment_values(run: dict) -> tuple[str, ...]:
    """Collect configured build-environment values for output-only redaction."""
    values = set()
    for step in run.get("steps") or []:
        if not isinstance(step, dict):
            continue
        details = step.get("details") if isinstance(step.get("details"), dict) else {}
        plan = details.get("plan") if isinstance(details.get("plan"), dict) else {}
        environment = plan.get("environment") if isinstance(plan.get("environment"), dict) else {}
        values.update(str(value) for value in environment.values() if value is not None and str(value))
    return tuple(sorted(values, key=len, reverse=True))


def _redact_environment_text(value, private_values: tuple[str, ...]) -> str:
    text = str(value or "")
    for private in private_values:
        text = text.replace(private, "[redacted-env]")
    return text


def _redact_environment_values(value, private_values: tuple[str, ...]):
    if isinstance(value, dict):
        return {key: _redact_environment_values(item, private_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_environment_values(item, private_values) for item in value]
    if isinstance(value, str):
        return _redact_environment_text(value, private_values)
    return value


def _fact(label: str, value) -> dict | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value if item is not None and item != "")
    if value == "":
        return None
    return {"label": label, "value": safe_text(value, limit=600)}


def _command_text(command: dict) -> str:
    value = str(command.get("command") or "")
    arguments = command.get("arguments") or []
    if not isinstance(arguments, list):
        arguments = []
    if not value and arguments:
        value = " ".join(str(item) for item in arguments)
    return safe_text(redact_command(value, [str(item) for item in arguments]), limit=600)


def _output_excerpt(command: dict) -> str:
    text = str(command.get("stderr") or command.get("stdout") or "").strip()
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip()][-3:]
    excerpt = safe_text("\n".join(lines), limit=600)
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
    if publications and isinstance(publications[-1], dict):
        publication = publications[-1]
        if artifact_publication.publication_attempt_status(publication, run=run) == "failed":
            if publication.get("status") == "success":
                publication = {
                    **publication,
                    "error": {
                        "code": "publication_proof_invalid",
                        "message": "Publication success has no valid matching PublicationProofV1",
                        "details": {},
                    },
                }
            return "publication", publication
    validations = run.get("_validation_attempts") if isinstance(run.get("_validation_attempts"), list) else []
    if validations and isinstance(validations[-1], dict) and validations[-1].get("status") == "failed":
        return "validation", validations[-1]
    if run.get("status") == "failed" and run.get("error"):
        return "build", {"error": run["error"]}
    return None, None


def execution_diagnostic(run: dict) -> dict | None:
    """Build a compact presentation contract from canonical lifecycle records.

    The result is derived at read time from the current Run and its canonical
    lifecycle records.
    """
    phase, record = _diagnostic_source(run)
    if not record:
        return None
    error = record.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = safe_text(error.get("code") or f"{phase}_failed", limit=128)
    stage = safe_text(error.get("stage") or ("validation" if phase == "validation" else "publication" if phase == "publication" else "build"), limit=64)
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
        where += filter(None, [_fact("Command index", command.get("index"))])
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
        where += filter(None, [_fact("Configured path", output.get("configured_path"))])
        facts.append(_fact("Observed", output.get("kind") or "missing"))
        next_action = "Update the expected output path or make the build command create it."
    elif code.startswith("post_build_directory_"):
        title = "Post-build directory preparation failed"
        where += filter(None, [_fact("Configured path", details.get("directory"))])
        facts += filter(None, [_fact("Observed", details.get("actual_kind"))])
        next_action = "Use a safe relative directory covered by Build output and remove any conflicting file or symbolic link."
    elif stage == "source_changes":
        failed = details.get("failed") if isinstance(details.get("failed"), dict) else details
        title = "Source change could not be applied"
        where += filter(None, [_fact("Change", error.get("change_index") or failed.get("index")), _fact("File", failed.get("path"))])
        facts += filter(None, [_fact("Operation", failed.get("operation")), _fact("Matches", failed.get("matches"))])
        next_action = f"Review Source change {error.get('change_index') or failed.get('index') or ''} against the resolved upstream source.".rstrip()
    elif code in {"configuration_source_missing", "unsafe_configuration_source", "unsafe_install_path"}:
        title = "Debian install mapping failed"
        where += filter(None, [_fact("Mapping", details.get("mapping_index")), _fact("Source", details.get("source")), _fact("Destination", details.get("destination"))])
        facts += filter(None, [_fact("Cause", details.get("cause"))])
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

    return _redact_environment_values({
        "phase": phase, "stage": stage, "code": code, "title": title,
        "reason": safe_text(error.get("message") or "The operation failed without a detailed message.", limit=600),
        "where": [row for row in where if row], "facts": [row for row in facts if row],
        "next_action": next_action, "recipe_step": recipe_step,
    }, _private_environment_values(run))


def cancellation_projection(run: dict) -> dict | None:
    """Return neutral, presentation-safe cancellation detail without a failure diagnostic."""
    metadata = run.get("cancellation")
    if not isinstance(metadata, dict) or not metadata:
        return None
    status = safe_text(run.get("status"), limit=32)
    allowed = ("code", "reason", "phase", "stage", "requested_at", "completed_at")
    reason = safe_text(metadata.get("reason"), limit=128)
    if status == "cancelled":
        messages = {
            "user_requested": "Cancelled by user",
            "server_shutdown": "Cancelled during server shutdown",
        }
        kind, title, message = "cancelled", "Run cancelled", messages.get(reason, "Execution cancelled")
    elif status == "cancelling":
        kind, title, message = "cancelling", "Cancellation requested", "Cancellation is in progress"
    else:
        kind, title, message = "cancellation_requested", "Cancellation requested", "Cancellation was requested"
    return {
        **{
            key: safe_text(metadata[key], limit=128)
            for key in allowed if metadata.get(key) is not None
        },
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
        (lambda summary: {
            **summary,
            "package": safe_text(package_resolver(run), limit=256),
        })(execution_projection.public_summary(run))
        for run in selected
    ]


def get_execution(store: BuildStore, run_id: str, *, run: dict | None = None) -> dict | None:
    require_safe_name(run_id, "execution")
    run = run if run is not None else store.load(run_id)
    if not run or store.execution_history_deleted(run_id, run):
        return None
    detail = execution_projection.public_detail(run)
    detail["diagnostic"] = execution_diagnostic(run)
    detail["cancellation"] = cancellation_projection(run)
    return detail


def _error_lines(run: dict) -> list[str]:
    lines = []
    if run.get("error"):
        error = run["error"]
        lines.append(safe_text(error.get("message", str(error)) if isinstance(error, dict) else str(error), limit=2000))
    for step in run.get("steps", []):
        if step.get("error"):
            error = step["error"]
            lines.append(safe_text(f"{step.get('name')}: {error.get('message', str(error)) if isinstance(error, dict) else str(error)}", limit=2000))
    return lines


def format_log(run: dict, *, verbosity: str = "normal") -> str:
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    private_values = _private_environment_values(run)
    if verbosity == "compact":
        rows = [safe_text(f"{step['name']}: {step['status']}", limit=2000) for step in run.get("steps", []) if step.get("status") != "pending"]
        rows.extend(f"error: {line}" for line in _error_lines(run))
        return _redact_environment_text("\n".join(rows) + ("\n" if rows else ""), private_values)
    if verbosity == "normal":
        rows = [safe_text(f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}", limit=2000) for step in run.get("steps", []) if step.get("status") != "pending"]
        events = [safe_text(event.get("message"), limit=2000) for event in run.get("events", []) if any(marker in str(event.get("message") or "") for marker in ("Build tools", "Dependencies", "Build command", "validation", "publication"))]
        rows.extend(events)
        rows.extend(
            safe_text(f"validation {validation.get('id', '')}: {validation.get('status', '')} ({validation.get('phase', 'lifecycle')})", limit=2000)
            for validation in run.get("_validation_attempts") or []
        )
        rows.extend(f"error: {line}" for line in _error_lines(run))
        return _redact_environment_text("\n".join(row for row in rows if row) + ("\n" if rows else ""), private_values)
    rows = []
    for step in run.get("steps", []):
        if step.get("status") == "pending":
            continue
        rows.append(safe_text(f"{step['name']}: {step['status']}{(' - ' + step.get('summary', '')) if step.get('summary') else ''}", limit=2000))
        details = step.get("details") or {}
        for command in details.get("commands") or []:
            rows.append(safe_text(f"Build command {command.get('index')}: {command.get('status')} cwd={command.get('working_directory', '')} duration={command.get('duration', '')}s", limit=2000))
            if command.get("stdout"):
                rows.append("stdout:\n" + safe_text(command["stdout"].rstrip(), limit=20_000))
            if command.get("stderr"):
                rows.append("stderr:\n" + safe_text(command["stderr"].rstrip(), limit=20_000))
    for validation in run.get("_validation_attempts") or []:
        rows.append(safe_text(f"validation {validation.get('id', '')}: {validation.get('status', '')}", limit=2000))
        if validation.get("error"):
            rows.append(safe_text(json.dumps(public_error(validation["error"]), indent=2, ensure_ascii=False), limit=20_000))
    for publication in run.get("publications") or []:
        rows.append(safe_text(
            f"publication {publication.get('id', '')}: {artifact_publication.publication_attempt_status(publication, run=run)}",
            limit=2000,
        ))
    rows.extend(f"error: {line}" for line in _error_lines(run))
    return _redact_environment_text("\n".join(row for row in rows if row) + ("\n" if rows else ""), private_values)


def get_log(store: BuildStore, run_id: str, *, verbosity: str = "normal", after: int = 0, run: dict | None = None) -> dict | None:
    require_safe_name(run_id, "execution")
    run = run if run is not None else store.load(run_id)
    verbosity = verbosity if verbosity in {"compact", "normal", "verbose", "raw"} else "normal"
    if not run or store.execution_history_deleted(run_id, run):
        return None
    lifecycle_complete = not execution_projection.public_summary(run)["lifecycle_active"]
    if verbosity == "raw":
        # Cursor over the already-redacted representation. Redacting only a
        # caller-selected suffix lets an offset begin after e.g. ``password=``
        # and remove the context required by pattern-based filtering.
        snapshot = store.log_slice(run_id, 0)
        filtered = _redact_environment_text(snapshot.get("text", ""), _private_environment_values(run))
        filtered = safe_text(filtered, limit=max(20_000, len(filtered)))
        start = max(0, min(int(after or 0), len(filtered)))
        return {
            "text": filtered[start:], "offset": len(filtered), "size": len(filtered),
            "complete": lifecycle_complete, "verbosity": verbosity,
        }
    rendered = format_log(run, verbosity=verbosity)
    start = max(0, min(int(after or 0), len(rendered)))
    return {"text": rendered[start:], "offset": len(rendered), "size": len(rendered), "complete": lifecycle_complete, "verbosity": verbosity}


def delete_log(store: BuildStore, run_id: str, *, authorization=None) -> dict:
    require_safe_name(run_id, "execution")
    return {
        **store.clear_log_history(run_id, authorization=authorization),
        "history_deleted": True,
        "visible": False,
    }


def delete_logs(
    store: BuildStore,
    run_ids: list[str] | None = None,
    *,
    all_runs: bool = False,
    dry_run: bool = False,
    authorization=None,
) -> dict:
    selected = workspace_cleanup.completed_history_ids(store) if all_runs else list(run_ids or [])
    if dry_run:
        return {"count": len(selected), "ids": selected}
    deleted, errors = [], []
    for run_id in selected:
        try:
            deleted.append(delete_log(store, str(run_id), authorization=authorization))
        except Exception as exc:
            errors.append({"id": str(run_id), "error": str(exc)})
    return {"deleted": deleted, "errors": errors}
