"""Serialized reprepro publication with durable, exact repository proof."""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from . import apt_repo, deb_inspector
from .build_models import utc_now
from .build_store import BuildStore
from .repository_lock import (
    RepositoryLease,
    RepositoryLockError,
    repository_lease,
    safe_relative_path,
)


MAX_CONFIG_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REPOSITORY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
UNSUPPORTED_OPTION_NAMES = {
    "basedir", "outdir", "confdir", "dbdir", "distdir", "listdir", "logdir",
    "morguedir", "methoddir", "gnupghome", "outhook", "endhook", "listhook",
}


class PublicationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class SourceArtifactIdentity:
    path: str
    size: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    package: str
    version: str
    architecture: str


@dataclass(frozen=True)
class PublicationProofV1:
    schema: str
    proof_version: int
    verified_at: str
    repository: dict
    distribution: dict
    component: str
    package: str
    version: str
    architecture: str
    database_architecture: str
    database_architectures: list[str]
    index: dict
    pool: dict
    targets: list[dict]
    source: dict


def publication_readiness(run: dict) -> dict:
    artifact = run.get("artifact") or {}
    validations = [
        row for row in run.get("validations", [])
        if row.get("artifact") == artifact.get("path")
    ]
    successful = [row for row in validations if row.get("status") == "success"]
    reasons = []
    if run.get("status") != "success":
        reasons.append("build_not_successful")
    if not artifact.get("path") or artifact.get("pruning") is not None:
        reasons.append("artifact_unavailable")
    if not successful:
        reasons.append("validation_not_successful")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "validation_id": successful[-1]["id"] if successful else "",
    }


