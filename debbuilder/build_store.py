"""Atomic persistence and workspace ownership for Build Runs."""
from __future__ import annotations

import hashlib
import json
import shutil
import secrets
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import storage
from .build_models import new_run, utc_now, validate_run
from .recipe_schema import recipe_for_storage, require_safe_name

WORKSPACE_DIRECTORIES = ("source", "staging", "artifacts", "logs", "manifests")


def make_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}-{secrets.token_hex(2)}"


class BuildStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def run_dir(self, run_id: str) -> Path:
        require_safe_name(run_id, "build run id")
        return self.root / run_id

    @contextmanager
    def locked_run(self, run_id: str):
        """Serialize a complete lifecycle mutation for one Build Run."""
        path = self.run_dir(run_id) / "run.json"
        with storage.locked_path(path):
            yield

    def create(self, recipe: dict, *, recipe_id: str = "", mode: str = "dry_run", run_id: str | None = None) -> dict:
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
        run = new_run(identifier, recipe_id or canonical["name"], mode, str(folder.resolve()), digest)
        self.save(run)
        (folder / "logs" / "pipeline.log").touch(mode=0o600)
        return run

    def save(self, run: dict) -> None:
        validate_run(run)
        path = self.run_dir(str(run["id"])) / "run.json"
        storage.save_json(path, run)
        path.chmod(0o600)

    def load(self, run_id: str) -> dict | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            return None
        run = storage.load_json(path, None)
        if not isinstance(run, dict):
            return None
        validate_run(run)
        return run

    def list(self, limit: int = 50) -> list[dict]:
        if not self.root.exists():
            return []
        rows = []
        for path in self.root.glob("*/run.json"):
            run = storage.load_json(path, None)
            if isinstance(run, dict):
                try:
                    validate_run(run)
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

    def clear_log_history(self, run_id: str) -> dict:
        run = self.load(run_id)
        if not run:
            raise FileNotFoundError("build run not found")
        workspace = self.run_dir(run_id)
        logs = workspace / "logs"
        removed = []
        if logs.exists():
            shutil.rmtree(logs)
            removed.append("logs")
        logs.mkdir(mode=0o700, exist_ok=True)
        (logs / "commands").mkdir(mode=0o700, exist_ok=True)
        for step in run.get("steps", []):
            details = step.get("details") if isinstance(step, dict) else None
            if isinstance(details, dict):
                for command in details.get("commands") or []:
                    if isinstance(command, dict):
                        command["stdout"] = ""
                        command["stderr"] = ""
                        command["log_deleted"] = True
        for validation in run.get("validations") or []:
            for command in validation.get("commands") or []:
                if isinstance(command, dict):
                    command["stdout"] = ""
                    command["stderr"] = ""
                    command["log_deleted"] = True
        run["events"] = []
        run["log_deleted"] = True
        self.save(run)
        return {"id": run_id, "deleted": "log_history", "removed": removed}

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
