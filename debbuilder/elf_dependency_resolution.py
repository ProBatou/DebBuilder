"""Bookworm/amd64 ELF dependency proposals from a prepared Debian environment.

The caller supplies an already prepared, owned OCI container with the final
staging tree mounted read-only at ``/debbuilder-staging`` and a small working
directory containing ``debian/control``. No host dpkg database is consulted.
This module does not edit a Recipe or DEBIAN/control.
"""
from __future__ import annotations

import posixpath
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .elf_inspection import ElfInspectionError, ElfInspectionResult, inspect_elf
from .validation_images import admitted_image
from .recipe_schema import normalize_runtime_dependency_detection


CONTRACT_VERSION = 1
MAX_ELF_FILES = 128
MAX_REQUIREMENTS = 512
MAX_DEPTH = 16
MAX_COMMAND_BYTES = 1024 * 1024
COMMAND_TIMEOUT = 30
MOUNT_ROOT = "/debbuilder-staging"
WORK_ROOT = "/debbuilder-elf-work"
DEPENDENCY = re.compile(r"([a-z0-9][a-z0-9+.-]*)(?: \(>= ([A-Za-z0-9.+:~\-]+)\))?")
SYMBOLS = re.compile(r"^dpkg-shlibdeps: debug: Using symbols file /var/lib/dpkg/info/([a-z0-9.+-]+):amd64\.symbols for (\S+)$")
SHLIBS = re.compile(r"^dpkg-shlibdeps: debug: Using shlibs\+objdump for (\S+) \(file (/\S+)\)$")
FOUND = re.compile(r"^dpkg-shlibdeps: debug: Library (\S+) found in (/\S+)$")


class ResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, requirements: tuple[Requirement, ...] = ()):
        super().__init__(message)
        self.code = code
        self.requirements = requirements


class PreparedBookwormEnvironment(Protocol):
    """An owned, prepared container; see ``OwnedContainerResolutionEnvironment``."""

    def execute(self, arguments: list[str], *, timeout: float) -> dict: ...


@dataclass(frozen=True)
class Requirement:
    requester: str
    soname: str
    selected_library: str
    status: str  # bundled, resolved_external, unresolved
    package: str = ""
    version_constraint: str = ""
    metadata_source: str = ""
    operator_decision: str = ""


@dataclass(frozen=True)
class ResolutionResult:
    contract_version: int
    status: str  # success, unsupported; failures raise ResolutionError
    distribution: str
    architecture: str
    depends: tuple[str, ...]
    requirements: tuple[Requirement, ...]
    diagnostics: tuple[str, ...]


class OwnedContainerResolutionEnvironment:
    """Use #27's owned container and bounded-process helper, without a new OCI lifecycle."""

    def __init__(self, container, *, filtered_installed: frozenset[str] = frozenset()):
        identity = container.identity
        expected = admitted_image("bookworm", architecture="amd64")["digest"]
        if (identity.get("state") != "running" or identity.get("image", {}).get("digest") != expected
                or identity.get("role") != "lifecycle"
                or identity.get("configuration", {}).get("network") != "none"):
            raise ResolutionError("resolver_environment_mismatch", "Resolver requires a networkless owned Bookworm/amd64 lifecycle container")
        mounts = identity.get("configuration", {}).get("mounts", [])
        required = {MOUNT_ROOT, WORK_ROOT, "/debbuilder-input/bounded_process.py"}
        if not required.issubset({mount.get("destination") for mount in mounts}):
            raise ResolutionError("resolver_environment_incomplete", "Resolver staging, work directory or bounded helper is not mounted")
        if any(not mount.get("read_only") for mount in mounts if mount.get("destination") in required):
            raise ResolutionError("resolver_environment_incomplete", "Resolver inputs must be mounted read-only")
        self.container = container
        self.filtered_installed = filtered_installed

    def execute(self, arguments: list[str], *, timeout: float) -> dict:
        result = self.container.exec([
            "env", "-i", "LC_ALL=C", "PATH=/usr/bin:/bin", "python3",
            "/debbuilder-input/bounded_process.py", "--limit", str(MAX_COMMAND_BYTES),
            "--", *arguments,
        ], timeout=timeout, check=False)
        return result


