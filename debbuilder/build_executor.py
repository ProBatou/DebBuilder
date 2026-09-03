"""Build plan validation, ordered execution, and output resolution."""
from __future__ import annotations

from pathlib import Path

from .command_runner import (
    CommandValidationError,
    controlled_environment,
    parse_command,
    redact_arguments,
    redact_command,
    resolve_working_directory,
    run_command,
)


class BuildError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def select_commands(configured: list[str], proposed: list[str], *, dry_run: bool) -> dict:
    if configured:
        return {"source": "recipe", "commands": list(configured), "confirmed": True}
    if dry_run and proposed:
        return {"source": "detection_proposal", "commands": list(proposed), "confirmed": False}
    if proposed:
        raise BuildError(
            "build_commands_not_confirmed",
            "Detected build commands must be reviewed and saved in the Recipe before a real build.",
            details={"proposed_commands": list(proposed)},
        )
    raise BuildError("build_commands_missing", "No build commands are configured or proposed")


def _safe_output(source_directory: str | Path, output: dict, *, require_exists: bool) -> dict:
    source = Path(source_directory).resolve()
    mode = output.get("mode")
    if mode == "source":
        return {"mode": "source", "path": str(source), "exists": source.is_dir(), "kind": "directory"}
    if mode == "paths":
        paths = [_safe_path_output(source, relative, require_exists=require_exists) for relative in output.get("paths", [])]
        return {"mode": "paths", "paths": paths, "exists": all(row["exists"] for row in paths), "kind": "collection"}
    if mode != "path":
        raise BuildError("invalid_output_mode", f"Unsupported build output mode: {mode}")
    return _safe_path_output(source, str(output.get("path") or ""), require_exists=require_exists)


def _safe_path_output(source: Path, relative: str, *, require_exists: bool) -> dict:
    raw = Path(relative)
    if not relative or raw.is_absolute() or ".." in raw.parts:
        raise BuildError("unsafe_output_path", "Build output path must be relative and remain in workspace/source")
    current = source
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise BuildError("unsafe_output_path", f"Build output path contains a symbolic link: {relative}")
    target = (source / raw).resolve(strict=False)
    try:
        target.relative_to(source)
    except ValueError as exc:
        raise BuildError("unsafe_output_path", "Build output path escapes workspace/source") from exc
    exists = target.exists()
    result = {"mode": "path", "configured_path": relative, "path": str(target), "exists": exists, "kind": "directory" if target.is_dir() else "file" if target.is_file() else "missing"}
    if require_exists and not exists:
        raise BuildError("expected_output_missing", f"Expected build output does not exist: {relative}", details={"output": result})
    return result


def validate_build_plan(recipe: dict, detection: dict, source_directory: str | Path, *, dry_run: bool) -> dict:
    build = recipe["build"]
    is_static = detection.get("project_type") == "static"
    if is_static and build["commands"]:
        raise BuildError("static_build_commands_not_allowed", "Static projects must not configure build commands")
    if is_static and build["output"]["mode"] != "source":
        raise BuildError("invalid_static_output", "Static projects require output.mode = source")
    selection = {"source": "static", "commands": [], "confirmed": True} if is_static else select_commands(build["commands"], detection.get("proposed_commands") or [], dry_run=dry_run)
    try:
        cwd = resolve_working_directory(source_directory, build["working_directory"])
        environment = controlled_environment(source_directory, build["environment"])
        commands = []
        for command in selection["commands"]:
            arguments = parse_command(command)
            commands.append({"command": redact_command(command, arguments, environment), "arguments": redact_arguments(arguments, environment)})
    except CommandValidationError as exc:
        raise BuildError("invalid_build_command", str(exc)) from exc
    output = _safe_output(source_directory, build["output"], require_exists=False)
    return {
        "selection": {**selection, "commands": [item["command"] for item in commands]},
        "commands": commands,
        "working_directory": str(cwd),
        "configured_working_directory": build["working_directory"],
        "environment_keys": sorted(build["environment"]),
        "timeout": build.get("timeout", 120),
        "output": output,
    }


def execute_build(recipe: dict, detection: dict, source_directory: str | Path, *, dry_run: bool, runner=run_command, timeout: float | None = None, on_result=None) -> dict:
    plan = validate_build_plan(recipe, detection, source_directory, dry_run=dry_run)
    if dry_run:
        return {"executed": False, "reason": "dry_run", "plan": plan, "commands": [], "output": plan["output"]}
    actual_commands = [] if detection.get("project_type") == "static" else select_commands(recipe["build"]["commands"], detection.get("proposed_commands") or [], dry_run=False)["commands"]
    timeout = timeout if timeout is not None else recipe["build"].get("timeout", 120)
    results = []
    for index, command in enumerate(actual_commands, 1):
        result = runner(
            command,
            workspace=source_directory,
            working_directory=recipe["build"]["working_directory"],
            environment=recipe["build"]["environment"],
            timeout=timeout,
        )
        result = {"index": index, **result}
        results.append(result)
        if callable(on_result):
            on_result(result)
        if result["status"] != "success":
            code = "build_command_timeout" if result.get("timed_out") else "build_command_failed"
            message = f"Build command {index} timed out" if result.get("timed_out") else f"Build command {index} failed with exit code {result.get('exit_code')}"
            raise BuildError(code, message, details={"plan": plan, "commands": results, "failed_command": result})
    try:
        output = _safe_output(source_directory, recipe["build"]["output"], require_exists=True)
    except BuildError as exc:
        exc.details = {"plan": plan, "commands": results, **exc.details}
        raise
    return {"executed": True, "reason": "static_noop" if detection.get("project_type") == "static" else "build", "plan": plan, "commands": results, "output": output}
