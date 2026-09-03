"""Debian package inspection through the central command runner."""
from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

from .command_runner import run_command

CONTROL_KEYS = ["Package", "Version", "Architecture", "Depends", "Maintainer", "Description", "Homepage", "Section", "Priority"]


def inspection_for_storage(inspection: dict, limit: int | None = None) -> dict:
    """Add lifecycle metadata without discarding the complete package inventory.

    The BuildStore owns representation compaction and moves ``files`` to a Run
    manifest.  Keeping the complete list here ensures that externalization never
    turns an old preview limit into permanent data loss.  ``limit`` remains an
    accepted compatibility argument but is intentionally ignored.
    """
    stored = dict(inspection)
    files = list(stored.get("files") or [])
    stored["service_units"] = [row for row in files if str(row.get("path", "")).endswith(".service") and "/systemd/" in str(row.get("path", ""))]
    stored["file_count"] = stored.get("file_count", len(files))
    return stored


def _invoke(arguments: list[str], workspace: Path, runner=run_command) -> dict:
    command = " ".join(shlex.quote(argument) for argument in arguments)
    return runner(command, workspace=workspace, working_directory=".", environment={"LC_ALL":"C"}, timeout=30)


def _control_fields(deb: Path, workspace: Path, runner=run_command) -> dict:
    result = _invoke(["dpkg-deb", "-f", str(deb)], workspace, runner)
    if result["status"] != "success":
        raise ValueError(result["stderr"].strip() or "dpkg-deb -f failed")
    fields, current = {}, None
    for raw in result["stdout"].splitlines():
        if raw.startswith(" ") and current:
            fields[current] += "\n" + raw[1:]
        elif ": " in raw:
            key, value = raw.split(": ", 1)
            fields[key], current = value, key
    return fields


def _file_list(deb: Path, workspace: Path, runner=run_command) -> list[dict]:
    result = _invoke(["dpkg-deb", "-c", str(deb)], workspace, runner)
    if result["status"] != "success":
        return []
    files = []
    for line in result["stdout"].splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) >= 6:
            files.append({"mode": parts[0], "owner": parts[1], "size": parts[2], "date": " ".join(parts[3:5]), "path": parts[5]})
    return files


def _control_metadata(deb: Path, workspace: Path, runner=run_command) -> tuple[list[str], list[str]]:
    control_dir = workspace / "logs" / "deb-control"
    control_dir.mkdir(parents=True, exist_ok=True)
    result = _invoke(["dpkg-deb", "--control", str(deb), str(control_dir)], workspace, runner)
    if result["status"] != "success":
        return [], []
    scripts = sorted(name for name in ("preinst", "postinst", "prerm", "postrm") if (control_dir / name).is_file())
    conffiles_path = control_dir / "conffiles"
    conffiles = [line.strip().split()[0] for line in conffiles_path.read_text().splitlines() if line.strip()] if conffiles_path.is_file() else []
    return scripts, conffiles


def inspect_deb(path: str | Path, *, workspace: str | Path | None = None, runner=run_command) -> dict:
    deb = Path(path).resolve()
    if not deb.exists():
        raise FileNotFoundError(str(deb))
    if workspace is None:
        with tempfile.TemporaryDirectory(prefix="debbuilder-inspect-") as temporary:
            return inspect_deb(deb, workspace=temporary, runner=runner)
    root = Path(workspace).resolve()
    fields = _control_fields(deb, root, runner)
    files = _file_list(deb, root, runner)
    scripts, conffiles = _control_metadata(deb, root, runner)
    warnings = [f"missing {key}" for key in ("Package", "Version", "Architecture") if not fields.get(key)]
    return {
        "ok": not warnings, "path": str(deb), "size": deb.stat().st_size,
        "package": fields.get("Package", ""), "version": fields.get("Version", ""),
        "architecture": fields.get("Architecture", ""), "depends": fields.get("Depends", ""),
        "maintainer": fields.get("Maintainer", ""), "description": fields.get("Description", ""),
        "homepage": fields.get("Homepage", ""), "control": fields, "files": files,
        "file_count": len(files), "maintainer_scripts": scripts, "conffiles": conffiles, "warnings": warnings,
    }
