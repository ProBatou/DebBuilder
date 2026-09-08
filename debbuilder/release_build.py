"""Build and inspect an official Debian Release asset from a local source tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from . import __version__, build_executor, builtin_recipe, deb_inspector, debian_packaging, project_detection
from .command_runner import run_command
from .recipe_schema import validate_recipe_metadata
from .runtime import RuntimeConfig
from .source_acquisition import SourceError, version_from_resolution
from .systemd_unit import generate_unit


PACKAGED_ENVIRONMENT_FILE = "/etc/debbuilder/debbuilder.env"
PROTECTED_RUNTIME_ROOTS = (Path("/var/lib/debbuilder"), Path("/opt/debbuilder"))
RELEASE_TAG = re.compile(r"v[0-9][A-Za-z0-9.+~_-]*\Z")


class ReleaseBuildError(RuntimeError):
    """Fail-closed error for an inconsistent or unsafe Release build."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_output_directory(output_directory: str | Path) -> Path:
    output = Path(output_directory).resolve(strict=False)
    if any(_is_below(output, root) for root in PROTECTED_RUNTIME_ROOTS):
        raise ReleaseBuildError(
            "unsafe_release_output",
            f"Release assets cannot be written below a production runtime path: {output}",
        )
    if output.exists():
        raise ReleaseBuildError("release_output_exists", f"Release output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _safe_temporary_parent(temporary_parent: str | Path | None) -> Path | None:
    if temporary_parent is None:
        return None
    parent = Path(temporary_parent).resolve(strict=True)
    if not parent.is_dir() or any(_is_below(parent, root) for root in PROTECTED_RUNTIME_ROOTS):
        raise ReleaseBuildError("unsafe_temporary_root", f"Release temporary state cannot use this path: {parent}")
    return parent


def release_plan(tag: str) -> dict:
    """Resolve Release identity entirely from the application and built-in Recipe."""
    if not RELEASE_TAG.fullmatch(str(tag or "")):
        raise ReleaseBuildError("invalid_release_tag", "Release tag must be v followed by a Debian-compatible version")
    definition = builtin_recipe.load_builtin_definition()
    recipe = validate_recipe_metadata(definition)
    try:
        upstream_version, debian_version = version_from_resolution(recipe, {"tag": tag, "ref": tag})
    except SourceError as exc:
        raise ReleaseBuildError(exc.code, str(exc)) from exc
    if upstream_version != __version__:
        raise ReleaseBuildError(
            "release_version_mismatch",
            f"Release tag version {upstream_version} does not match debbuilder.__version__ {__version__}",
        )
    package = recipe["package"]
    filename = f"{package['name']}_{debian_version}_{package['architecture']}.deb"
    return {
        "tag": tag,
        "upstream_version": upstream_version,
        "debian_version": debian_version,
        "debian_revision": package["version_revision"],
        "architecture": package["architecture"],
        "package": package["name"],
        "definition_version": definition["management"]["definition_version"],
        "filename": filename,
        "definition": definition,
        "recipe": recipe,
    }


def _safe_source_input(source_root: Path, relative_value: str) -> Path:
    relative = Path(relative_value)
    if not relative_value or relative.is_absolute() or ".." in relative.parts:
        raise ReleaseBuildError("unsafe_release_source", f"Canonical source input is unsafe: {relative_value}")
    current = source_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ReleaseBuildError("unsafe_release_source", f"Canonical source input contains a symbolic link: {relative_value}")
    resolved = current.resolve(strict=False)
    if not _is_below(resolved, source_root) or not resolved.exists():
        raise ReleaseBuildError("release_source_missing", f"Canonical source input is missing: {relative_value}")
    for entry in [resolved, *resolved.rglob("*")] if resolved.is_dir() else [resolved]:
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ReleaseBuildError("unsafe_release_source", f"Canonical source input contains an unsupported entry: {entry}")
    return resolved


def _copy_source_inputs(plan: dict, source_root: Path, workspace_source: Path) -> None:
    output = plan["recipe"]["build"]["output"]
    if output.get("mode") != "paths" or not output.get("paths"):
        raise ReleaseBuildError("unsupported_release_output", "Official Release builds require explicit canonical output paths")
    relative_inputs = list(output["paths"])
    relative_inputs.extend(mapping["source"] for mapping in plan["recipe"]["install"]["config_files"])
    for relative_value in dict.fromkeys(relative_inputs):
        source = _safe_source_input(source_root, relative_value)
        destination = workspace_source / relative_value
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source, destination, copy_function=shutil.copy2,
                ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
            )
        else:
            shutil.copy2(source, destination)


