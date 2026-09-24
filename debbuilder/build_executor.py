"""Build plan validation, ordered execution, and output resolution."""
from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from .command_runner import (
    CommandValidationError,
    controlled_environment,
    parse_command,
    redact_arguments,
    redact_command,
    resolve_working_directory,
    run_command,
)
from .execution_cancellation import ExecutionCancelled


class BuildError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MAX_ENSURE_DIRECTORIES = 256
_MAX_ENSURE_DIRECTORY_LENGTH = 1024


def _post_build_parts(value, *, index: int) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise BuildError(
            "post_build_directory_invalid_path",
            "Post-build directory paths must be strings",
            details={"directory_index": index},
        )
    parsed = PurePosixPath(value)
    if (
        not value
        or not parsed.parts
        or len(value) > _MAX_ENSURE_DIRECTORY_LENGTH
        or any(ord(character) < 32 for character in value)
        or "\\" in value
        or parsed.is_absolute()
        or value != parsed.as_posix()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise BuildError(
            "post_build_directory_invalid_path",
            "Post-build directory must be a canonical relative POSIX path",
            details={"directory_index": index},
        )
    return parsed.parts


def _validate_post_build_directories(build: dict) -> list[str]:
    directories = build.get("ensure_directories") or []
    if not isinstance(directories, list):
        raise BuildError(
            "post_build_directory_invalid_path",
            "build.ensure_directories must be a list",
        )
    if len(directories) > _MAX_ENSURE_DIRECTORIES:
        raise BuildError(
            "post_build_directory_invalid_path",
            f"build.ensure_directories must contain at most {_MAX_ENSURE_DIRECTORIES} paths",
        )
    seen = set()
    output = build["output"]
    selected = [] if output.get("mode") == "source" else (
        [output.get("path")] if output.get("mode") == "path" else output.get("paths", [])
    )
    selected_parts = [PurePosixPath(path).parts for path in selected if isinstance(path, str)]
    for index, directory in enumerate(directories):
        parts = _post_build_parts(directory, index=index)
        if directory in seen:
            raise BuildError(
                "post_build_directory_invalid_path",
                "Post-build directory paths must be unique",
                details={"directory_index": index, "directory": directory},
            )
        seen.add(directory)
        if output.get("mode") != "source" and not any(parts[:len(parent)] == parent for parent in selected_parts):
            raise BuildError(
                "post_build_directory_invalid_path",
                "Post-build directory is not covered by build.output",
                details={"directory_index": index, "directory": directory},
            )
    return list(directories)


def _ensure_summary(entries: list[dict], requested: int) -> dict:
    return {
        "requested": requested,
        "created": sum(row["status"] == "created" for row in entries),
        "already_existed": sum(row["status"] == "already_existed" for row in entries),
        "entries": list(entries),
    }


def _directory_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _directory_open_error(parent_fd: int, part: str, relative: str, index: int, entries: list[dict], requested: int) -> BuildError:
    details = {
        "directory_index": index,
        "directory": relative,
        "ensure_directories": _ensure_summary(entries, requested),
    }
    try:
        kind = _directory_kind(os.stat(part, dir_fd=parent_fd, follow_symlinks=False).st_mode)
    except OSError:
        return BuildError(
            "post_build_directory_creation_failed",
            f"Post-build directory could not be created safely: {relative}",
            details=details,
        )
    details["actual_kind"] = kind
    if kind == "symlink":
        return BuildError(
            "post_build_directory_symlink",
            f"Post-build directory path contains a symbolic link: {relative}",
            details=details,
        )
    return BuildError(
        "post_build_directory_not_directory",
        f"Post-build directory path contains a non-directory entry: {relative}",
        details=details,
    )


def ensure_post_build_directories(source_directory: str | Path, directories: list[str], *, on_result=None) -> dict:
    """Create declared directories below a pinned source root without following links."""
    if not isinstance(directories, list) or len(directories) > _MAX_ENSURE_DIRECTORIES:
        raise BuildError(
            "post_build_directory_invalid_path",
            f"Post-build directories must be a list of at most {_MAX_ENSURE_DIRECTORIES} paths",
        )
    validated = []
    seen = set()
    for index, directory in enumerate(directories):
        parts = _post_build_parts(directory, index=index)
        if directory in seen:
            raise BuildError(
                "post_build_directory_invalid_path",
                "Post-build directory paths must be unique",
                details={"directory_index": index, "directory": directory},
            )
        seen.add(directory)
        validated.append((directory, parts))
    entries = []
    try:
        root_fd = os.open(os.path.abspath(source_directory), _DIRECTORY_FLAGS)
    except OSError as exc:
        raise BuildError(
            "post_build_directory_escape",
            "The acquired source root could not be pinned safely",
            details={"ensure_directories": _ensure_summary(entries, len(validated))},
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise BuildError(
                "post_build_directory_escape",
                "The acquired source root is not a real directory",
                details={"ensure_directories": _ensure_summary(entries, len(validated))},
            )
        for index, (relative, parts) in enumerate(validated):
            parent_fd = os.dup(root_fd)
            created_final = False
            try:
                for component_index, part in enumerate(parts):
                    created = False
                    try:
                        child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                    except FileNotFoundError:
                        try:
                            os.mkdir(part, 0o755, dir_fd=parent_fd)
                            created = True
                        except FileExistsError:
                            pass
                        except OSError as exc:
                            raise BuildError(
                                "post_build_directory_creation_failed",
                                f"Post-build directory could not be created: {relative}",
                                details={
                                    "directory_index": index,
                                    "directory": relative,
                                    "ensure_directories": _ensure_summary(entries, len(validated)),
                                },
                            ) from exc
                        try:
                            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                        except OSError as exc:
                            raise _directory_open_error(
                                parent_fd, part, relative, index, entries, len(validated),
                            ) from exc
                    except OSError as exc:
                        raise _directory_open_error(
                            parent_fd, part, relative, index, entries, len(validated),
                        ) from exc
                    os.close(parent_fd)
                    parent_fd = child_fd
                    if component_index == len(parts) - 1:
                        created_final = created
                entry = {"path": relative, "status": "created" if created_final else "already_existed"}
                entries.append(entry)
                if callable(on_result):
                    on_result(dict(entry), _ensure_summary(entries, len(validated)))
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    return _ensure_summary(entries, len(validated))


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
    ensure_directories = _validate_post_build_directories(build)
    is_static = detection.get("project_type") == "static"
    is_source_noop = detection.get("build_mode") == "source" and not build["commands"] and not detection.get("proposed_commands")
    if is_static and build["commands"]:
        raise BuildError("static_build_commands_not_allowed", "Static projects must not configure build commands")
    if is_static and build["output"]["mode"] != "source":
        raise BuildError("invalid_static_output", "Static projects require output.mode = source")
    selection = {"source": "static", "commands": [], "confirmed": True} if is_static else {"source": "detected_source", "commands": [], "confirmed": True} if is_source_noop else select_commands(build["commands"], detection.get("proposed_commands") or [], dry_run=dry_run)
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
        "inactivity_timeout": build.get("inactivity_timeout", 300),
        "maximum_runtime": build.get("maximum_runtime"),
        "ensure_directories": ensure_directories,
        "output": output,
    }


def execute_build(recipe: dict, detection: dict, source_directory: str | Path, *, dry_run: bool, runner=run_command, inactivity_timeout: float | None = None, maximum_runtime: float | None = None, on_result=None, on_output=None, on_directory_result=None, cancellation_event=None, on_cancel=None) -> dict:
    plan = validate_build_plan(recipe, detection, source_directory, dry_run=dry_run)
    if dry_run:
        return {"executed": False, "reason": "dry_run", "plan": plan, "commands": [], "output": plan["output"]}
    source_noop = detection.get("build_mode") == "source" and not recipe["build"]["commands"] and not detection.get("proposed_commands")
    actual_commands = [] if detection.get("project_type") == "static" or source_noop else select_commands(recipe["build"]["commands"], detection.get("proposed_commands") or [], dry_run=False)["commands"]
    inactivity_timeout = inactivity_timeout if inactivity_timeout is not None else recipe["build"].get("inactivity_timeout", 300)
    maximum_runtime = maximum_runtime if maximum_runtime is not None else recipe["build"].get("maximum_runtime")
    results = []
    for index, command in enumerate(actual_commands, 1):
        if cancellation_event is not None and cancellation_event.is_set():
            cancellation = on_cancel() if callable(on_cancel) else {}
            raise ExecutionCancelled(cancellation)
        kwargs = {
            "workspace": source_directory,
            "working_directory": recipe["build"]["working_directory"],
            "environment": recipe["build"]["environment"],
            "inactivity_timeout": inactivity_timeout,
            "maximum_runtime": maximum_runtime,
        }
        if callable(on_output):
            kwargs["on_output"] = lambda item, command_index=index: on_output(command_index, item)
        if cancellation_event is not None:
            kwargs["cancellation_event"] = cancellation_event
        if callable(on_cancel):
            kwargs["on_cancel"] = on_cancel
        try:
            result = runner(command, **kwargs)
        except ExecutionCancelled as exc:
            if exc.command_result is not None:
                result = {"index": index, **exc.command_result}
                results.append(result)
                if callable(on_result):
                    on_result(result)
                exc.command_result = result
            raise
        result = {"index": index, **result}
        results.append(result)
        if callable(on_result):
            on_result(result)
        if result.get("cancellation_requested") or result.get("status") == "cancelled":
            raise ExecutionCancelled(result.get("cancellation"), command_result=result)
        if result["status"] != "success":
            code = result.get("error_code") or ("build_command_timeout" if result.get("timed_out") else "build_command_failed")
            timeout_reason = result.get("timeout_reason")
            message = f"Build command {index} timed out ({timeout_reason})" if result.get("timed_out") and timeout_reason else f"Build command {index} timed out" if result.get("timed_out") else f"Build command {index} failed with exit code {result.get('exit_code')}"
            raise BuildError(code, message, details={"plan": plan, "commands": results, "failed_command": result})
        if cancellation_event is not None and cancellation_event.is_set():
            cancellation = on_cancel() if callable(on_cancel) else {}
            raise ExecutionCancelled(cancellation)
    if cancellation_event is not None and cancellation_event.is_set():
        cancellation = on_cancel() if callable(on_cancel) else {}
        raise ExecutionCancelled(cancellation)
    ensure_directories = None
    try:
        ensure_directories = ensure_post_build_directories(
            source_directory, plan["ensure_directories"], on_result=on_directory_result,
        )
        output = _safe_output(source_directory, recipe["build"]["output"], require_exists=True)
    except BuildError as exc:
        completed_ensure = {"ensure_directories": ensure_directories} if ensure_directories is not None else {}
        exc.details = {"plan": plan, "commands": results, **completed_ensure, **exc.details}
        raise
    reason = "static_noop" if detection.get("project_type") == "static" else "source_noop" if source_noop else "build"
    return {"executed": True, "reason": reason, "plan": plan, "commands": results, "ensure_directories": ensure_directories, "output": output}