def _run(environment: PreparedBookwormEnvironment, arguments: list[str], *, filtered: bool = False) -> str:
    result = environment.execute(arguments, timeout=COMMAND_TIMEOUT)
    stdout, stderr = str(result.get("stdout") or ""), str(result.get("stderr") or "")
    if len(stdout.encode()) + len(stderr.encode()) > MAX_COMMAND_BYTES:
        raise ResolutionError("resolver_output_limit", "Debian resolver output exceeds its bound")
    if result.get("timed_out"):
        raise ResolutionError("resolver_timeout", "Debian resolver command timed out")
    if result.get("exit_code") != 0:
        raise ResolutionError("resolver_command_failed", (stderr or stdout or "Debian resolver command failed")[:1000])
    if stderr.strip() and not (filtered and all(
        re.fullmatch(r"dpkg-shlibdeps: warning: /debbuilder-elf-work/filtered/[^\n]+ contains an unresolvable reference to symbol [^\n]+: it's probably a plugin", line)
        for line in stderr.strip().splitlines()
    )):
        raise ResolutionError("resolver_command_output_invalid", "Unexpected Debian resolver diagnostic output")
    return stdout


def _relative(path: str) -> str:
    if not path.startswith("/") or "\\" in path or "\x00" in path:
        raise ResolutionError("unsafe_staging_path", "Installed ELF path is invalid")
    normalized = posixpath.normpath(path)
    if normalized == "/" or normalized != path or any(part in {".", ".."} for part in PurePosixPath(path).parts):
        raise ResolutionError("unsafe_staging_path", "Installed ELF path is not canonical")
    return normalized


def _staged(root: Path, installed: str) -> Path:
    path = root
    for part in PurePosixPath(_relative(installed)).parts[1:]:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ResolutionError("staged_elf_missing", "Installed ELF path is missing") from exc
        if stat.S_ISLNK(mode) or (path != root / installed.lstrip("/") and not stat.S_ISDIR(mode)):
            raise ResolutionError("unsafe_staging_path", "Installed ELF path contains an unsafe component")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ResolutionError("unsafe_staging_path", "Installed ELF path is not a regular file")
    return path


def _staged_bundled(root: Path, installed: str) -> tuple[str, Path]:
    """Follow only payload-local symlinks; absolute link targets are installed paths."""
    current = _relative(installed)
    followed = 0
    while True:
        path = root
        parts = PurePosixPath(current).parts[1:]
        redirected = False
        for index, part in enumerate(parts):
            path = path / part
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                code = "staged_elf_missing" if followed == 0 else "bundled_library_invalid"
                raise ResolutionError(code, "Bundled library path is missing") from exc
            if stat.S_ISLNK(mode):
                followed += 1
                if followed > 16:
                    raise ResolutionError("bundled_library_invalid", "Bundled library symlink chain exceeds its bound")
                target = path.readlink().as_posix()
                base = target if target.startswith("/") else posixpath.join("/", *parts[:index], target)
                current = posixpath.normpath(posixpath.join(base, *parts[index + 1:]))
                redirected = True
                break
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise ResolutionError("bundled_library_invalid", "Bundled library path has a non-directory component")
        if not redirected:
            return current, _staged(root, current)


def _private_search_paths(requester: str, inspection: ElfInspectionResult) -> tuple[str, ...]:
    entries = inspection.runpath if inspection.runpath else inspection.rpath
    result = []
    for entry in entries:
        expanded = re.sub(r"\$(?:\{ORIGIN\}|ORIGIN)(?=/|$)", posixpath.dirname(requester), entry)
        if "$" in expanded or not expanded.startswith("/"):
            continue
        canonical = posixpath.normpath(expanded)
        if canonical.startswith("/") and canonical not in result:
            result.append(canonical)
    return tuple(result)


def _bundled(root: Path, requester: str, inspection: ElfInspectionResult, soname: str) -> str:
    if "/" in soname or soname in {".", ".."}:
        raise ResolutionError("invalid_soname", "ELF SONAME is not a safe basename")
    for directory in _private_search_paths(requester, inspection):
        candidate = posixpath.join(directory, soname)
        try:
            selected, _ = _staged_bundled(root, candidate)
        except ResolutionError as exc:
            if exc.code == "staged_elf_missing":
                continue
            raise
        return selected
    return ""