def _environment_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ReleaseBuildError("invalid_packaged_environment", f"Invalid packaged environment assignment: {line}")
        values[key] = value
    return values


def _dependency_names(value: str) -> set[str]:
    names = set()
    for relation in value.split(","):
        match = re.match(r"\s*([a-z0-9][a-z0-9+.-]*)", relation)
        if match:
            names.add(match.group(1))
    return names


def _extract_and_verify(plan: dict, staging: dict, artifact: dict, workspace: Path) -> dict:
    recipe = plan["recipe"]
    inspection = artifact["inspection"]
    expected_metadata = {
        "package": plan["package"],
        "version": plan["debian_version"],
        "architecture": plan["architecture"],
    }
    actual_metadata = {key: inspection.get(key, "") for key in expected_metadata}
    if actual_metadata != expected_metadata:
        raise ReleaseBuildError(
            "release_metadata_mismatch", "Generated package metadata does not match the canonical Release plan",
            details={"expected": expected_metadata, "actual": actual_metadata},
        )
    required_dependencies = set(recipe["package"]["runtime_dependencies"])
    actual_dependencies = _dependency_names(str(inspection.get("depends") or ""))
    if not required_dependencies.issubset(actual_dependencies):
        raise ReleaseBuildError(
            "release_dependencies_mismatch", "Generated package omits canonical runtime dependencies",
            details={"required": sorted(required_dependencies), "actual": sorted(actual_dependencies)},
        )

    inventory = {str(row.get("path") or "") for row in inspection.get("files") or []}
    if any(path == "./opt/debbuilder/data" or path.startswith("./opt/debbuilder/data/") for path in inventory):
        raise ReleaseBuildError("mutable_payload_in_package", "Generated package contains mutable application data")
    if any("/__pycache__/" in path or path.endswith((".pyc", ".pyo")) for path in inventory):
        raise ReleaseBuildError("generated_cache_in_package", "Generated package contains Python cache files")

    extract_root = workspace / "inspection-root"
    command = f"dpkg-deb --extract artifacts/{plan['filename']} inspection-root"
    extracted = run_command(
        command, workspace=workspace, working_directory=".", environment={"LC_ALL": "C"}, timeout=30,
    )
    if extracted["status"] != "success":
        raise ReleaseBuildError("release_extract_failed", extracted["stderr"] or "Unable to extract generated package")

    unit_relative = Path(staging["systemd"]["path"].lstrip("/"))
    unit_path = extract_root / unit_relative
    if not unit_path.is_file():
        raise ReleaseBuildError("release_service_missing", "Generated package omits its canonical systemd unit")
    unit = unit_path.read_text()
    expected_unit = generate_unit(recipe["service"])
    if unit != expected_unit:
        raise ReleaseBuildError("release_service_mismatch", "Packaged systemd unit differs from the canonical Recipe")
    environment_lines = [line for line in unit.splitlines() if line.startswith("EnvironmentFile=")]
    expected_environment_line = f"EnvironmentFile={PACKAGED_ENVIRONMENT_FILE}"
    if environment_lines != [expected_environment_line]:
        raise ReleaseBuildError(
            "release_environment_file_mismatch", "Packaged service does not load exactly the canonical environment file",
            details={"actual": environment_lines, "expected": [expected_environment_line]},
        )

    mapping = next(
        (row for row in staging["configurations"] if row["destination"] == PACKAGED_ENVIRONMENT_FILE), None,
    )
    if mapping is None or mapping["policy"] != "create_if_missing":
        raise ReleaseBuildError("release_environment_install_missing", "Canonical packaged environment installation is missing")
    template_path = extract_root / mapping["staged_path"].lstrip("/")
    if not template_path.is_file():
        raise ReleaseBuildError("release_environment_template_missing", "Generated package omits its environment template")
    postinst_path = workspace / "logs" / "deb-control" / "postinst"
    expected_install = (
        f"if [ ! -e {mapping['destination']} ]; then install -D -m {mapping['mode']} "
        f"{mapping['staged_path']} {mapping['destination']}; fi"
    )
    if not postinst_path.is_file() or expected_install not in postinst_path.read_text():
        raise ReleaseBuildError("release_environment_install_missing", "Packaged postinst does not install its environment file")
    runtime = RuntimeConfig.from_environment(Path(recipe["install"]["destination"]), _environment_values(template_path))
    if runtime.data != Path("/var/lib/debbuilder"):
        raise ReleaseBuildError(
            "release_runtime_path_mismatch", "Packaged runtime configuration does not place mutable data in /var/lib/debbuilder",
            details={"actual": str(runtime.data)},
        )
    return {
        "metadata": actual_metadata,
        "depends": inspection.get("depends", ""),
        "file_count": inspection.get("file_count", len(inventory)),
        "unit_path": "/" + unit_relative.as_posix(),
        "unit": unit,
        "runtime_data_directory": str(runtime.data),
        "mutable_application_data_present": False,
        "generated_python_cache_present": False,
    }


