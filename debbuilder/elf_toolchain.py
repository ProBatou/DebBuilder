"""Explicit #27 preparation and offline OCI session for Bookworm ELF resolution."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import stat

from .artifact_validation import _local_apt_arguments, _prepare_lifecycle_inputs
from .build_store import BuildStore
from .command_runner import run_command
from .debian_packaging import build_deb
from .dependency_preparation import inspect_artifact, prepare_runtime_dependencies
from .elf_dependency_resolution import (
    MAX_ELF_FILES, OwnedContainerResolutionEnvironment, ResolutionError,
    _merge_depends, _staged, resolve_elf_dependencies,
)
from .elf_inspection import ElfInspectionError, inspect_elf
from .elf_override_snapshot import filtered_snapshot
from .recipe_schema import DEBIAN_RELATION
from .validation_backend import OwnedOciSystemdBackend
from .validation_service import ValidationManager


TOOLCHAIN_PACKAGE = "debbuilder-elf-toolchain"
TOOLCHAIN_DEPENDS = "dpkg-dev, binutils"
MAX_PAYLOAD_CANDIDATES = 256


def _toolchain_artifact(store: BuildStore, extra_relations: tuple[str, ...]) -> tuple[dict, Path]:
    recipe = {
        "schema_version": 5, "name": TOOLCHAIN_PACKAGE,
        "package": {"name": TOOLCHAIN_PACKAGE, "architecture": "all"},
        "source": {"repository": "debbuilder/elf-toolchain"},
    }
    run = store.create(recipe, mode="build")
    workspace = Path(run["workspace"])
    debian = workspace / "staging/DEBIAN"
    debian.mkdir(mode=0o755)
    (debian / "control").write_text(
        f"Package: {TOOLCHAIN_PACKAGE}\nVersion: 1.0-1\nArchitecture: all\n"
        "Section: devel\nPriority: optional\nMaintainer: DebBuilder <noreply@example.invalid>\n"
        f"Depends: {', '.join([TOOLCHAIN_DEPENDS, *extra_relations])}\nDescription: Temporary ELF resolver toolchain selector\n",
        encoding="utf-8",
    )
    artifact = build_deb(
        {"package": {"name": TOOLCHAIN_PACKAGE, "architecture": "all"}},
        {"staging_directory": str(workspace / "staging"), "version": "1.0-1", "include_output": False,
         "maintainer_scripts": {}},
        workspace,
    )
    metadata = inspect_artifact(artifact["path"], workspace=workspace)
    run["status"] = "success"
    run["artifact"] = {
        "path": artifact["path"], "name": artifact["name"],
        "inspection": {"package": metadata.package, "version": metadata.version,
                       "architecture": metadata.architecture, "depends": metadata.depends},
    }
    store.save(run)
    return run, Path(artifact["path"])


@contextmanager
def prepared_bookworm_toolchain(staging_root: Path, workspace: Path, *, extra_relations: tuple[str, ...] = (),
                                filtered_entrypoints: dict | None = None, ignored_sonames: frozenset[str] = frozenset(),
                                cancellation_event=None):
    """Yield a networkless owned container after #27's signed APT preparation."""
    workspace = workspace.resolve(strict=True)
    staging_root = staging_root.resolve(strict=True)
    if len(extra_relations) > 32 or any(not DEBIAN_RELATION.fullmatch(row) for row in extra_relations):
        raise ResolutionError("invalid_dependency_override", "Toolchain package additions are invalid")
    nested = BuildStore(workspace / "elf-toolchain" / "builds")
    run, artifact = _toolchain_artifact(nested, extra_relations)
    nested_workspace = Path(run["workspace"])
    # The parent BuildStore uses <data>/builds/<run-id>; share #27's
    # <data>/validation-containers registry so all owned OCI work observes
    # the same overlap and recovery boundary.
    registry_root = workspace.parent.parent / "validation-containers"
    manager = ValidationManager(
        nested, execute=lambda *_args: None, registry_root=registry_root,
        workspace_root=nested.root.parent,
    )
    with nested.locked_run(run["id"]):
        admission = manager._admit_locked(
            run["id"], nested.load(run["id"]), profile_name="bookworm",
            previous_artifact="", automatic=False, publish_after_success=False,
        )
    prepared = prepare_runtime_dependencies(
        run["id"], admission["id"], store=nested, current_artifact=artifact,
        profile_name="bookworm", registry_root=registry_root,
        cancellation_event=cancellation_event,
    )["prepared"]
    if prepared["native_architecture"] != "amd64" or "dpkg-dev" not in {row["package"] for row in prepared["packages"]}:
        raise ResolutionError("resolver_environment_incomplete", "Bookworm toolchain preparation did not select dpkg-dev/amd64")
    validation_dir = nested_workspace / "validation" / admission["id"]
    validation_dir.mkdir(parents=True, exist_ok=True)
    bundle = _prepare_lifecycle_inputs(
        workspace=nested_workspace, validation_dir=validation_dir,
        attempt_id=admission["id"], current_artifact=artifact, previous_artifact=None,
        prepared_dependencies=prepared,
        selected_profile={"name": "bookworm", "image": prepared["image"]["name"]},
        runner=run_command, cancellation_event=cancellation_event,
    )
    work = nested_workspace / "elf-work"
    (work / "debian").mkdir(parents=True, mode=0o700)
    (work / "debian/control").write_text(
        "Source: debbuilder-elf-proposal\nPackage: debbuilder-elf-proposal\nArchitecture: any\n"
        "Description: internal dependency proposal\n", encoding="utf-8",
    )
    filtered_installed = set()
    for installed, inspection in (filtered_entrypoints or {}).items():
        matches = set(inspection.needed) & ignored_sonames
        if not matches:
            continue
        source = _staged(staging_root, installed)
        destination = work / "filtered" / installed.lstrip("/")
        filtered_snapshot(source, destination, inspection, matches)
        filtered_installed.add(installed)
    if filtered_installed:
        (work / "filtered/DEBIAN").mkdir(parents=True, exist_ok=True)
        (work / "filtered/DEBIAN/control").write_bytes((staging_root / "DEBIAN/control").read_bytes())
    backend = OwnedOciSystemdBackend(
        nested_workspace, image=prepared["image"]["name"],
        run_id=run["id"], attempt_id=admission["id"], registry_root=registry_root,
        resource_policy=run["resource_limits"]["effective"],
        mounts=[*bundle["mounts"], (staging_root, "/debbuilder-staging", "ro"),
                (work, "/debbuilder-elf-work", "ro"),
                (Path(__file__).with_name("bounded_process.py"), "/debbuilder-input/bounded_process.py", "ro")],
        expected_image=prepared["image"], cancellation_event=cancellation_event,
    )
    try:
        started = backend.start(admission["id"])
        if started["network"] != "disabled" or not started["network_verified"]:
            raise ResolutionError("resolver_environment_mismatch", "Bookworm resolver network isolation is unverified")
        installed = backend.exec(_local_apt_arguments(
            conffile_option="--force-confnew",
            package_paths=[*bundle["phase_paths"]["current"], bundle["current_artifact"]],
        ), timeout=300)
        if not installed.get("accepted"):
            raise ResolutionError("resolver_environment_incomplete", "Prepared Bookworm toolchain could not be installed offline")
        owner = backend.exec(["dpkg-query", "-S", "/usr/bin/dpkg-shlibdeps"], timeout=30)
        if not owner.get("accepted") or not str(owner.get("stdout") or "").startswith("dpkg-dev: "):
            raise ResolutionError("resolver_environment_incomplete", "dpkg-dev does not own the prepared dpkg-shlibdeps")
        yield OwnedContainerResolutionEnvironment(backend.container, filtered_installed=frozenset(filtered_installed))
    finally:
        backend.stop()