def _parse_shlibdeps(output: str, needed: tuple[str, ...]) -> tuple[dict[str, tuple[str, str, str]], tuple[str, ...], dict[str, tuple[str, ...]]]:
    sources: dict[str, tuple[str, str, str]] = {}
    found: dict[str, list[str]] = {}
    depends_line = ""
    for line in output.splitlines():
        if not (line.startswith("dpkg-shlibdeps: debug: ") or line.startswith("shlibs:Depends=")):
            raise ResolutionError("resolver_output_invalid", "Unexpected dpkg-shlibdeps output line")
        match = FOUND.fullmatch(line)
        if match:
            paths = found.setdefault(match.group(1), [])
            if match.group(2) not in paths:
                paths.append(match.group(2))
        match = SYMBOLS.fullmatch(line)
        if match:
            package, soname = match.groups()
            if soname in sources and sources[soname][:2] != (package, "symbols"):
                raise ResolutionError("resolver_mapping_ambiguous", "Debian metadata maps a SONAME ambiguously")
            sources[soname] = (package, "symbols", found.get(soname, [""])[0])
        match = SHLIBS.fullmatch(line)
        if match:
            soname, path = match.groups()
            if soname in sources and sources[soname] != ("", "shlibs", path):
                raise ResolutionError("resolver_mapping_ambiguous", "Debian metadata maps a SONAME ambiguously")
            sources[soname] = ("", "shlibs", path)
        if line.startswith("shlibs:Depends="):
            if depends_line:
                raise ResolutionError("resolver_output_invalid", "Duplicate shlibs:Depends output")
            depends_line = line.removeprefix("shlibs:Depends=")
    if not depends_line and needed:
        raise ResolutionError("resolver_output_invalid", "dpkg-shlibdeps produced no dependencies for a dynamic ELF")
    depends = tuple(part.strip() for part in depends_line.split(",") if part.strip())
    for relation in depends:
        if not DEPENDENCY.fullmatch(relation):
            raise ResolutionError("resolver_output_invalid", "dpkg-shlibdeps returned an unsupported relation")
    if any(soname not in sources or not sources[soname][2] for soname in needed):
        raise ResolutionError("soname_unresolved", "Debian resolver did not explain every external SONAME")
    return sources, depends, {name: tuple(paths) for name, paths in found.items()}


def _dependency_for(package: str, depends: tuple[str, ...]) -> str:
    matches = [row for row in depends if DEPENDENCY.fullmatch(row).group(1) == package]
    if len(matches) != 1:
        raise ResolutionError("resolver_mapping_ambiguous", "Debian package mapping does not match the proposed Depends")
    return matches[0]


def _merge_depends(environment: PreparedBookwormEnvironment, relations: set[str]) -> tuple[str, ...]:
    selected: dict[str, str] = {}
    for relation in sorted(relations):
        match = DEPENDENCY.fullmatch(relation)
        assert match is not None
        package, version = match.groups()
        previous = selected.get(package)
        if previous is None:
            selected[package] = relation
            continue
        older = DEPENDENCY.fullmatch(previous)
        assert older is not None
        old_version = older.group(2)
        if old_version is None:
            selected[package] = relation
        elif version is not None:
            compared = environment.execute(["dpkg", "--compare-versions", version, "gt", old_version], timeout=COMMAND_TIMEOUT)
            if compared.get("timed_out") or compared.get("exit_code") not in {0, 1} or compared.get("stdout") or compared.get("stderr"):
                raise ResolutionError("resolver_command_failed", "Debian version comparison failed")
            if compared["exit_code"] == 0:
                selected[package] = relation
    return tuple(selected[package] for package in sorted(selected))


def _resolve_one(environment: PreparedBookwormEnvironment, installed: str, needed: tuple[str, ...]) -> tuple[dict[str, tuple[str, str, str]], tuple[str, ...], dict[str, tuple[str, ...]]]:
    filtered = installed in getattr(environment, "filtered_installed", ())
    selected = f"{WORK_ROOT}/filtered{installed}" if filtered else f"{MOUNT_ROOT}{installed}"
    output = _run(environment, [
        "env", "-i", "-C", WORK_ROOT, "LC_ALL=C", "PATH=/usr/bin:/bin", "DEB_HOST_ARCH=amd64", "dpkg-shlibdeps", "-v", "-O",
        f"-S{MOUNT_ROOT}", f"-e{selected}",
    ], filtered=filtered)
    return _parse_shlibdeps(output, needed)


