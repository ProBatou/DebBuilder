"""Inspectable Debian staging and artifact construction."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

from .command_runner import run_command
from .systemd_unit import generate_unit


class PackagingError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _stage_path(staging: Path, absolute: str) -> Path:
    if not absolute.startswith("/") or ".." in Path(absolute).parts:
        raise PackagingError("unsafe_install_path", f"Install path must be safe and absolute: {absolute}")
    target = (staging / absolute.lstrip("/")).resolve(strict=False)
    try:
        target.relative_to(staging.resolve())
    except ValueError as exc:
        raise PackagingError("unsafe_install_path", f"Install path escapes staging: {absolute}") from exc
    return target


def _copy_regular_tree(source: Path, destination: Path, *, allowed_root: Path | None = None) -> list[str]:
    copied = []
    if source.is_symlink() or not source.exists():
        raise PackagingError("invalid_install_content", "Resolved build output is missing or is a symbolic link")
    allowed_root = (allowed_root or source).resolve()
    entries = [source] if source.is_file() else sorted(source.rglob("*"))
    for entry in entries:
        relative = Path(entry.name) if source.is_file() else entry.relative_to(source)
        target = destination / relative
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode):
            link = Path(os.readlink(entry))
            if link.is_absolute():
                raise PackagingError("unsafe_install_symlink", f"Absolute symlink in install content: {relative}")
            try:
                entry.resolve(strict=True).relative_to(allowed_root)
            except (OSError, ValueError) as exc:
                raise PackagingError("unsafe_install_symlink", f"Symlink escapes install content: {relative}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
            copied.append(relative.as_posix())
        elif stat.S_ISDIR(mode):
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, target)
            target.chmod(entry.stat().st_mode & 0o777)
            copied.append(relative.as_posix())
        else:
            raise PackagingError("unsupported_install_entry", f"Unsupported special file in install content: {relative}")
    return copied


def _apply_modes(root: Path, directory_mode: str, file_mode: str) -> None:
    directory_bits, file_bits = int(directory_mode, 8), int(file_mode, 8)
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(directory_bits)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(file_bits | (0o111 if executable else 0))


def generate_control(recipe: dict, version: str) -> str:
    package = recipe["package"]
    if not package.get("maintainer"):
        raise PackagingError("invalid_debian_metadata", "Package maintainer is required")
    if not package.get("description"):
        raise PackagingError("invalid_debian_metadata", "Package description is required")
    description = package["description"].replace("\r", "").split("\n")
    lines = [
        f"Package: {package['name']}", f"Version: {version}",
        f"Section: {package.get('section') or 'misc'}", f"Priority: {package.get('priority') or 'optional'}",
        f"Architecture: {package['architecture']}", f"Maintainer: {package['maintainer']}",
    ]
    dependencies = package.get("runtime_dependencies") or []
    if dependencies:
        lines.append(f"Depends: {', '.join(dependencies)}")
    lines.append(f"Description: {description[0]}")
    lines += [f" {line or '.'}" for line in description[1:]]
    return "\n".join(lines) + "\n"


def _configured_scripts(recipe: dict, generated: dict[str, list[str]]) -> dict[str, str]:
    explicit = recipe["install"]["maintainer_scripts"]
    scripts = {}
    for name in ("preinst", "postinst", "prerm", "postrm"):
        parts = generated.get(name, [])
        custom = explicit.get(name, "")
        if not parts and not custom.strip():
            continue
        body = ["#!/bin/sh", "set -e", *parts]
        if custom:
            body += ["", "# Recipe-provided actions", custom]
        scripts[name] = "\n".join(body).rstrip() + "\n"
    return scripts


def prepare_staging(recipe: dict, build_result: dict, workspace: str | Path, *, preview: bool = False) -> dict:
    workspace = Path(workspace).resolve()
    staging = (workspace / "staging").resolve()
    staging.mkdir(parents=True, exist_ok=True)
    if any(staging.iterdir()):
        raise PackagingError("staging_not_empty", "Staging directory is not empty")
    staging.chmod(0o755)
    debian = staging / "DEBIAN"
    debian.mkdir(mode=0o755)
    install = recipe["install"]
    include_output = install["content"]["source"] != "configured_files"
    destination = _stage_path(staging, install["destination"]) if include_output else None
    if include_output:
        assert destination is not None
        destination.mkdir(parents=True, exist_ok=True)
    output = build_result["output"]
    output_rows = output.get("paths") if output.get("mode") == "paths" else [output]
    content_sources = [Path(row["path"]).resolve() for row in output_rows]
    for candidate in content_sources:
        try:
            candidate.relative_to(workspace / "source")
        except ValueError as exc:
            raise PackagingError("invalid_install_content", "Build output is outside workspace/source") from exc
    content_source = content_sources[0]
    content_available = all(path.exists() for path in content_sources)
    if not content_available and not preview:
        raise PackagingError("invalid_install_content", "Resolved build output does not exist")
    copied = []
    if content_available and include_output and destination:
        for candidate in content_sources:
            relative = candidate.relative_to(workspace / "source")
            target = destination / relative if output.get("mode") == "paths" and candidate.is_dir() else destination
            copied.extend((relative / row).as_posix() for row in _copy_regular_tree(candidate, target, allowed_root=workspace / "source"))
    preview_warnings = [] if content_available else ["Build output is unavailable because build commands are not executed during dry-run"]

    generated: dict[str, list[str]] = {}
    owner = install["owner"]
    postinst = generated.setdefault("postinst", [])
    if owner.get("create_group"):
        postinst.append(f"getent group {owner['group']} >/dev/null 2>&1 || addgroup --system {owner['group']}")
    if owner.get("create_user"):
        postinst.append(f"id -u {owner['user']} >/dev/null 2>&1 || adduser --system --ingroup {owner['group']} --no-create-home {owner['user']}")
    if include_output and (owner.get("user") or owner.get("group")):
        postinst.append(f"chown -R {owner['user']}:{owner['group']} {install['destination']}")

    conffiles, configurations = [], []
    for configured in install["config_files"]:
        destination_path = str(configured if isinstance(configured, str) else configured["destination"])
        source_name = destination_path.lstrip("/") if isinstance(configured, str) else str(configured["source"])
        source_file = (content_source / source_name).resolve(strict=False)
        try:
            source_file.relative_to(content_source)
        except ValueError as exc:
            raise PackagingError("unsafe_configuration_source", f"Configuration source escapes build output: {source_name}") from exc
        if source_file.is_symlink() or not source_file.is_file():
            if not preview:
                raise PackagingError("configuration_source_missing", f"Configuration source file is missing: {source_name}")
            preview_warnings.append(f"Configuration source is unavailable during preview: {source_name}")
        policy = install["config_policy"]
        if policy == "create_if_missing":
            template = staging / "usr" / "share" / recipe["package"]["name"] / "config-templates" / destination_path.lstrip("/")
            template.parent.mkdir(parents=True, exist_ok=True)
            if source_file.is_file():
                shutil.copyfile(source_file, template)
                template.chmod(int(install["file_mode"], 8))
            postinst.append(f"if [ ! -e {destination_path} ]; then install -D -m {install['file_mode']} {('/' + template.relative_to(staging).as_posix())} {destination_path}; fi")
            generated.setdefault("postrm", []).append(f"if [ \"$1\" = purge ]; then rm -f {destination_path}; fi")
            staged_path = template
        else:
            staged_path = _stage_path(staging, destination_path)
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            if source_file.is_file():
                shutil.copyfile(source_file, staged_path)
                staged_path.chmod(int(install["file_mode"], 8))
            if policy == "dpkg_conffile":
                conffiles.append(destination_path)
        configurations.append({"source": source_name, "destination": destination_path, "staged_path": "/" + staged_path.relative_to(staging).as_posix(), "policy": policy})
        if (owner.get("user"), owner.get("group")) != ("root", "root"):
            postinst.append(f"chown {owner['user']}:{owner['group']} {destination_path}")

    service = recipe["service"]
    unit_text, unit_path = "", ""
    if service["enabled"]:
        if not service.get("command"):
            raise PackagingError("invalid_systemd_service", "Enabled systemd service requires ExecStart command")
        try:
            unit_text = generate_unit(service)
        except ValueError as exc:
            raise PackagingError("invalid_systemd_service", str(exc)) from exc
        unit_target = staging / "usr" / "lib" / "systemd" / "system" / service["name"]
        unit_target.parent.mkdir(parents=True, exist_ok=True)
        unit_target.write_text(unit_text)
        unit_target.chmod(0o644)
        unit_path = "/" + unit_target.relative_to(staging).as_posix()
        postinst += ["systemctl daemon-reload || true", f"systemctl enable {service['name']} || true", f"systemctl restart {service['name']} || true"]
        generated.setdefault("prerm", []).append(f"if [ \"$1\" = remove ]; then systemctl stop {service['name']} || true; fi")
        generated.setdefault("postrm", []).append("systemctl daemon-reload || true")

    control = generate_control(recipe, build_result["version"])
    (debian / "control").write_text(control)
    if conffiles:
        (debian / "conffiles").write_text("\n".join(conffiles) + "\n")
    scripts = _configured_scripts(recipe, generated)
    for name, text in scripts.items():
        path = debian / name
        path.write_text(text)
        path.chmod(0o755)
    if include_output and destination:
        _apply_modes(destination, install["directory_mode"], install["file_mode"])
    debian.chmod(0o755)
    (debian / "control").chmod(0o644)
    if (debian / "conffiles").exists():
        (debian / "conffiles").chmod(0o644)
    return {
        "staging_directory": str(staging), "install_destination": install["destination"], "include_output": include_output,
        "content_source": str(content_source), "content_sources": [str(path) for path in content_sources], "content_available": content_available, "content_files": copied, "preview": preview, "warnings": preview_warnings,
        "version": build_result["version"], "control": control, "conffiles": conffiles, "configurations": configurations,
        "maintainer_scripts": scripts, "systemd": {"enabled": service["enabled"], "path": unit_path, "content": unit_text},
        "ownership": {"user": owner["user"], "group": owner["group"], "applied_by": "postinst"},
        "permissions": {"directories": install["directory_mode"], "files": install["file_mode"]},
    }


def validate_staging(staging_result: dict) -> dict:
    staging = Path(staging_result["staging_directory"])
    required = [staging / "DEBIAN/control"]
    if staging_result.get("include_output", True):
        required.append(_stage_path(staging, staging_result["install_destination"]))
    if not staging_result.get("preview"):
        required += [_stage_path(staging, row["staged_path"]) for row in staging_result.get("configurations", [])]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PackagingError("invalid_staging", f"Staging is missing required paths: {', '.join(missing)}")
    scripts = staging_result["maintainer_scripts"]
    for name in scripts:
        if (staging / "DEBIAN" / name).stat().st_mode & 0o777 != 0o755:
            raise PackagingError("invalid_staging", f"Maintainer script is not executable: {name}")
    return {"valid": True, "required_paths": [str(path) for path in required]}


def build_deb(recipe: dict, staging_result: dict, workspace: str | Path, *, runner=run_command, inspector=None) -> dict:
    workspace = Path(workspace).resolve()
    validate_staging(staging_result)
    package, version, architecture = recipe["package"]["name"], staging_result["version"], recipe["package"]["architecture"]
    filename = f"{package}_{version}_{architecture}.deb"
    artifact = workspace / "artifacts" / filename
    result = runner(f"dpkg-deb --build --root-owner-group staging artifacts/{filename}", workspace=workspace, working_directory=".", environment={"LC_ALL":"C"}, timeout=120)
    if result["status"] != "success" or not artifact.is_file():
        raise PackagingError("deb_build_failed", result["stderr"] or "dpkg-deb failed", details={"command": result})
    digest = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    artifact_details = {"name": filename, "path": str(artifact), "size": artifact.stat().st_size, "sha256": digest.hexdigest(), "build_command": result}
    try:
        inspection = inspector(artifact, workspace=workspace) if inspector else {}
    except (OSError, ValueError) as exc:
        raise PackagingError("deb_inspection_failed", str(exc), details={"artifact": artifact_details}) from exc
    if inspection and not inspection.get("ok"):
        raise PackagingError("deb_inspection_failed", "Generated Debian package failed inspection", details={"artifact": artifact_details, "inspection": inspection})
    if inspection and (inspection.get("package") != package or inspection.get("version") != version or inspection.get("architecture") != architecture):
        raise PackagingError("deb_inspection_failed", "Generated Debian package metadata does not match the Recipe", details={"artifact": artifact_details, "inspection": inspection})
    if inspection:
        from .deb_inspector import inspection_for_storage
        inspection = inspection_for_storage(inspection)
    return {**artifact_details, "inspection": inspection}