def analyze_staged_payload(recipe: dict, staging_root: Path, content_files: list[str],
                           install_destination: str, workspace: Path, *, cancellation_event=None) -> dict:
    """Inspect only opted-in Release-asset payload files after final staging."""
    policy = recipe["package"]["runtime_dependency_detection"]
    if not policy["enabled"]:
        return {"status": "disabled", "manual_packages": list(recipe["package"]["runtime_dependencies"]),
                "detected_packages": [], "bundled_requirements": 0, "overridden_requirements": 0, "unresolved_requirements": 0,
                "effective_depends": list(recipe["package"]["runtime_dependencies"])}
    if len(content_files) > MAX_PAYLOAD_CANDIDATES:
        raise ResolutionError("resolver_traversal_limit", "Release-asset payload contains too many candidate files")
    staging_root = staging_root.resolve(strict=True)
    entrypoints = {}
    for relative in sorted(set(content_files)):
        installed = install_destination.rstrip("/") + "/" + relative
        candidate = staging_root / installed.lstrip("/")
        if not candidate.is_relative_to(staging_root) or candidate.is_symlink():
            continue
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise ResolutionError("staged_elf_missing", "Selected payload file disappeared from final staging") from exc
        if not stat.S_ISREG(mode):
            continue
        with candidate.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        try:
            inspection = inspect_elf(candidate)
        except ElfInspectionError as exc:
            raise ResolutionError(exc.code, str(exc)) from exc
        if inspection.kind != "elf":
            raise ResolutionError("malformed_elf", "Selected Release-asset ELF is malformed")
        entrypoints[installed] = inspection
    if len(entrypoints) > MAX_ELF_FILES:
        raise ResolutionError("resolver_traversal_limit", "Release-asset ELF count exceeds its bound")
    manual = list(recipe["package"]["runtime_dependencies"])
    if not any(result.linkage == "dynamic" for result in entrypoints.values()):
        if policy["overrides"]:
            raise ResolutionError("invalid_dependency_override", "No dynamic ELF requirement matches an override")
        return {"status": "success", "manual_packages": manual, "detected_packages": [],
                "bundled_requirements": 0, "overridden_requirements": 0, "unresolved_requirements": 0,
                "effective_depends": sorted(set(manual)), "elf_count": len(entrypoints)}
    mappings = tuple(row["relation"] for row in policy["overrides"] if row["action"] == "manual")
    ignored = frozenset(row["soname"] for row in policy["overrides"])
    with prepared_bookworm_toolchain(staging_root, workspace, extra_relations=mappings,
                                     filtered_entrypoints=entrypoints, ignored_sonames=ignored,
                                     cancellation_event=cancellation_event) as environment:
        result = resolve_elf_dependencies(
            staging_root, entrypoints, distribution="bookworm", architecture="amd64",
            environment=environment, overrides=tuple(policy["overrides"]),
        )
        if result.status != "success":
            raise ResolutionError("resolver_environment_mismatch", "Bookworm resolution is unsupported")
        effective = _merge_depends(environment, set([*manual, *result.depends]))
    return {
        "status": "success", "manual_packages": manual, "detected_packages": list(result.depends),
        "bundled_requirements": sum(row.status == "bundled" for row in result.requirements),
        "overridden_requirements": sum(row.status in {"ignored_explicitly", "manually_resolved"} for row in result.requirements),
        "unresolved_requirements": 0,
        "effective_depends": list(effective),
        "elf_count": len(entrypoints), "requirement_count": len(result.requirements),
    }