def _owner(environment: PreparedBookwormEnvironment, path: str) -> str:
    if not path.startswith("/") or path.startswith(MOUNT_ROOT + "/") or "\n" in path:
        raise ResolutionError("resolver_mapping_ambiguous", "Selected Debian library path is invalid")
    canonical = _canonical(environment, path)
    try:
        ownership = _run(environment, ["dpkg-query", "-S", path]).strip().splitlines()
    except ResolutionError as exc:
        if path == canonical or exc.code != "resolver_command_failed":
            raise
        ownership = _run(environment, ["dpkg-query", "-S", canonical]).strip().splitlines()
    if len(ownership) != 1 or ": " not in ownership[0]:
        raise ResolutionError("resolver_mapping_ambiguous", "Shared-library ownership is ambiguous")
    package = ownership[0].split(": ", 1)[0].split(":", 1)[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package):
        raise ResolutionError("resolver_mapping_ambiguous", "Shared-library owner is invalid")
    return package


def _canonical(environment: PreparedBookwormEnvironment, path: str) -> str:
    canonical = _run(environment, ["realpath", "-e", path]).strip()
    if not canonical.startswith("/") or "\n" in canonical:
        raise ResolutionError("resolver_mapping_ambiguous", "Selected Debian library path is invalid")
    return canonical