def build_release_artifacts(
    *, tag: str, source_root: str | Path, output_directory: str | Path,
    temporary_parent: str | Path | None = None,
) -> dict:
    """Build checked Release assets without touching DebBuilder runtime state."""
    plan = release_plan(tag)
    source = Path(source_root).resolve(strict=True)
    if not source.is_dir():
        raise ReleaseBuildError("invalid_release_source", f"Release source is not a directory: {source}")
    output = _safe_output_directory(output_directory)
    temporary_parent_path = _safe_temporary_parent(temporary_parent)

    with tempfile.TemporaryDirectory(prefix="debbuilder-release-build-", dir=temporary_parent_path) as temporary:
        workspace = Path(temporary)
        for name in ("source", "staging", "artifacts", "logs"):
            (workspace / name).mkdir()
        _copy_source_inputs(plan, source, workspace / "source")
        detection = project_detection.detect_project(workspace / "source", working_directory=plan["recipe"]["build"]["working_directory"])
        build = build_executor.execute_build(plan["recipe"], detection, workspace / "source", dry_run=False)
        staging = debian_packaging.prepare_staging(
            plan["recipe"], {**build, "version": plan["debian_version"]}, workspace,
        )
        debian_packaging.validate_staging(staging)
        artifact = debian_packaging.build_deb(
            plan["recipe"], staging, workspace, inspector=deb_inspector.inspect_deb,
        )
        if artifact["name"] != plan["filename"] or Path(artifact["path"]).parent != workspace / "artifacts":
            raise ReleaseBuildError("release_artifact_mismatch", "Packaging returned an unexpected artifact path")
        checks = _extract_and_verify(plan, staging, artifact, workspace)

        with tempfile.TemporaryDirectory(prefix=".debbuilder-release-assets-", dir=output.parent) as publish_temporary:
            publish = Path(publish_temporary)
            published_artifact = publish / plan["filename"]
            shutil.copy2(artifact["path"], published_artifact)
            digest = hashlib.sha256(published_artifact.read_bytes()).hexdigest()
            if digest != artifact["sha256"]:
                raise ReleaseBuildError("release_checksum_mismatch", "Copied Release artifact checksum changed")
            (publish / "SHA256SUMS").write_text(f"{digest}  {plan['filename']}\n")
            os.replace(publish, output)

    return {
        "tag": plan["tag"],
        "package": plan["package"],
        "upstream_version": plan["upstream_version"],
        "debian_version": plan["debian_version"],
        "debian_revision": plan["debian_revision"],
        "architecture": plan["architecture"],
        "definition_version": plan["definition_version"],
        "artifact": {
            "name": plan["filename"], "path": str(output / plan["filename"]),
            "size": artifact["size"], "sha256": artifact["sha256"],
        },
        "sha256sums": str(output / "SHA256SUMS"),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, for example v0.2.1")
    parser.add_argument("--source-root", default=".", help="Checked-out tagged source tree")
    parser.add_argument("--output-dir", default="release-assets", help="New directory for the .deb and SHA256SUMS")
    arguments = parser.parse_args(argv)
    try:
        result = build_release_artifacts(
            tag=arguments.tag, source_root=arguments.source_root, output_directory=arguments.output_dir,
        )
    except (ReleaseBuildError, debian_packaging.PackagingError, build_executor.BuildError, project_detection.DetectionError, OSError, ValueError) as exc:
        parser.exit(1, f"release build failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
