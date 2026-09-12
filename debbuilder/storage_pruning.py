"""Recovery-safe pruning of repository-proven Run-local storage."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Callable

from . import artifact_publication
from .build_models import utc_now, validate_run
from .command_containment import containment_safety_gate
from .repository_lock import RepositoryMutationBusy
from .workspace_cleanup import (
    DISPOSABLE_DIRECTORIES,
    DISPOSABLE_FILES,
    OPEN_CLEANUP_AUTHORIZATION,
    CleanupAuthorization,
    WorkspaceBusyError,
    directory_fd,
    read_json,
    read_run,
    require_current_run_directory,
    require_current_workspace_directory,
    require_destructive_run_safe,
    require_safe_workspace_targets,
    validate_policy,
    write_json,
)


PRUNING_SCHEMA = "debbuilder.artifact-pruning.v1"
PRUNING_VERSION = 1
PRUNING_INTENT = ".artifact-pruning-intent.json"
STAGING_MANIFEST = "manifests/staging-files.json"
STAGING_PRUNING_INTENT = ".staging-manifest-pruning-intent.json"
STAGING_PRUNING_SCHEMA = "debbuilder.staging-manifest-pruning.v1"


class PruningStopped(RuntimeError):
    pass


class PruningIneligible(RuntimeError):
    pass


def _artifact_name(run: dict) -> str:
    artifact = run.get("artifact") or {}
    workspace = Path(str(run.get("workspace") or "")).absolute()
    path = Path(str(artifact.get("path") or ""))
    if not path.is_absolute():
        path = workspace / path
    path = Path(os.path.abspath(path))
    if path.parent != workspace / "artifacts" or path.name in {"", ".", ".."} or path.suffix != ".deb":
        raise ValueError("Artifact is not a direct .deb child of the Run artifacts directory")
    return path.name


def _latest_proof(run: dict, *, repo_root: Path, distribution: str, component: str) -> tuple[str, dict]:
    artifact = run.get("artifact") or {}
    if run.get("mode") != "build" or run.get("status") != "success" or not artifact.get("path"):
        raise PruningIneligible("Run does not contain a successful Build artifact")
    for attempt in reversed(run.get("publications") or []):
        if not isinstance(attempt, dict) or attempt.get("status") != "success" or not attempt.get("proof"):
            continue
        repository = attempt.get("repository") or {}
        if (
            str(Path(str(repository.get("root") or "")).absolute()) != str(repo_root.absolute())
            or repository.get("distribution") != distribution
            or repository.get("component") != component
            or attempt.get("artifact") != artifact.get("path")
        ):
            continue
        reference = artifact_publication.publication_proof_reference(attempt["proof"])
        source = reference["source"]
        if (
            source["path"] != artifact.get("path")
            or source["size"] != artifact.get("size")
            or source["sha256"] != str(artifact.get("sha256") or "").lower()
        ):
            raise ValueError("Publication proof does not match the current Run artifact metadata")
        attempt_id = attempt.get("id")
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 256:
            raise ValueError("Successful publication attempt identity is invalid")
        return attempt_id, reference
    raise PruningIneligible("Run has no applicable successful PublicationProofV1")


def _valid_intent(
    value,
    *,
    run_id: str,
    artifact_name: str,
    attempt_id: str,
    proof: dict,
    artifacts_directory: dict,
) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") != PRUNING_SCHEMA or value.get("pruning_version") != PRUNING_VERSION or value.get("status") != "prepared":
        raise ValueError("Artifact pruning intent is malformed")
    source = value.get("source")
    if (
        value.get("run_id") != run_id
        or not isinstance(value.get("prepared_at"), str)
        or not value["prepared_at"]
        or len(value["prepared_at"]) > 128
        or value.get("publication_attempt_id") != attempt_id
        or value.get("publication_proof") != proof
        or value.get("artifacts_directory") != artifacts_directory
        or not isinstance(source, dict)
        or source.get("name") != artifact_name
        or source.get("path") != proof["source"]["path"]
        or source.get("size") != proof["source"]["size"]
        or source.get("sha256") != proof["source"]["sha256"]
    ):
        raise ValueError("Artifact pruning intent does not match the current publication proof")
    return value


def _valid_pruning(value, *, artifact_name: str, attempt_id: str, proof: dict) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") != PRUNING_SCHEMA or value.get("pruning_version") != PRUNING_VERSION or value.get("status") != "pruned":
        raise ValueError("Artifact pruning metadata is malformed")
    source = value.get("source")
    publication = value.get("publication")
    if (
        not isinstance(source, dict)
        or not isinstance(publication, dict)
        or value.get("reason") != "duplicate_after_exact_publication"
        or not isinstance(value.get("pruned_at"), str)
        or not value["pruned_at"]
        or len(value["pruned_at"]) > 128
        or source.get("name") != artifact_name
        or source.get("path") != proof["source"]["path"]
        or source.get("size") != proof["source"]["size"]
        or source.get("sha256") != proof["source"]["sha256"]
        or publication.get("attempt_id") != attempt_id
        or publication.get("proof") != proof
    ):
        raise ValueError("Artifact pruning metadata does not match the current publication proof")
    return value


def intentional_pruned_artifact_size(run: dict) -> int | None:
    """Return verified reclaimed bytes using Run metadata only, or None."""
    try:
        artifact = run.get("artifact") or {}
        pruning = artifact.get("pruning")
        publication = pruning.get("publication") if isinstance(pruning, dict) else None
        attempt_id = publication.get("attempt_id") if isinstance(publication, dict) else None
        proof = publication.get("proof") if isinstance(publication, dict) else None
        canonical_proof = artifact_publication.publication_proof_reference(proof)
        if (
            run.get("mode") != "build"
            or run.get("status") != "success"
            or canonical_proof != proof
            or not isinstance(attempt_id, str)
            or not attempt_id
            or len(attempt_id) > 256
        ):
            return None
        artifact_name = _artifact_name(run)
        _valid_pruning(pruning, artifact_name=artifact_name, attempt_id=attempt_id, proof=proof)
        source = proof["source"]
        if (
            artifact.get("path") != source["path"]
            or artifact.get("size") != source["size"]
            or str(artifact.get("sha256") or "").lower() != source["sha256"]
            or Path(source["path"]).name != artifact_name
        ):
            return None
        for attempt in run.get("publications") or []:
            if (
                isinstance(attempt, dict)
                and attempt.get("id") == attempt_id
                and attempt.get("status") == "success"
                and attempt.get("artifact") == artifact.get("path")
                and artifact_publication.publication_proof_reference(attempt.get("proof")) == proof
            ):
                return source["size"]
    except (KeyError, TypeError, ValueError, artifact_publication.PublicationError):
        return None
    return None


def _file_exists(fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Unsafe artifact path: {name}")
    return True


def _hash_fd(fd: int, should_stop: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        if should_stop():
            raise PruningStopped()
        digest.update(chunk)
    return digest.hexdigest()


def _staging_manifest_identity(workspace_fd: int, run: dict, should_stop: Callable[[], bool]) -> dict | None:
    staging = next((step for step in run.get("steps") or [] if step.get("name") == "staging"), None)
    details = (staging or {}).get("details") or {}
    if (staging or {}).get("status") != "success" or details.get("content_manifest") != STAGING_MANIFEST:
        return None
    count = details.get("content_file_count")
    if type(count) is not int or count < 0:
        raise ValueError("Staging manifest compact file count is invalid")
    require_safe_workspace_targets(workspace_fd, ("manifests",))
    manifests_fd = file_fd = -1
    try:
        manifests_fd = os.open("manifests", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
        directory = os.fstat(manifests_fd)
        file_fd = os.open("staging-files.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=manifests_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != os.fstat(workspace_fd).st_dev:
            raise ValueError("Staging manifest is not a safe regular file")
        return {
            "path": STAGING_MANIFEST,
            "size": info.st_size,
            "sha256": _hash_fd(file_fd, should_stop),
            "device": info.st_dev,
            "inode": info.st_ino,
            "directory": {"device": directory.st_dev, "inode": directory.st_ino},
            "content_file_count": count,
        }
    except FileNotFoundError as exc:
        raise ValueError("Referenced staging manifest is unexpectedly missing") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if manifests_fd >= 0:
            os.close(manifests_fd)


def _delete_staging_manifest(
    workspace_fd: int,
    identity: dict | None,
    should_stop: Callable[[], bool],
    *,
    builds_root: Path,
    run_id: str,
) -> bool:
    if identity is None:
        return False
    if should_stop():
        raise PruningStopped()
    manifests_fd = file_fd = -1
    try:
        manifests_fd = os.open("manifests", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
        directory = os.fstat(manifests_fd)
        if identity["directory"] != {"device": directory.st_dev, "inode": directory.st_ino}:
            raise ValueError("Staging manifest parent directory changed before pruning")
        require_current_workspace_directory(workspace_fd, "manifests", manifests_fd)
        try:
            file_fd = os.open("staging-files.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=manifests_fd)
        except FileNotFoundError:
            # A durable intent can survive a crash after the exact unlink.
            return True
        info = os.fstat(file_fd)
        expected = (identity["device"], identity["inode"], identity["size"])
        current = os.stat("staging-files.json", dir_fd=manifests_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino, info.st_size) != expected
            or (current.st_dev, current.st_ino, current.st_size) != expected
            or _hash_fd(file_fd, should_stop) != identity["sha256"]
        ):
            raise ValueError("Staging manifest changed before pruning")
        require_current_workspace_directory(workspace_fd, "manifests", manifests_fd)
        if should_stop():
            raise PruningStopped()
        require_current_run_directory(builds_root, run_id, workspace_fd)
        os.unlink("staging-files.json", dir_fd=manifests_fd)
        os.fsync(manifests_fd)
        require_current_workspace_directory(workspace_fd, "manifests", manifests_fd)
        os.fsync(workspace_fd)
        return True
    except FileNotFoundError as exc:
        if manifests_fd < 0:
            raise ValueError("Staging manifest parent directory is unexpectedly missing") from exc
        # A durable intent can survive a crash after this unlink but before
        # run.json is updated. Absence now completes that planned deletion.
        return True
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if manifests_fd >= 0:
            os.close(manifests_fd)


def _retained_disposable_data(workspace_fd: int) -> bool:
    for name in DISPOSABLE_DIRECTORIES:
        child_fd = -1
        try:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
            if os.listdir(child_fd):
                return True
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(f"Disposable workspace target {name} cannot be inspected safely") from exc
        finally:
            if child_fd >= 0:
                os.close(child_fd)
    for name in DISPOSABLE_FILES:
        try:
            os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            pass
    return False


def _valid_staging_record(details: dict) -> dict | None:
    value = details.get("content_manifest_pruning")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != STAGING_PRUNING_SCHEMA
        or value.get("status") != "pruned"
        or value.get("reason") != "terminal_manifest_no_longer_required"
        or value.get("path") != STAGING_MANIFEST
        or type(value.get("content_file_count")) is not int
        or value["content_file_count"] < 0
        or not isinstance(value.get("pruned_at"), str)
        or not value["pruned_at"]
    ):
        raise ValueError("Staging manifest pruning metadata is malformed")
    return value


def _valid_staging_intent(value, *, run_id: str, identity: dict, count: int) -> dict | None:
    if value is None:
        return None
    digest = identity.get("sha256") if isinstance(identity, dict) else None
    directory = identity.get("directory") if isinstance(identity, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != STAGING_PRUNING_SCHEMA
        or value.get("status") != "prepared"
        or value.get("run_id") != run_id
        or value.get("path") != STAGING_MANIFEST
        or value.get("content_file_count") != count
        or value.get("identity") != identity
        or not isinstance(value.get("prepared_at"), str)
        or not value["prepared_at"]
        or identity.get("path") != STAGING_MANIFEST
        or type(identity.get("size")) is not int
        or identity["size"] < 0
        or type(identity.get("device")) is not int
        or identity["device"] < 0
        or type(identity.get("inode")) is not int
        or identity["inode"] < 0
        or not isinstance(directory, dict)
        or type(directory.get("device")) is not int
        or directory["device"] < 0
        or type(directory.get("inode")) is not int
        or directory["inode"] < 0
        or identity.get("content_file_count") != count
        or not isinstance(digest, str)
        or not artifact_publication.SHA256_RE.fullmatch(digest)
    ):
        raise ValueError("Staging manifest pruning intent is malformed")
    return value


def _finish_staging_metadata(workspace_fd: int, run: dict, *, count: int) -> None:
    now = utc_now()
    staging = next(step for step in run["steps"] if step.get("name") == "staging")
    details = staging.setdefault("details", {})
    details.pop("content_manifest", None)
    details["content_manifest_pruning"] = {
        "schema": STAGING_PRUNING_SCHEMA,
        "status": "pruned",
        "reason": "terminal_manifest_no_longer_required",
        "path": STAGING_MANIFEST,
        "content_file_count": count,
        "pruned_at": now,
    }
    validate_run(run)
    write_json(workspace_fd, "run.json", run)
    try:
        os.unlink(STAGING_PRUNING_INTENT, dir_fd=workspace_fd)
        os.fsync(workspace_fd)
    except FileNotFoundError:
        pass


def _prune_staging_manifest_locked(
    workspace_fd: int,
    run: dict,
    *,
    authorization: CleanupAuthorization,
    should_stop: Callable[[], bool],
    allow_new: bool,
    builds_root: Path,
) -> str:
    require_destructive_run_safe(workspace_fd, run, authorization=authorization)
    staging = next(step for step in run.get("steps") or [] if step.get("name") == "staging")
    details = (staging or {}).get("details") or {}
    if not isinstance(details, dict):
        raise ValueError("Staging details are malformed")
    record = _valid_staging_record(details)
    reference = details.get("content_manifest")
    manifests_fd = os.open("manifests", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
    try:
        exists = _file_exists(manifests_fd, "staging-files.json")
    finally:
        os.close(manifests_fd)
    marker = read_json(workspace_fd, STAGING_PRUNING_INTENT)
    if record is not None:
        if reference is not None or exists:
            raise ValueError("A pruned staging manifest unexpectedly reappeared")
        if marker is not None:
            os.unlink(STAGING_PRUNING_INTENT, dir_fd=workspace_fd)
            os.fsync(workspace_fd)
        return "already_pruned"
    if reference != STAGING_MANIFEST:
        if marker is not None:
            raise ValueError("Staging manifest pruning intent has no matching Run reference")
        raise PruningIneligible("Run has no externalized staging manifest")
    count = details.get("content_file_count")
    if type(count) is not int or count < 0:
        raise ValueError("Staging manifest compact file count is invalid")
    if marker is not None:
        if not isinstance(marker, dict) or not isinstance(marker.get("identity"), dict):
            raise ValueError("Staging manifest pruning intent is malformed")
        identity = marker["identity"]
        _valid_staging_intent(marker, run_id=run["id"], identity=identity, count=count)
        if not _delete_staging_manifest(
            workspace_fd, identity, should_stop, builds_root=builds_root, run_id=run["id"],
        ):
            raise ValueError("Staging manifest deletion did not complete")
        _finish_staging_metadata(workspace_fd, run, count=count)
        return "recovered"
    if not exists:
        raise ValueError("Staging manifest is missing without durable pruning intent")
    if not allow_new or _retained_disposable_data(workspace_fd):
        raise PruningIneligible("Staging manifest remains part of retained workspace evidence")
    if should_stop():
        raise PruningStopped()
    identity = _staging_manifest_identity(workspace_fd, run, should_stop)
    assert identity is not None
    intent = {
        "schema": STAGING_PRUNING_SCHEMA,
        "status": "prepared",
        "prepared_at": utc_now(),
        "run_id": run["id"],
        "path": STAGING_MANIFEST,
        "content_file_count": count,
        "identity": identity,
    }
    write_json(workspace_fd, STAGING_PRUNING_INTENT, intent)
    if should_stop():
        raise PruningStopped()
    if not _delete_staging_manifest(
        workspace_fd, identity, should_stop, builds_root=builds_root, run_id=run["id"],
    ):
        raise ValueError("Staging manifest deletion did not complete")
    _finish_staging_metadata(workspace_fd, run, count=count)
    return "pruned"


def _finish_metadata(workspace_fd: int, run: dict, intent: dict) -> dict:
    now = utc_now()
    artifact = run["artifact"]
    record = {
        "schema": PRUNING_SCHEMA,
        "pruning_version": PRUNING_VERSION,
        "status": "pruned",
        "reason": "duplicate_after_exact_publication",
        "pruned_at": now,
        "source": intent["source"],
        "publication": {
            "attempt_id": intent["publication_attempt_id"],
            "proof": intent["publication_proof"],
        },
    }
    artifact["pruning"] = record
    run.setdefault("events", []).append({
        "at": now,
        "level": "info",
        "message": "Run-local artifact pruned after exact repository verification",
    })
    validate_run(run)
    write_json(workspace_fd, "run.json", run)
    try:
        os.unlink(PRUNING_INTENT, dir_fd=workspace_fd)
        os.fsync(workspace_fd)
    except FileNotFoundError:
        pass
    return record


def _prune_locked(
    workspace_fd: int,
    run: dict,
    *,
    repo_root: Path,
    distribution: str,
    component: str,
    authorization: CleanupAuthorization,
    should_stop: Callable[[], bool],
    allow_new: bool,
    builds_root: Path,
    runner=None,
) -> dict:
    require_destructive_run_safe(workspace_fd, run, authorization=authorization)
    require_safe_workspace_targets(workspace_fd, ("artifacts",))
    artifact_name = _artifact_name(run)
    attempt_id, persisted_proof = _latest_proof(
        run, repo_root=repo_root, distribution=distribution, component=component,
    )
    try:
        artifacts_fd = os.open("artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=workspace_fd)
    except FileNotFoundError as exc:
        raise ValueError("Run artifacts directory is unexpectedly missing") from exc
    try:
        directory = os.fstat(artifacts_fd)
        artifacts_directory = {"device": directory.st_dev, "inode": directory.st_ino}
        artifact_exists = _file_exists(artifacts_fd, artifact_name)
    finally:
        os.close(artifacts_fd)
    pruning = _valid_pruning(
        (run.get("artifact") or {}).get("pruning"), artifact_name=artifact_name,
        attempt_id=attempt_id, proof=persisted_proof,
    )
    intent = _valid_intent(
        read_json(workspace_fd, PRUNING_INTENT), run_id=run["id"], artifact_name=artifact_name,
        attempt_id=attempt_id, proof=persisted_proof, artifacts_directory=artifacts_directory,
    )
    if pruning is not None:
        if artifact_exists:
            raise ValueError("A pruned Run-local artifact unexpectedly reappeared")
        if intent is not None:
            os.unlink(PRUNING_INTENT, dir_fd=workspace_fd)
            os.fsync(workspace_fd)
        return {"id": run["id"], "status": "already_pruned", "manifest_pruned": False}
    if not artifact_exists:
        if intent is None:
            raise ValueError("Run-local artifact is missing without durable pruning intent")
        _finish_metadata(workspace_fd, run, intent)
        return {"id": run["id"], "status": "recovered", "manifest_pruned": False}
    if should_stop():
        raise PruningStopped()
    if intent is None and not allow_new:
        raise PruningIneligible("Automatic pruning is disabled")
    with artifact_publication.verified_published_run_artifact(
        run, workspace_fd, repo_root=repo_root, distribution=distribution,
        component=component, operation=f"prune:{run['id']}", runner=runner,
    ) as (_lease, source, source_fd, artifacts_fd, verified_name, fresh_proof):
        fresh_reference = artifact_publication.publication_proof_reference(fresh_proof)
        if fresh_reference != persisted_proof:
            raise ValueError("Fresh repository proof does not match the successful publication proof")
        if should_stop():
            raise PruningStopped()
        if intent is None:
            if should_stop():
                raise PruningStopped()
            require_current_workspace_directory(workspace_fd, "artifacts", artifacts_fd)
            directory = os.fstat(artifacts_fd)
            current_artifacts_directory = {"device": directory.st_dev, "inode": directory.st_ino}
            intent = {
                "schema": PRUNING_SCHEMA,
                "pruning_version": PRUNING_VERSION,
                "status": "prepared",
                "prepared_at": utc_now(),
                "run_id": run["id"],
                "publication_attempt_id": attempt_id,
                "publication_proof": persisted_proof,
                "artifacts_directory": current_artifacts_directory,
                "source": {
                    "path": source.path,
                    "name": verified_name,
                    "size": source.size,
                    "sha256": source.sha256,
                },
            }
            write_json(workspace_fd, PRUNING_INTENT, intent)
        if should_stop():
            raise PruningStopped()
        artifact_publication.verify_source_artifact_fd(source, source_fd, artifacts_fd, verified_name)
        if should_stop():
            raise PruningStopped()
        require_current_workspace_directory(workspace_fd, "artifacts", artifacts_fd)
        if should_stop():
            raise PruningStopped()
        require_current_run_directory(builds_root, run["id"], workspace_fd)
        os.unlink(verified_name, dir_fd=artifacts_fd)
        os.fsync(artifacts_fd)
        require_current_workspace_directory(workspace_fd, "artifacts", artifacts_fd)
        os.fsync(workspace_fd)
        _finish_metadata(workspace_fd, run, intent)
    return {"id": run["id"], "status": "pruned", "manifest_pruned": False}


def apply_pruning(
    store,
    *,
    repo_root: str | Path,
    distribution: str,
    component: str,
    policy: dict | None = None,
    authorization: CleanupAuthorization = OPEN_CLEANUP_AUTHORIZATION,
    should_stop: Callable[[], bool] | None = None,
    runner=None,
) -> dict:
    """Prune eligible Run-local duplicates while isolating malformed Runs."""
    result = {"pruned": [], "recovered": [], "already_pruned": [], "skipped": [], "manifests_pruned": [], "errors": []}
    configured = validate_policy({} if policy is None else policy)
    stop_requested = should_stop or (lambda: False)
    if stop_requested():
        return result
    try:
        authorization.require_global()
    except WorkspaceBusyError as exc:
        result["blocked"] = {"scope": "global", "reason": str(exc)}
        return result
    try:
        with directory_fd(store.root) as root_fd:
            run_ids = sorted(entry.name for entry in os.scandir(root_fd) if entry.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return result
    for run_id in run_ids:
        if stop_requested():
            break
        try:
            with containment_safety_gate():
                with store.locked_run(run_id, blocking=False) as workspace_fd:
                    run = read_run(workspace_fd, store.root, run_id)
                    try:
                        manifest_status = _prune_staging_manifest_locked(
                            workspace_fd, run, authorization=authorization,
                            should_stop=stop_requested, allow_new=configured["enabled"],
                            builds_root=store.root,
                        )
                        if manifest_status in {"pruned", "recovered"}:
                            result["manifests_pruned"].append(run_id)
                        run = read_run(workspace_fd, store.root, run_id)
                    except PruningIneligible:
                        pass
                    except PruningStopped:
                        raise
                    except WorkspaceBusyError:
                        raise
                    except (OSError, ValueError) as exc:
                        result["errors"].append({"id": run_id, "error": str(exc), "scope": "staging_manifest"})
                    row = _prune_locked(
                        workspace_fd, run, repo_root=Path(repo_root).absolute(),
                        distribution=distribution, component=component,
                        authorization=authorization, should_stop=stop_requested,
                        allow_new=configured["enabled"],
                        builds_root=store.root,
                        runner=runner,
                    )
            result[row["status"]].append(run_id)
        except PruningStopped:
            break
        except (PruningIneligible, WorkspaceBusyError, RepositoryMutationBusy, FileNotFoundError):
            result["skipped"].append(run_id)
        except Exception as exc:
            result["errors"].append({"id": run_id, "error": str(exc)})
    return result