def resolve_elf_dependencies(
    staging_root: str | Path, entrypoints: dict[str, ElfInspectionResult], *,
    distribution: str, architecture: str, environment: PreparedBookwormEnvironment | None,
    overrides: tuple[dict, ...] = (),
) -> ResolutionResult:
    """Return a bounded proposal; all Debian commands run in the supplied target OCI environment."""
    if distribution != "bookworm" or architecture != "amd64":
        return ResolutionResult(CONTRACT_VERSION, "unsupported", distribution, architecture, (), (), ("Automatic resolution supports Bookworm/amd64 only",))
    if environment is None:
        raise ResolutionError("resolver_environment_missing", "A prepared Bookworm/amd64 environment is required")
    try:
        decisions = {row["soname"]: row for row in normalize_runtime_dependency_detection(
            {"enabled": True, "overrides": list(overrides)},
        )["overrides"]}
    except ValueError as exc:
        raise ResolutionError("invalid_dependency_override", str(exc)) from exc
    root = Path(staging_root)
    if root.is_symlink() or not root.is_dir():
        raise ResolutionError("unsafe_staging_path", "Staging root must be a real directory")
    root = root.resolve(strict=True)
    if isinstance(environment, OwnedContainerResolutionEnvironment):
        mounted = next(row for row in environment.container.identity["configuration"]["mounts"] if row["destination"] == MOUNT_ROOT)
        if Path(mounted["source"]).resolve(strict=True) != root:
            raise ResolutionError("resolver_environment_mismatch", "Mounted staging tree does not match the inspected staging tree")
    if ((root / "DEBIAN").is_symlink() or (root / "DEBIAN/control").is_symlink()
            or not (root / "DEBIAN").is_dir() or not (root / "DEBIAN/control").is_file()):
        raise ResolutionError("resolver_environment_incomplete", "Final staging must contain DEBIAN/control for dpkg-shlibdeps")
    if len(entrypoints) > MAX_ELF_FILES:
        raise ResolutionError("resolver_traversal_limit", "ELF entrypoint count exceeds its bound")
    release = _run(environment, ["cat", "/etc/os-release"])
    if not re.search(r'^VERSION_CODENAME=bookworm$', release, re.M):
        raise ResolutionError("resolver_environment_mismatch", "Prepared environment is not Debian Bookworm")
    if _run(environment, ["dpkg", "--print-architecture"]).strip() != "amd64":
        raise ResolutionError("resolver_environment_mismatch", "Prepared environment is not amd64")
    try:
        _run(environment, ["test", "-x", "/usr/bin/dpkg-shlibdeps"])
        _run(environment, ["test", "-f", WORK_ROOT + "/debian/control"])
    except ResolutionError as exc:
        if exc.code == "resolver_command_failed":
            raise ResolutionError("resolver_environment_incomplete", "Bookworm dpkg-dev is not prepared in the target environment") from exc
        raise
    rows: list[Requirement] = []
    depends: set[str] = set()
    visited: set[str] = set()
    active: set[str] = set()

    def walk(installed: str, inspection: ElfInspectionResult, depth: int) -> None:
        installed = _relative(installed)
        if installed in active or installed in visited:
            return
        if depth > MAX_DEPTH or len(visited) >= MAX_ELF_FILES:
            raise ResolutionError("resolver_traversal_limit", "Bundled ELF traversal exceeds its bound")
        staged_file = _staged(root, installed)
        try:
            verified = inspect_elf(staged_file, expected_sha256=inspection.sha256)
        except ElfInspectionError as exc:
            raise ResolutionError(exc.code, str(exc)) from exc
        if verified != inspection:
            raise ResolutionError("elf_identity_mismatch", "Staged ELF inspection differs from supplied metadata")
        visited.add(installed)
        if inspection.kind == "not_elf":
            return
        if inspection.kind != "elf":
            raise ResolutionError("malformed_elf", "A staged ELF is malformed")
        if inspection.debian_architecture != "amd64":
            raise ResolutionError("elf_architecture_mismatch", "ELF architecture differs from Bookworm/amd64")
        active.add(installed)
        try:
            if len(rows) + len(inspection.needed) > MAX_REQUIREMENTS:
                raise ResolutionError("resolver_traversal_limit", "ELF requirement count exceeds its bound")
            bundled = {soname: _bundled(root, installed, inspection, soname) for soname in inspection.needed}
            external = tuple(soname for soname in inspection.needed if not bundled[soname] and
                             soname not in decisions)
            if inspection.linkage == "dynamic":
                try:
                    sources, proposed, found = _resolve_one(environment, installed, external)
                except ResolutionError as exc:
                    if exc.code in {"soname_unresolved", "resolver_command_failed", "resolver_mapping_ambiguous"}:
                        exc.requirements = tuple(rows) + tuple(
                            Requirement(installed, soname, "", "unresolved") for soname in external
                        )
                    raise
                depends.update(proposed)
            else:
                sources, proposed, found = {}, (), {}
            if inspection.interpreter:
                owner = _owner(environment, inspection.interpreter)
                _dependency_for(owner, proposed)
            for soname in inspection.needed:
                if len(rows) >= MAX_REQUIREMENTS:
                    raise ResolutionError("resolver_traversal_limit", "ELF requirement count exceeds its bound")
                local = bundled[soname]
                decision = decisions.get(soname)
                if local:
                    if decision:
                        raise ResolutionError("invalid_dependency_override", "Override targets a bundled requirement")
                    child = inspect_elf(_staged(root, local))
                    if child.kind != "elf" or child.soname != soname:
                        raise ResolutionError("bundled_library_invalid", "Bundled library is not a matching valid ELF")
                    rows.append(Requirement(installed, soname, local, "bundled"))
                    walk(local, child, depth + 1)
                    continue
                if decision and decision["action"] == "ignore":
                    rows.append(Requirement(installed, soname, "", "ignored_explicitly",
                                            operator_decision=decision["reason"]))
                    continue
                if decision and decision["action"] == "manual":
                    relation = decision["relation"]
                    depends.add(relation)
                    rows.append(Requirement(installed, soname, "", "manually_resolved",
                                            DEPENDENCY.fullmatch(relation).group(1), relation,
                                            "operator_mapping", decision["reason"]))
                    continue
                source = sources.get(soname)
                if source is None:
                    raise ResolutionError("soname_unresolved", f"Required SONAME {soname} was not resolved", requirements=tuple(rows) + (Requirement(installed, soname, "", "unresolved"),))
                package, metadata, path = source
                selected = _canonical(environment, path)
                if any(_canonical(environment, candidate) != selected for candidate in found.get(soname, ())):
                    raise ResolutionError("resolver_mapping_ambiguous", "Debian resolver found different libraries for one SONAME")
                owner = _owner(environment, path)
                if metadata == "shlibs":
                    package = owner
                elif package != owner:
                    raise ResolutionError("resolver_mapping_ambiguous", "Debian symbols metadata disagrees with installed library owner")
                relation = _dependency_for(package, proposed)
                rows.append(Requirement(installed, soname, path,
                                        "resolved_external", package, relation, metadata))
        finally:
            active.remove(installed)

    for installed, inspection in sorted(entrypoints.items()):
        walk(installed, inspection, 0)
    if set(decisions) - {row.soname for row in rows}:
        raise ResolutionError("invalid_dependency_override", "Override does not match an observed ELF requirement")
    return ResolutionResult(CONTRACT_VERSION, "success", distribution, architecture, _merge_depends(environment, depends), tuple(rows), ())
