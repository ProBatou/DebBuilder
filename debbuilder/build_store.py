"""Atomic persistence and workspace ownership for Build Runs."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import storage
from .build_models import new_run, utc_now, validate_run
from .recipe_schema import recipe_for_storage, require_safe_name

WORKSPACE_DIRECTORIES = ("source", "staging", "artifacts", "logs", "manifests")
EXECUTION_HISTORY_DELETION_FILE = ".execution-history-deleted.json"


class RunStatusTransitionError(RuntimeError):
    """The persisted Run did not have the state required by a transition."""

    def __init__(self, run_id: str, expected: str, actual: str):
        super().__init__(f"Build Run {run_id} has status {actual}; expected {expected}")
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


def make_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}-{secrets.token_hex(2)}"


class BuildStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        require_safe_name(run_id, "build run id")
        return self.root / run_id

    @staticmethod
    def allocate_run_id() -> str:
        """Allocate durable Run identity before its first filesystem mutation."""
        return make_run_id()

    def discard_incomplete_creation(self, run_id: str) -> bool:
        """Remove only a newly-owned workspace that never committed run.json."""
        folder = self.run_dir(run_id)
        if not folder.exists():
            return True
        if folder.is_symlink() or not folder.is_dir() or (folder / "run.json").exists():
            return False
        shutil.rmtree(folder)
        return not folder.exists()

    def execution_history_deletion_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / EXECUTION_HISTORY_DELETION_FILE

    def execution_history_deleted(self, run_id: str, run: dict | None = None) -> bool:
        """Return persistent Logs-history visibility independently from Run rewrites."""
        if self.execution_history_deletion_path(run_id).is_file():
            return True
        if run and run.get("log_deleted"):
            return True
        return False

    def _record_execution_history_deletion(self, run_id: str) -> dict:
        path = self.execution_history_deletion_path(run_id)
        marker = storage.load_json(path, None)
        if isinstance(marker, dict) and marker.get("deleted_at"):
            return marker
        marker = {"run_id": run_id, "deleted_at": utc_now()}
        storage.save_json(path, marker)
        path.chmod(0o600)
        return marker

    @contextmanager
    def locked_run(self, run_id: str, *, blocking: bool = True):
        """Lease the workspace across processes and serialize Run mutations."""
        from .repository_lock import require_no_repository_lease
        from .workspace_cleanup import locked_workspace
        require_no_repository_lease()
        path = self.run_dir(run_id) / "run.json"
        with locked_workspace(self.root, run_id, blocking=blocking) as fd:
            with storage.locked_path(path):
                yield fd

    def create(self, recipe: dict, *, recipe_id: str = "", mode: str = "dry_run", run_id: str | None = None, resource_contract: dict | None = None) -> dict:
        canonical = recipe_for_storage(recipe)
        identifier = run_id or make_run_id()
        folder = self.run_dir(identifier)
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name in WORKSPACE_DIRECTORIES:
            (folder / name).mkdir(mode=0o700)
        (folder / "logs" / "commands").mkdir(mode=0o700)
        snapshot = json.dumps(canonical, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        snapshot_path = folder / "recipe.json"
        snapshot_path.write_text(snapshot)
        snapshot_path.chmod(0o400)
        digest = hashlib.sha256(snapshot.encode()).hexdigest()
        run = new_run(
            identifier, recipe_id or canonical["name"], mode, str(folder.resolve()), digest,
            resource_contract=resource_contract,
        )
        self.save(run)
        (folder / "logs" / "pipeline.log").touch(mode=0o600)
        return run

    def save(self, run: dict) -> None:
        normalized = validate_run(run)
        path = self.run_dir(str(normalized["id"])) / "run.json"
        storage.save_json(path, normalized)
        path.chmod(0o600)

    def load(self, run_id: str) -> dict | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            return None
        run = storage.load_json(path, None)
        if not isinstance(run, dict):
            return None
        return validate_run(run)

    def transition_status(self, run_id: str, *, expected: str, status: str) -> dict:
        """Atomically persist one compare-and-set Run status transition."""
        with self.locked_run(run_id):
            run = self.load(run_id)
            if not run:
                raise FileNotFoundError(f"Build Run {run_id} was not found")
            actual = str(run.get("status") or "")
            if actual != expected:
                raise RunStatusTransitionError(run_id, expected, actual)
            run["status"] = status
            self.save(run)
            return run

    def list(self, limit: int = 50) -> list[dict]:
        if not self.root.exists():
            return []
        rows = []
        for path in self.root.glob("*/run.json"):
            run = storage.load_json(path, None)
            if isinstance(run, dict):
                try:
                    run = validate_run(run)
                except ValueError:
                    continue
                rows.append(run)
        return sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)[:limit]

    def append_event(self, run: dict, message: str, *, level: str = "info") -> None:
        event = {"at": utc_now(), "level": level, "message": str(message)}
        run.setdefault("events", []).append(event)
        log = self.run_dir(str(run["id"])) / "logs" / "pipeline.log"
        with log.open("a") as handle:
            handle.write(f"{event['at']} {level.upper()} {event['message']}\n")
        self.save(run)

    def append_log_line(self, run_id: str, message: str, *, level: str = "info") -> None:
        log = self.run_dir(run_id) / "logs" / "pipeline.log"
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log.open("a") as handle:
            handle.write(f"{utc_now()} {level.upper()} {message.rstrip()}\n")

    def log_text(self, run_id: str) -> str:
        path = self.run_dir(run_id) / "logs" / "pipeline.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def log_slice(self, run_id: str, offset: int = 0) -> dict:
        path = self.run_dir(run_id) / "logs" / "pipeline.log"
        if not path.exists():
            return {"text": "", "offset": 0, "size": 0}
        size = path.stat().st_size
        start = max(0, min(int(offset or 0), size))
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
        return {"text": data.decode("utf-8", errors="replace"), "offset": size, "size": size}

    def save_command_result(self, run_id: str, result: dict) -> Path:
        index = int(result.get("index") or 0)
        if index < 1:
            raise ValueError("command result requires a positive index")
        path = self.run_dir(run_id) / "logs" / "commands" / f"{index:03d}.json"
        storage.save_json(path, result)
        path.chmod(0o600)
        return path

    def clear_log_history(self, run_id: str, *, authorization=None) -> dict:
        from .workspace_cleanup import delete_history
        if authorization is None:
            return delete_history(self, run_id)
        return delete_history(self, run_id, authorization=authorization)

    def _manifest_path(self, run_id: str, relative_path: str) -> Path:
        """Resolve a manifest reference without allowing it outside its Run."""
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or relative.parts[0] != "manifests":
            raise ValueError("manifest path must be relative to the Run manifests directory")
        target = (self.run_dir(run_id) / relative).resolve(strict=False)
        manifests = (self.run_dir(run_id) / "manifests").resolve(strict=False)
        try:
            target.relative_to(manifests)
        except ValueError as exc:
            raise ValueError("manifest path escapes the Run manifests directory") from exc
        return target

    def save_manifest(self, run_id: str, relative_path: str, value) -> Path:
        path = self._manifest_path(run_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage.save_json(path, value)
        path.chmod(0o600)
        return path

    def load_manifest(self, run_id: str, relative_path: str):
        path = self._manifest_path(run_id, relative_path)
        return storage.load_json(path, None) if path.is_file() else None

    def staging_details_for_storage(self, run: dict, details: dict) -> dict:
        """Externalize the unbounded staging inventory and relativize workspace paths."""
        stored = deepcopy(details)
        files = list(stored.pop("content_files", []) or [])
        manifest = "manifests/staging-files.json"
        self.save_manifest(str(run["id"]), manifest, files)
        stored.update({"content_file_count": len(files), "content_manifest": manifest})
        workspace = Path(run["workspace"]).resolve()
        for field in ("staging_directory", "content_source"):
            if stored.get(field):
                stored[field] = self._workspace_relative(workspace, stored[field])
        stored["content_sources"] = [self._workspace_relative(workspace, path) for path in stored.get("content_sources", [])]
        validation = stored.get("validation") or {}
        if validation.get("required_paths"):
            validation["required_paths"] = [self._workspace_relative(workspace, path) for path in validation["required_paths"]]
        return stored

    @staticmethod
    def _workspace_relative(workspace: Path, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.resolve(strict=False).relative_to(workspace).as_posix()
        except ValueError:
            return str(path)

    def staging_content_files(self, run_id: str, details: dict) -> list[str]:
        """Read an externalized staging inventory."""
        reference = details.get("content_manifest")
        if not reference:
            return []
        value = self.load_manifest(run_id, str(reference))
        if not isinstance(value, list) or not all(isinstance(row, str) for row in value):
            raise ValueError("staging content manifest must contain a list of paths")
        return value

    def artifact_details_for_storage(self, run: dict, artifact: dict) -> dict:
        stored = deepcopy(artifact)
        inspection = stored.get("inspection") or {}
        files = inspection.pop("files", None)
        if files is not None:
            manifest = "manifests/artifact-files.json"
            self.save_manifest(str(run["id"]), manifest, files)
            inspection["files_manifest"] = manifest
            inspection["file_count"] = inspection.get("file_count", len(files))
        return stored

    def artifact_files(self, run_id: str, inspection: dict) -> list[dict]:
        reference = inspection.get("files_manifest")
        if not reference:
            return []
        value = self.load_manifest(run_id, str(reference))
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError("artifact file manifest must contain a list of records")
        return value
