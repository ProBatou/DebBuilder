"""Resolve and prepare immutable, per-Run Node.js build toolchains."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import storage

NODE_RELEASES_URL = "https://nodejs.org/dist/index.json"
NODE_DIST_URL = "https://nodejs.org/dist"
NPM_REGISTRY_URL = "https://registry.npmjs.org"
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 192 * 1024 * 1024
SAFE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class NodeToolchainError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"[vV]?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-[0-9A-Za-z.-]+)?", str(value).strip())
    return tuple(map(int, match.groups())) if match else None


def _partial(value: str) -> tuple[list[int], bool] | None:
    value = value.strip().lstrip("vV")
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    numbers, wildcard = [], False
    for part in parts:
        if part.lower() in {"x", "*"}:
            wildcard = True
            break
        if wildcard or not part.isdigit():
            return None
        numbers.append(int(part))
    return numbers, wildcard or len(numbers) < 3


def _compare(actual: tuple[int, int, int], operator: str, expected: tuple[int, int, int]) -> bool:
    return {"=": actual == expected, "==": actual == expected, ">": actual > expected,
            ">=": actual >= expected, "<": actual < expected, "<=": actual <= expected}[operator]


def _simple_range_satisfies(actual: tuple[int, int, int], expression: str) -> bool | None:
    expression = expression.strip()
    hyphen = re.fullmatch(r"([^\s]+)\s+-\s+([^\s]+)", expression)
    if hyphen:
        low = _partial(hyphen.group(1))
        high = _partial(hyphen.group(2))
        if not low or not high:
            return None
        lo = tuple((low[0] + [0, 0, 0])[:3])
        high_numbers = high[0]
        if len(high_numbers) == 3:
            hi = tuple(high_numbers)
            return lo <= actual <= hi
        hi = tuple((high_numbers + [0, 0, 0])[:3])
        upper = (hi[0] + 1, 0, 0) if len(high_numbers) == 1 else (hi[0], hi[1] + 1, 0)
        return lo <= actual < upper
    tokens = [token for token in re.split(r"[\s,]+", expression) if token]
    if not tokens:
        return None
    for token in tokens:
        match = re.fullmatch(r"(\^|~|>=|<=|>|<|==|=)?(.+)", token)
        if not match:
            return None
        operator, raw = match.groups()
        parsed = _partial(raw)
        if not parsed:
            return None
        numbers, partial = parsed
        lower = tuple((numbers + [0, 0, 0])[:3])
        if operator == "^":
            if not numbers:
                return None
            upper = (lower[0] + 1, 0, 0) if lower[0] else ((0, lower[1] + 1, 0) if lower[1] else (0, 0, lower[2] + 1))
            satisfied = lower <= actual < upper
        elif operator == "~":
            upper = (lower[0] + 1, 0, 0) if len(numbers) == 1 else (lower[0], lower[1] + 1, 0)
            satisfied = lower <= actual < upper
        elif operator in {">", ">=", "<", "<=", "=", "=="}:
            if partial and operator in {"=", "=="}:
                upper = (lower[0] + 1, 0, 0) if len(numbers) == 1 else (lower[0], lower[1] + 1, 0)
                satisfied = lower <= actual < upper
            else:
                satisfied = _compare(actual, operator, lower)
        elif partial:
            upper = (lower[0] + 1, 0, 0) if len(numbers) == 1 else (lower[0], lower[1] + 1, 0)
            satisfied = lower <= actual < upper
        else:
            satisfied = actual == lower
        if not satisfied:
            return False
    return True


def version_satisfies(version: str, requirement: str) -> bool | None:
    """Evaluate the stable npm range forms used by Node engines metadata."""
    actual = _version(version)
    if actual is None or not isinstance(requirement, str) or not requirement.strip():
        return None
    results = [_simple_range_satisfies(actual, branch) for branch in requirement.split("||")]
    if any(result is True for result in results):
        return True
    return None if any(result is None for result in results) else False


def _read_url(url: str, *, limit: int, opener=None, headers: dict[str, str] | None = None, checkpoint=None) -> bytes:
    try:
        request = url if opener else urllib.request.Request(url, headers=headers or {})
        response = (opener or urllib.request.urlopen)(request, timeout=30)
        with response:
            chunks, size = [], 0
            while chunk := response.read(min(1024 * 1024, limit + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if callable(checkpoint):
                    checkpoint()
                if size > limit:
                    break
            data = b"".join(chunks)
    except (OSError, urllib.error.URLError) as exc:
        raise NodeToolchainError("node_toolchain_acquisition_failed", "Node toolchain metadata could not be downloaded", details={"source": url}) from exc
    if len(data) > limit:
        raise NodeToolchainError("node_toolchain_acquisition_failed", "Node toolchain download exceeded its size limit", details={"source": url})
    return data


def _json_url(url: str, *, opener=None, headers: dict[str, str] | None = None, checkpoint=None) -> dict | list:
    try:
        return json.loads(_read_url(url, limit=MAX_METADATA_BYTES, opener=opener, headers=headers, checkpoint=checkpoint))
    except (ValueError, UnicodeError) as exc:
        raise NodeToolchainError("node_toolchain_metadata_invalid", "Node toolchain metadata is invalid", details={"source": url}) from exc


def host_platform() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    mapped = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system != "linux" or mapped is None:
        raise NodeToolchainError("node_toolchain_platform_unsupported", "No Node binary distribution is supported for this host", details={"platform": system, "architecture": machine})
    return system, machine, mapped


def resolve_node(requirement: str, releases: list[dict], *, platform_name: str, node_arch: str) -> dict:
    if not requirement:
        raise NodeToolchainError("node_requirement_missing", "Node projects must declare engines.node", details={"requested_range": requirement})
    if version_satisfies("0.0.0", requirement) is None:
        raise NodeToolchainError("node_range_unsupported", "The Node version range uses unsupported or invalid syntax", details={"requested_range": requirement})
    candidates = []
    distribution = f"{platform_name}-{node_arch}"
    for row in releases:
        exact = str(row.get("version") or "").lstrip("v")
        parsed = _version(exact)
        if parsed is None or "-" in str(row.get("version") or "") or distribution not in (row.get("files") or []):
            continue
        result = version_satisfies(exact, requirement)
        if result:
            candidates.append((parsed, exact, str(row.get("npm") or "").lstrip("v")))
    if not candidates:
        raise NodeToolchainError("node_range_unsatisfied", "No published Node binary satisfies the requested range", details={"requested_range": requirement, "platform": platform_name, "architecture": node_arch})
    _parsed, exact, bundled_npm = max(candidates)
    return {"requested_range": requirement, "version": exact, "platform": platform_name, "architecture": node_arch, "bundled_npm": bundled_npm}


def _resolve_manager(name: str, requirement: str, metadata: dict) -> dict:
    versions = metadata.get("versions") if isinstance(metadata, dict) else None
    if not isinstance(versions, dict) or not requirement:
        raise NodeToolchainError("package_manager_requirement_missing", f"{name} must have a declared version", details={"package_manager": name, "requested_range": requirement})
    if version_satisfies("0.0.0", requirement) is None:
        raise NodeToolchainError("package_manager_range_unsupported", f"The {name} version range is unsupported", details={"package_manager": name, "requested_range": requirement})
    candidates = []
    for exact, document in versions.items():
        parsed = _version(exact)
        if parsed is None or "-" in exact:
            continue
        match = version_satisfies(exact, requirement)
        if match:
            candidates.append((parsed, exact, document))
    if not candidates:
        raise NodeToolchainError("package_manager_range_unsatisfied", f"No published {name} version satisfies the requested range", details={"package_manager": name, "requested_range": requirement})
    _parsed, exact, document = max(candidates, key=lambda row: row[0])
    dist = document.get("dist") if isinstance(document, dict) else None
    if not isinstance(dist, dict) or not dist.get("tarball") or not dist.get("integrity"):
        raise NodeToolchainError("package_manager_metadata_invalid", f"Published {name} metadata has no integrity identity", details={"package_manager": name, "version": exact})
    return {"name": name, "requested_range": requirement, "version": exact, "tarball": dist["tarball"], "integrity": dist["integrity"]}


def resolve(requirements: dict[str, str], *, package_manager: str = "npm", opener=None, releases=None, manager_metadata=None, checkpoint=None) -> dict:
    platform_name, host_arch, node_arch = host_platform()
    node = resolve_node(str(requirements.get("node") or ""), releases if releases is not None else _json_url(NODE_RELEASES_URL, opener=opener, checkpoint=checkpoint), platform_name=platform_name, node_arch=node_arch)
    managers = [name for name in ("npm", "pnpm") if requirements.get(name)]
    if not managers and package_manager == "npm" and SAFE_VERSION.fullmatch(node.get("bundled_npm", "")):
        requirements = {**requirements, "npm": node["bundled_npm"]}
        managers = ["npm"]
    if len(managers) != 1 or managers[0] != package_manager:
        raise NodeToolchainError("package_manager_requirement_missing", "Node projects must declare one supported package-manager version", details={"requirements": requirements})
    name = managers[0]
    metadata = manager_metadata if manager_metadata is not None else _json_url(
        f"{NPM_REGISTRY_URL}/{name}", opener=opener,
        headers={"Accept": "application/vnd.npm.install-v1+json"},
        checkpoint=checkpoint,
    )
    manager = _resolve_manager(name, str(requirements[name]), metadata)
    node["host_architecture"] = host_arch
    return {"node": node, "package_manager": manager}


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _download(url: str, destination: Path, *, limit: int, opener=None, checkpoint=None) -> None:
    destination.write_bytes(_read_url(url, limit=limit, opener=opener, checkpoint=checkpoint))


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for directory, directories, _files in os.walk(path):
        Path(directory).chmod(0o700)
        for name in directories:
            (Path(directory) / name).chmod(0o700)
    shutil.rmtree(path)


def _safe_extract(archive: Path, destination: Path, *, strip_first: bool) -> None:
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        selected = []
        for member in members:
            path = PurePosixPath(member.name)
            parts = path.parts[1:] if strip_first else path.parts
            if path.is_absolute() or ".." in parts or member.isdev():
                raise NodeToolchainError("node_toolchain_archive_unsafe", "A toolchain archive contains an unsafe entry")
            if not parts:
                continue
            if member.issym() or member.islnk():
                continue
            member.name = PurePosixPath(*parts).as_posix()
            selected.append(member)
        source.extractall(destination, members=selected, filter="data")


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        mode = path.stat().st_mode
        path.chmod((mode & 0o555) if path.is_file() else 0o555)
    root.chmod(0o555)


def _node_manifest_valid(root: Path, expected: dict) -> bool:
    manifest = storage.load_json(root / "manifest.json", None)
    binary = root / "bin" / "node"
    return bool(manifest == expected and binary.is_file() and os.access(binary, os.X_OK) and _digest(binary) == manifest.get("node_binary_sha256"))


def _prepare_node(resolution: dict, cache: Path, *, opener=None, checkpoint=None) -> tuple[Path, dict]:
    node = resolution["node"]
    exact, platform_name, arch = node["version"], node["platform"], node["architecture"]
    filename = f"node-v{exact}-{platform_name}-{arch}.tar.xz"
    base = f"{NODE_DIST_URL}/v{exact}"
    try:
        checksums = _read_url(f"{base}/SHASUMS256.txt", limit=MAX_METADATA_BYTES, opener=opener, checkpoint=checkpoint).decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise NodeToolchainError("node_integrity_missing", "Official Node checksums are invalid", details={"version": exact}) from exc
    matches = [line.split()[0] for line in checksums.splitlines() if line.split()[1:] == [filename]]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
        raise NodeToolchainError("node_integrity_missing", "Official Node checksums do not identify the selected archive", details={"version": exact, "archive": filename})
    checksum = matches[0]
    root = cache / "node" / f"{platform_name}-{arch}" / exact
    root.parent.mkdir(parents=True, exist_ok=True)
    expected_base = {"schema_version": 1, "version": exact, "platform": platform_name, "architecture": arch, "archive": filename, "sha256": checksum, "source": f"{base}/{filename}"}
    with _lock(root.parent / f".{exact}.lock"):
        existing = storage.load_json(root / "manifest.json", None)
        if existing and _node_manifest_valid(root, existing) and all(existing.get(key) == value for key, value in expected_base.items()):
            return root, existing
        if root.exists():
            _remove_tree(root)
        staging = Path(tempfile.mkdtemp(prefix=f".{exact}.", dir=root.parent))
        try:
            archive = staging / filename
            _download(f"{base}/{filename}", archive, limit=MAX_ARCHIVE_BYTES, opener=opener, checkpoint=checkpoint)
            if _digest(archive) != checksum:
                raise NodeToolchainError("node_integrity_mismatch", "The Node archive does not match its official SHA-256 checksum", details={"version": exact, "archive": filename, "expected_sha256": checksum})
            extracted = staging / "runtime"
            _safe_extract(archive, extracted, strip_first=True)
            if callable(checkpoint):
                checkpoint()
            binary = extracted / "bin" / "node"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise NodeToolchainError("node_toolchain_invalid", "The prepared Node runtime has no executable node binary", details={"version": exact})
            manifest = {**expected_base, "node_binary_sha256": _digest(binary)}
            storage.save_json(extracted / "manifest.json", manifest)
            _make_read_only(extracted)
            os.replace(extracted, root)
            return root, manifest
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _integrity_digest(value: str) -> tuple[str, bytes]:
    try:
        algorithm, encoded = value.split("-", 1)
        if algorithm not in {"sha256", "sha384", "sha512"}:
            raise ValueError
        return algorithm, base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise NodeToolchainError("package_manager_integrity_invalid", "Package-manager integrity metadata is invalid") from exc


def _manager_manifest_valid(root: Path, expected: dict) -> bool:
    manifest = storage.load_json(root / "manifest.json", None)
    cli = root / manifest.get("cli", "missing") if isinstance(manifest, dict) else root / "missing"
    return bool(manifest == expected and cli.is_file() and _digest(cli) == manifest.get("cli_sha256"))


def _prepare_manager(resolution: dict, cache: Path, *, opener=None, checkpoint=None) -> tuple[Path, dict]:
    manager = resolution["package_manager"]
    name, exact = manager["name"], manager["version"]
    integrity_key = hashlib.sha256(manager["integrity"].encode()).hexdigest()[:16]
    root = cache / "managers" / name / exact / integrity_key
    root.parent.mkdir(parents=True, exist_ok=True)
    expected_base = {"schema_version": 1, "name": name, "version": exact, "integrity": manager["integrity"], "source": manager["tarball"]}
    with _lock(root.parent / f".{integrity_key}.lock"):
        existing = storage.load_json(root / "manifest.json", None)
        if existing and _manager_manifest_valid(root, existing) and all(existing.get(key) == value for key, value in expected_base.items()):
            return root, existing
        if root.exists():
            _remove_tree(root)
        staging = Path(tempfile.mkdtemp(prefix=f".{integrity_key}.", dir=root.parent))
        try:
            archive = staging / "package.tgz"
            _download(manager["tarball"], archive, limit=MAX_ARCHIVE_BYTES, opener=opener, checkpoint=checkpoint)
            algorithm, expected_digest = _integrity_digest(manager["integrity"])
            actual_digest = bytes.fromhex(_digest(archive, algorithm))
            if actual_digest != expected_digest:
                raise NodeToolchainError("package_manager_integrity_mismatch", f"The {name} archive does not match its published integrity", details={"package_manager": name, "version": exact, "integrity": manager["integrity"]})
            extracted = staging / "package"
            _safe_extract(archive, extracted, strip_first=True)
            if callable(checkpoint):
                checkpoint()
            candidates = {"npm": ["bin/npm-cli.js"], "pnpm": ["bin/pnpm.cjs", "bin/pnpm.js"]}[name]
            cli = next((candidate for candidate in candidates if (extracted / candidate).is_file()), "")
            if not cli:
                raise NodeToolchainError("package_manager_archive_invalid", f"The prepared {name} archive has no supported CLI", details={"package_manager": name, "version": exact})
            manifest = {**expected_base, "cli": cli, "cli_sha256": _digest(extracted / cli)}
            storage.save_json(extracted / "manifest.json", manifest)
            _make_read_only(extracted)
            os.replace(extracted, root)
            return root, manifest
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _wrapper(path: Path, node: Path, cli: Path) -> None:
    path.write_text(f"#!/bin/sh\nexec {shlex_quote(str(node))} {shlex_quote(str(cli))} \"$@\"\n")
    path.chmod(0o500)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def prepare(resolution: dict, *, workspace: str | Path, cache: str | Path, opener=None, checkpoint=None) -> dict:
    """Acquire during the networked phase, then expose only Run-local entry points."""
    workspace, cache = Path(workspace), Path(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        node_root, node_manifest = _prepare_node(resolution, cache, opener=opener, checkpoint=checkpoint)
        manager_root, manager_manifest = _prepare_manager(resolution, cache, opener=opener, checkpoint=checkpoint)
    except NodeToolchainError:
        raise
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise NodeToolchainError("node_toolchain_acquisition_failed", "The selected Node toolchain could not be prepared", details={"node_version": resolution.get("node", {}).get("version", "")}) from exc
    run_root = workspace / "toolchain"
    if run_root.exists():
        shutil.rmtree(run_root)
    bin_dir = run_root / "bin"
    bin_dir.mkdir(parents=True, mode=0o700)
    run_home = run_root / "home"
    run_home.mkdir(mode=0o700)
    corepack_home = run_root / "corepack"
    corepack_home.mkdir(mode=0o700)
    node_binary = node_root / "bin" / "node"
    (bin_dir / "node").symlink_to(node_binary)
    name = manager_manifest["name"]
    _wrapper(bin_dir / name, node_binary, manager_root / manager_manifest["cli"])
    identity = {
        "schema_version": 1,
        "node": {key: node_manifest[key] for key in ("version", "platform", "architecture", "archive", "sha256", "source")},
        "requested_node_range": resolution["node"]["requested_range"],
        "package_manager": {"name": name, "version": manager_manifest["version"], "requested_range": resolution["package_manager"]["requested_range"], "integrity": manager_manifest["integrity"], "source": manager_manifest["source"]},
    }
    storage.save_json(run_root / "manifest.json", identity)
    return {"identity": identity, "environment": {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(run_home),
        "COREPACK_HOME": str(corepack_home),
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "npm_config_cache": str(run_root / "npm-cache"),
        "npm_config_update_notifier": "false",
    }}


def validate_prepared(prepared: dict, *, workspace: str | Path) -> dict[str, str]:
    environment = prepared.get("environment") if isinstance(prepared, dict) else None
    bin_dir = Path(workspace) / "toolchain" / "bin"
    identity = prepared.get("identity") if isinstance(prepared, dict) else None
    manager = identity.get("package_manager", {}).get("name") if isinstance(identity, dict) else None
    if not isinstance(environment, dict) or manager not in {"npm", "pnpm"} or not (bin_dir / "node").is_file() or not (bin_dir / manager).is_file():
        raise NodeToolchainError("prepared_node_toolchain_missing", "The prepared Run-local Node toolchain is missing; offline execution is refused")
    return dict(environment)