def _read_fd(fd: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    os.lseek(fd, 0, os.SEEK_SET)
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise PublicationError("repository_file_too_large", "Repository metadata exceeds the supported size limit")
    return payload


def _read_repository_text(
    lease: RepositoryLease, relative: str, *, required: bool = True, limit: int = MAX_CONFIG_BYTES,
) -> str:
    try:
        with lease.open_regular(relative) as (fd, _):
            return _read_fd(fd, limit=limit).decode("utf-8", errors="strict")
    except FileNotFoundError:
        if not required:
            return ""
        raise PublicationError(
            "repository_configuration_unsupported",
            f"Required repository file {relative} is missing",
        )
    except (UnicodeDecodeError, RepositoryLockError) as exc:
        raise PublicationError(
            "repository_configuration_unsupported",
            f"Repository file {relative} cannot be read safely",
        ) from exc


def _option_name(line: str) -> str:
    value = line.strip()
    if not value or value.startswith("#"):
        return ""
    return value.split(None, 1)[0].split("=", 1)[0].lstrip("-").lower()


def _repository_config(lease: RepositoryLease, requested: str, *, create_layout: bool = True) -> dict:
    distributions = _read_repository_text(lease, "conf/distributions")
    options = _read_repository_text(lease, "conf/options", required=False)
    unsupported = sorted({
        name for name in (_option_name(line) for line in options.splitlines())
        if name in UNSUPPORTED_OPTION_NAMES
    })
    if unsupported:
        raise PublicationError(
            "repository_configuration_unsupported",
            "Repository path overrides and hooks are outside DebBuilder's supported layout",
            details={"options": unsupported},
        )
    try:
        config = apt_repo.select_reprepro_distribution(distributions, requested)
    except ValueError as exc:
        raise PublicationError("distribution_mismatch", str(exc)) from exc
    export_options = {value.lower() for value in config.get("export_options", [])}
    if export_options.intersection({"noexport", "never"}):
        raise PublicationError(
            "repository_configuration_unsupported",
            "The selected distribution must export APT indices",
        )
    codename = str(config.get("codename") or "")
    if not REPOSITORY_TOKEN_RE.fullmatch(codename):
        raise PublicationError("repository_configuration_unsupported", "The selected Codename is unsafe")
    if any(not REPOSITORY_TOKEN_RE.fullmatch(str(value)) for value in config.get("architectures", [])):
        raise PublicationError("repository_configuration_unsupported", "The selected Architectures contain an unsafe value")
    try:
        if create_layout:
            lease.pin_standard_layout()
        else:
            lease.pin_directory("conf", create=False)
            for name in ("db", "dists", "pool", "lists", "logs", "morgue"):
                lease.pin_directory(name, create=False)
    except RepositoryLockError as exc:
        raise PublicationError(exc.code, str(exc), details=exc.details) from exc
    return config


def _validate_requested_identity(component: str, source: SourceArtifactIdentity) -> None:
    try:
        component_path = safe_relative_path(component)
    except RepositoryLockError as exc:
        raise PublicationError("repository_configuration_unsupported", "The selected Component is unsafe") from exc
    if not all(REPOSITORY_TOKEN_RE.fullmatch(part) for part in component_path.parts):
        raise PublicationError("repository_configuration_unsupported", "The selected Component is unsafe")
    if not PACKAGE_RE.fullmatch(source.package) or not REPOSITORY_TOKEN_RE.fullmatch(source.architecture):
        raise PublicationError("artifact_identity_mismatch", "Artifact package identity contains an unsafe value")


@contextmanager
def _source_artifact(run: dict, workspace_fd: int):
    artifact = run.get("artifact") or {}
    if artifact.get("pruning") is not None:
        raise PublicationError("artifact_not_available", "The Run-local artifact has been pruned")
    recorded = artifact.get("inspection") or {}
    workspace = Path(str(run.get("workspace") or "")).absolute()
    path = Path(str(artifact.get("path") or ""))
    if not path.is_absolute():
        path = workspace / path
    path = Path(os.path.abspath(path))
    expected_parent = workspace / "artifacts"
    if path.parent != expected_parent or path.name in {"", ".", ".."}:
        raise PublicationError("artifact_identity_mismatch", "Artifact must be a direct child of the Run artifacts directory")
    artifacts_fd = file_fd = -1
    try:
        artifacts_fd = os.open("artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=artifacts_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PublicationError("artifact_identity_mismatch", "Artifact is not a safe regular file")
        digest = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
        actual_sha = digest.hexdigest()
        recorded_sha = str(artifact.get("sha256") or "").lower()
        recorded_size = artifact.get("size")
        if (
            not SHA256_RE.fullmatch(recorded_sha)
            or int(recorded_size) != info.st_size
            or actual_sha != recorded_sha
        ):
            raise PublicationError("artifact_identity_mismatch", "Artifact size or SHA-256 no longer matches the Build Run")
        inspected = deb_inspector.inspect_deb(
            Path(f"/proc/self/fd/{file_fd}"),
            workspace=Path(f"/proc/self/fd/{workspace_fd}"),
            pass_fds=(workspace_fd, file_fd),
        )
        current = os.stat(path.name, dir_fd=artifacts_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise PublicationError("artifact_identity_mismatch", "Artifact changed while its identity was being verified")
        digest_after = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        while chunk := os.read(file_fd, 1024 * 1024):
            digest_after.update(chunk)
        final_info = os.fstat(file_fd)
        if (
            digest_after.hexdigest() != actual_sha
            or (final_info.st_dev, final_info.st_ino, final_info.st_size, final_info.st_mtime_ns)
            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        ):
            raise PublicationError("artifact_identity_mismatch", "Artifact changed while its identity was being verified")
        identity_fields = ("package", "version", "architecture")
        if not inspected.get("ok") or any(inspected.get(key) != recorded.get(key) for key in identity_fields):
            raise PublicationError("artifact_identity_mismatch", "Artifact package metadata no longer matches the Build Run")
        source = SourceArtifactIdentity(
            path=str(path), size=info.st_size, sha256=actual_sha,
            device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns,
            package=str(recorded.get("package") or ""),
            version=str(recorded.get("version") or ""),
            architecture=str(recorded.get("architecture") or ""),
        )
        if not all((source.package, source.version, source.architecture)):
            raise PublicationError("artifact_identity_mismatch", "Artifact package identity is incomplete")
        yield source, file_fd, artifacts_fd, path.name
    except FileNotFoundError as exc:
        raise PublicationError("artifact_not_available", "The recorded artifact is unavailable") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise PublicationError("artifact_identity_mismatch", "Artifact cannot be opened safely") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if artifacts_fd >= 0:
            os.close(artifacts_fd)


def _index_rows(lease: RepositoryLease, relative: str) -> list[dict] | None:
    try:
        with lease.open_regular(relative) as (fd, info):
            with os.fdopen(os.dup(fd), "rb") as raw:
                if relative.endswith(".gz"):
                    with gzip.GzipFile(fileobj=raw) as zipped:
                        payload = zipped.read(MAX_INDEX_BYTES + 1)
                else:
                    payload = raw.read(MAX_INDEX_BYTES + 1)
            if len(payload) > MAX_INDEX_BYTES:
                raise PublicationError("publication_proof_failed", "Exported Packages index exceeds the supported size limit")
            rows = apt_repo.parse_packages_index(payload.decode("utf-8", errors="strict"))
            return rows, info
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError, RepositoryLockError) as exc:
        raise PublicationError("publication_proof_failed", "Exported Packages index cannot be read safely") from exc


def _exact_index_entry(
    lease: RepositoryLease, *, codename: str, component: str, index_architecture: str,
    expected_architecture: str, package: str, version: str, required: bool,
) -> tuple[dict, str, os.stat_result] | None:
    base = f"dists/{codename}/{component}/binary-{index_architecture}/Packages"
    selected = None
    for relative in (base + ".gz", base):
        result = _index_rows(lease, relative)
        if result is not None:
            selected = (relative, *result)
            break
    if selected is None:
        if required:
            raise PublicationError("publication_proof_failed", "The exported Packages index is missing")
        return None
    relative, rows, info = selected
    matches = [
        row for row in rows
        if row.get("Package") == package and row.get("Version") == version
        and row.get("Architecture") == expected_architecture
    ]
    if not matches:
        if required:
            raise PublicationError("publication_proof_failed", "The exact package identity is absent from the exported Packages index")
        return None
    if len(matches) != 1:
        raise PublicationError("publication_identity_conflict", "The exported Packages index contains duplicate exact identities")
    entry = matches[0]
    try:
        filename = safe_relative_path(entry.get("Filename", ""), required_prefix="pool").as_posix()
        size = int(entry.get("Size", ""))
        sha256 = str(entry.get("SHA256") or "").lower()
    except (ValueError, RepositoryLockError) as exc:
        raise PublicationError("publication_proof_failed", "The exported package identity has invalid integrity fields") from exc
    if size < 0 or not SHA256_RE.fullmatch(sha256):
        raise PublicationError("publication_proof_failed", "The exported package identity has invalid integrity fields")
    return {**entry, "Filename": filename, "Size": str(size), "SHA256": sha256}, relative, info


def _index_package_versions(
    lease: RepositoryLease, *, codename: str, component: str,
    index_architecture: str, expected_architecture: str, package: str,
) -> list[str]:
    base = f"dists/{codename}/{component}/binary-{index_architecture}/Packages"
    for relative in (base + ".gz", base):
        result = _index_rows(lease, relative)
        if result is not None:
            rows, _info = result
            return sorted({
                str(row.get("Version") or "") for row in rows
                if row.get("Package") == package and row.get("Architecture") == expected_architecture
                and row.get("Version")
            })
    return []


def _database_query(
    lease: RepositoryLease, *, codename: str, component: str, package: str, runner,
) -> dict:
    kwargs = {"component": component, "package": package, "lease": lease}
    if runner is not None:
        kwargs["runner"] = runner
    result = apt_repo.reprepro_list(lease.root, codename, **kwargs)
    if result["command"].get("status") != "success":
        raise PublicationError(
            "repository_query_failed",
            result["command"].get("stderr") or "Unable to query reprepro",
        )
    return result


def _database_matches(rows: list[dict], *, codename: str, component: str, architecture: str, package: str, version: str) -> list[dict]:
    return [
        row for row in rows
        if row.get("distribution") == codename and row.get("component") == component
        and row.get("architecture") == architecture and row.get("package") == package
        and row.get("version") == version
    ]


def _target_architectures(config: dict, source: SourceArtifactIdentity) -> list[str]:
    configured = [value for value in config.get("architectures", []) if value not in {"all", "source"}]
    if source.architecture == "all":
        if not configured:
            raise PublicationError("architecture_not_configured", "Architecture all requires a configured binary architecture")
        return configured
    if source.architecture not in configured:
        raise PublicationError("architecture_not_configured", f"Architecture {source.architecture!r} is not configured")
    return [source.architecture]


def _verify_source_fd(source: SourceArtifactIdentity, source_fd: int, artifacts_fd: int, artifact_name: str) -> None:
    digest = hashlib.sha256()
    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        digest.update(chunk)
    current = os.fstat(source_fd)
    try:
        path_info = os.stat(artifact_name, dir_fd=artifacts_fd, follow_symlinks=False)
    except OSError as exc:
        raise PublicationError("artifact_identity_mismatch", "Source artifact path changed before proof persistence") from exc
    expected = (source.device, source.inode, source.size, source.mtime_ns)
    if (
        (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != expected
        or (path_info.st_dev, path_info.st_ino, path_info.st_size, path_info.st_mtime_ns) != expected
        or digest.hexdigest() != source.sha256
    ):
        raise PublicationError("artifact_identity_mismatch", "Source artifact changed before proof persistence")


def _proof(
    lease: RepositoryLease, *, config: dict, component: str,
    source: SourceArtifactIdentity, source_fd: int, artifacts_fd: int, artifact_name: str,
    runner=None, database: dict | None = None,
) -> PublicationProofV1:
    codename = config["codename"]
    target_architectures = _target_architectures(config, source)
    database = database or _database_query(
        lease, codename=codename, component=component, package=source.package, runner=runner,
    )
    targets = []
    for index_architecture in target_architectures:
        matches = _database_matches(
            database["packages"], codename=codename, component=component,
            architecture=index_architecture, package=source.package, version=source.version,
        )
        if len(matches) != 1:
            raise PublicationError(
                "publication_proof_failed" if not matches else "publication_identity_conflict",
                f"The reprepro database does not contain exactly one matching identity for {index_architecture}",
            )
        index = _exact_index_entry(
            lease, codename=codename, component=component,
            index_architecture=index_architecture, expected_architecture=source.architecture,
            package=source.package, version=source.version, required=True,
        )
        assert index is not None
        entry, index_path, index_info = index
        if int(entry["Size"]) != source.size or entry["SHA256"] != source.sha256:
            raise PublicationError("publication_identity_conflict", "The exported package identity does not match the Build artifact")
        try:
            with lease.open_regular(entry["Filename"], required_prefix="pool") as (pool_fd, pool_info):
                digest = hashlib.sha256()
                while chunk := os.read(pool_fd, 1024 * 1024):
                    digest.update(chunk)
                final_pool_info = os.fstat(pool_fd)
        except (FileNotFoundError, RepositoryLockError) as exc:
            raise PublicationError("publication_proof_failed", "The indexed pool artifact cannot be opened safely") from exc
        pool_sha = digest.hexdigest()
        if (
            (final_pool_info.st_dev, final_pool_info.st_ino, final_pool_info.st_size, final_pool_info.st_mtime_ns)
            != (pool_info.st_dev, pool_info.st_ino, pool_info.st_size, pool_info.st_mtime_ns)
            or pool_info.st_size != source.size or pool_sha != source.sha256
        ):
            raise PublicationError("publication_identity_conflict", "The indexed pool artifact does not match the Build artifact")
        targets.append({
            "database_architecture": index_architecture,
            "index": {"path": index_path, "device": index_info.st_dev, "inode": index_info.st_ino, "filename": entry["Filename"], "size": int(entry["Size"]), "sha256": entry["SHA256"]},
            "pool": {"path": entry["Filename"], "size": pool_info.st_size, "sha256": pool_sha, "device": pool_info.st_dev, "inode": pool_info.st_ino},
        })
    _verify_source_fd(source, source_fd, artifacts_fd, artifact_name)
    primary = targets[0]
    return PublicationProofV1(
        schema="debbuilder.repository-publication-proof.v1",
        proof_version=1,
        verified_at=utc_now(),
        repository=lease.identity,
        distribution={"requested": config.get("requested", ""), "codename": codename, "suite": config.get("suite", "")},
        component=component, package=source.package, version=source.version,
        architecture=source.architecture, database_architecture=target_architectures[0],
        database_architectures=target_architectures,
        index=primary["index"], pool=primary["pool"], targets=targets,
        source={"path": source.path, "size": source.size, "sha256": source.sha256, "device": source.device, "inode": source.inode},
    )


def verify_published_artifact_exact(
    lease: RepositoryLease, *, config: dict, component: str,
    source: SourceArtifactIdentity, source_fd: int, artifacts_fd: int,
    artifact_name: str, runner=None,
) -> PublicationProofV1:
    """Prove database, exported index, pool bytes, and source bytes all agree."""
    lease.require_active()
    return _proof(
        lease, config=config, component=component, source=source,
        source_fd=source_fd, artifacts_fd=artifacts_fd, artifact_name=artifact_name,
        runner=runner,
    )


def publication_proof_reference(proof: dict | PublicationProofV1) -> dict:
    """Return the bounded, durable identity used to authorize local pruning."""
    value = asdict(proof) if isinstance(proof, PublicationProofV1) else proof
    if not isinstance(value, dict) or value.get("schema") != "debbuilder.repository-publication-proof.v1" or value.get("proof_version") != 1:
        raise PublicationError("publication_proof_invalid", "Publication proof is not a supported PublicationProofV1 record")

    def text_value(container: dict, key: str, *, limit: int = 4096) -> str:
        result = container.get(key)
        if not isinstance(result, str) or not result or len(result) > limit or "\x00" in result:
            raise PublicationError("publication_proof_invalid", f"Publication proof field {key} is invalid")
        return result

    def integer_value(container: dict, key: str) -> int:
        result = container.get(key)
        if type(result) is not int or result < 0:
            raise PublicationError("publication_proof_invalid", f"Publication proof field {key} is invalid")
        return result

    repository = value.get("repository")
    distribution = value.get("distribution")
    source = value.get("source")
    targets = value.get("targets")
    architectures = value.get("database_architectures")
    if not isinstance(repository, dict) or not isinstance(distribution, dict) or not isinstance(source, dict):
        raise PublicationError("publication_proof_invalid", "Publication proof identity is incomplete")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64 or not isinstance(architectures, list):
        raise PublicationError("publication_proof_invalid", "Publication proof targets are invalid")
    canonical_targets = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("index"), dict) or not isinstance(target.get("pool"), dict):
            raise PublicationError("publication_proof_invalid", "Publication proof target is invalid")
        index = target["index"]
        pool = target["pool"]
        index_path = safe_relative_path(text_value(index, "path")).as_posix()
        filename = safe_relative_path(text_value(index, "filename"), required_prefix="pool").as_posix()
        pool_path = safe_relative_path(text_value(pool, "path"), required_prefix="pool").as_posix()
        if filename != pool_path:
            raise PublicationError("publication_proof_invalid", "Publication proof pool paths disagree")
        index_sha = text_value(index, "sha256", limit=64).lower()
        pool_sha = text_value(pool, "sha256", limit=64).lower()
        if not SHA256_RE.fullmatch(index_sha) or not SHA256_RE.fullmatch(pool_sha):
            raise PublicationError("publication_proof_invalid", "Publication proof digest is invalid")
        canonical_targets.append({
            "database_architecture": text_value(target, "database_architecture", limit=128),
            "index": {"path": index_path, "filename": filename, "size": integer_value(index, "size"), "sha256": index_sha},
            "pool": {"path": pool_path, "size": integer_value(pool, "size"), "sha256": pool_sha},
        })
    source_sha = text_value(source, "sha256", limit=64).lower()
    if not SHA256_RE.fullmatch(source_sha):
        raise PublicationError("publication_proof_invalid", "Publication proof source digest is invalid")
    result = {
        "schema": value["schema"],
        "proof_version": 1,
        "repository": {
            "root": text_value(repository, "root"),
            "device": integer_value(repository, "device"),
            "inode": integer_value(repository, "inode"),
        },
        "distribution": {
            "requested": text_value(distribution, "requested", limit=128),
            "codename": text_value(distribution, "codename", limit=128),
        },
        "component": text_value(value, "component", limit=256),
        "package": text_value(value, "package", limit=256),
        "version": text_value(value, "version", limit=512),
        "architecture": text_value(value, "architecture", limit=128),
        "database_architectures": list(architectures),
        "source": {
            "path": text_value(source, "path"),
            "size": integer_value(source, "size"),
            "sha256": source_sha,
        },
        "targets": canonical_targets,
    }
    if not result["database_architectures"] or len(result["database_architectures"]) > 64 or any(
        not isinstance(item, str) or not item or len(item) > 128
        for item in result["database_architectures"]
    ):
        raise PublicationError("publication_proof_invalid", "Publication proof architectures are invalid")
    return result


@contextmanager
def verified_published_run_artifact(
    run: dict, workspace_fd: int, *, repo_root: str | Path,
    distribution: str, component: str, operation: str, runner=None,
):
    """Hold the repository lease while re-proving a Run's exact artifact."""
    with repository_lease(repo_root, operation=operation) as lease:
        with _source_artifact(run, workspace_fd) as (source, source_fd, artifacts_fd, artifact_name):
            config = _repository_config(lease, distribution, create_layout=False)
            config["requested"] = distribution
            _validate_requested_identity(component, source)
            if component not in config.get("components", []):
                raise PublicationError("component_not_configured", f"Component {component!r} is not configured")
            proof = verify_published_artifact_exact(
                lease, config=config, component=component, source=source,
                source_fd=source_fd, artifacts_fd=artifacts_fd,
                artifact_name=artifact_name, runner=runner,
            )
            yield lease, source, source_fd, artifacts_fd, artifact_name, proof


def verify_source_artifact_fd(
    source: SourceArtifactIdentity, source_fd: int, artifacts_fd: int, artifact_name: str,
) -> None:
    """Recheck the pinned local artifact immediately before a destructive step."""
    _verify_source_fd(source, source_fd, artifacts_fd, artifact_name)


def _attempt(run: dict, *, repo_root: str | Path, distribution: str, component: str, kind: str = "publication") -> tuple[dict, dict, float]:
    artifact = run.get("artifact") or {}
    info = artifact.get("inspection") or {}
    attempt = {
        "id": utc_now().replace(":", "").replace("+", "-").replace(".", "-"),
        "type": "publication_reconciled" if kind == "reconciliation" else "publication",
        "build_run_id": run["id"], "artifact": artifact.get("path", ""),
        "package": info.get("package", ""), "version": info.get("version", ""),
        "architecture": info.get("architecture", ""), "status": "running",
        "requested_at": utc_now(), "finished_at": None, "duration": None,
        "repository": {"root": str(Path(repo_root).absolute()), "distribution": distribution, "component": component},
        "readiness": publication_readiness(run), "preflight": {}, "command": None,
        "proof": None, "published_version": "", "error": None,
    }
    compact = {key: attempt[key] for key in ("id", "status", "requested_at", "finished_at", "published_version")}
    run.setdefault("publications", []).append(attempt)
    artifact.setdefault("publications", []).append(compact)
    return attempt, compact, time.monotonic()


def _finish(store: BuildStore, run: dict, attempt: dict, compact: dict, started: float, *, event: str) -> dict:
    attempt["finished_at"] = utc_now()
    attempt["duration"] = round(time.monotonic() - started, 6)
    compact.update({key: attempt[key] for key in ("status", "finished_at", "published_version")})
    store.append_event(run, f"{event} {attempt['id']}: {attempt['status']}", level="error" if attempt["status"] == "failed" else "info")
    return attempt


def _fail(attempt: dict, exc: Exception, *, fallback: str) -> None:
    if isinstance(exc, PublicationError):
        code, details = exc.code, exc.details
    elif isinstance(exc, RepositoryLockError):
        code, details = exc.code, exc.details
    else:
        code, details = fallback, {}
    attempt.update({"status": "failed", "error": {"code": code, "message": str(exc), "details": details}})


def publish_artifact(
    run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str,
    component: str, confirm: str, runner=None,
) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise PublicationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id) as workspace_fd:
        run = store.load(run_id)
        if not run:
            raise PublicationError("build_run_not_found", "Build Run was not found")
        attempt, compact, started = _attempt(
            run, repo_root=repo_root, distribution=distribution, component=component,
        )
        store.save(run)
        try:
            expected = f"publish:{attempt['package']}:{attempt['version']}"
            if confirm != expected:
                raise PublicationError("publication_confirmation_required", f"Publication requires explicit confirmation: {expected}")
            if not attempt["readiness"]["ready"]:
                raise PublicationError("artifact_not_ready", "Artifact requires a successful Build and Validation", details=attempt["readiness"])
            with repository_lease(repo_root, operation=f"publish:{run_id}") as lease:
                with _source_artifact(run, workspace_fd) as (source, source_fd, artifacts_fd, artifact_name):
                    config = _repository_config(lease, distribution)
                    config["requested"] = distribution
                    _validate_requested_identity(component, source)
                    if component not in config.get("components", []):
                        raise PublicationError("component_not_configured", f"Component {component!r} is not configured")
                    target_architectures = _target_architectures(config, source)
                    attempt["preflight"]["architecture_policy"] = (
                        "all accepted through first configured binary architecture" if source.architecture == "all" else "explicitly configured"
                    )
                    database = _database_query(
                        lease, codename=config["codename"], component=component,
                        package=source.package, runner=runner,
                    )
                    exact_db = {
                        architecture: _database_matches(
                            database["packages"], codename=config["codename"], component=component,
                            architecture=architecture, package=source.package, version=source.version,
                        ) for architecture in target_architectures
                    }
                    exact_index = {
                        architecture: _exact_index_entry(
                            lease, codename=config["codename"], component=component,
                            index_architecture=architecture, expected_architecture=source.architecture,
                            package=source.package, version=source.version, required=False,
                        ) for architecture in target_architectures
                    }
                    attempt["preflight"].update({
                        "database_exact_matches": {key: len(value) for key, value in exact_db.items()},
                        "index_exact_matches": {key: bool(value) for key, value in exact_index.items()},
                    })
                    database_present = any(exact_db.values())
                    database_complete = all(len(value) == 1 for value in exact_db.values())
                    if database_present:
                        if not database_complete:
                            raise PublicationError("publication_proof_failed", "The exact database identity is incomplete across configured architectures")
                        proof = _proof(
                            lease, config=config, component=component, source=source,
                            source_fd=source_fd, artifacts_fd=artifacts_fd, artifact_name=artifact_name,
                            runner=runner, database=database,
                        )
                        attempt["command"] = {"status": "success", "command": "repository verification only", "arguments": [], "working_directory": str(lease.root), "exit_code": 0, "stdout": "Exact artifact already published", "stderr": "", "duration": 0, "timed_out": False}
                    else:
                        if any(exact_index.values()):
                            raise PublicationError("publication_identity_conflict", "The exported index has the identity but the reprepro database does not")
                        published_versions = {
                            str(row.get("version") or "") for row in database["packages"]
                            if row.get("distribution") == config["codename"] and row.get("component") == component
                            and row.get("architecture") in target_architectures and row.get("package") == source.package
                        }
                        for target_architecture in target_architectures:
                            published_versions.update(_index_package_versions(
                                lease, codename=config["codename"], component=component,
                                index_architecture=target_architecture, expected_architecture=source.architecture,
                                package=source.package,
                            ))
                        for published_version in sorted(published_versions):
                            kwargs = {"workspace": lease.root}
                            if runner is not None:
                                kwargs["runner"] = runner
                            comparison = apt_repo.debian_version_relation(source.version, published_version, **kwargs)
                            if comparison["relation"] == "older":
                                raise PublicationError("downgrade_refused", f"Candidate {source.version} is older than published version {published_version}")
                        kwargs = {"lease": lease}
                        if runner is not None:
                            kwargs["runner"] = runner
                        included = apt_repo.reprepro_include_deb(
                            lease.root, config["codename"], Path(source.path), component,
                            source_fd=source_fd, **kwargs,
                        )
                        attempt["command"] = included["command"]
                        if included["command"].get("status") != "success":
                            raise PublicationError("reprepro_include_failed", included["command"].get("stderr") or "reprepro includedeb failed")
                        proof = verify_published_artifact_exact(
                            lease, config=config, component=component, source=source,
                            source_fd=source_fd, artifacts_fd=artifacts_fd, artifact_name=artifact_name,
                            runner=runner,
                        )
                    attempt["proof"] = asdict(proof)
                    attempt.update({"status": "success", "published_version": source.version})
                    return _finish(store, run, attempt, compact, started, event="Artifact publication")
        except Exception as exc:
            _fail(attempt, exc, fallback="publication_execution_failed")
            return _finish(store, run, attempt, compact, started, event="Artifact publication")


def reconcile_publication(
    run_id: str, *, store: BuildStore, repo_root: str | Path, distribution: str,
    component: str, runner=None,
) -> dict:
    if not store.run_dir(run_id).is_dir():
        raise PublicationError("build_run_not_found", "Build Run was not found")
    with store.locked_run(run_id) as workspace_fd:
        run = store.load(run_id)
        if not run:
            raise PublicationError("build_run_not_found", "Build Run was not found")
        attempt, compact, started = _attempt(
            run, repo_root=repo_root, distribution=distribution, component=component,
            kind="reconciliation",
        )
        store.save(run)
        try:
            if run.get("status") != "success":
                raise PublicationError("artifact_not_available", "A successful Build Run with an artifact is required")
            with repository_lease(repo_root, operation=f"reconcile:{run_id}") as lease:
                with _source_artifact(run, workspace_fd) as (source, source_fd, artifacts_fd, artifact_name):
                    config = _repository_config(lease, distribution)
                    config["requested"] = distribution
                    _validate_requested_identity(component, source)
                    if component not in config.get("components", []):
                        raise PublicationError("component_not_configured", f"Component {component!r} is not configured")
                    proof = verify_published_artifact_exact(
                        lease, config=config, component=component, source=source,
                        source_fd=source_fd, artifacts_fd=artifacts_fd, artifact_name=artifact_name,
                        runner=runner,
                    )
                    attempt["proof"] = asdict(proof)
                    attempt["command"] = {"status": "success", "command": "repository reconciliation only", "arguments": [], "working_directory": str(lease.root), "exit_code": 0, "stdout": "Database, exported index, and pool artifact agree", "stderr": "", "duration": 0, "timed_out": False}
                    attempt.update({"status": "success", "published_version": source.version})
                    return _finish(store, run, attempt, compact, started, event="Publication reconciliation")
        except Exception as exc:
            _fail(attempt, exc, fallback="publication_reconciliation_failed")
            return _finish(store, run, attempt, compact, started, event="Publication reconciliation")
