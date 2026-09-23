"""Package projections built from Recipes, Build Runs and the live APT index."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from . import apt_repo, execution_projection, package_store, storage
from .build_store import BuildStore
from .recipe_schema import SAFE_ARCH, require_safe_name


def recipe_package_name(recipe: dict) -> str:
    package = recipe.get("package")
    if isinstance(package, dict) and package.get("name"):
        return str(package["name"])
    return ""


def normalized_package_name(value: str) -> str:
    return str(value or "").strip().lower()


def github_repo_from_homepage(homepage: str | None) -> str:
    match = re.search(r"github\.com/([^/]+/[^/#?]+)", homepage or "")
    return match.group(1).removesuffix(".git") if match else ""


def build_run_package(run: dict) -> str:
    snapshot = Path(str(run.get("workspace") or "")) / "recipe.json"
    try:
        return recipe_package_name(json.loads(snapshot.read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return str(run.get("recipe_id") or "")


class PackageService:
    """Expose the current package view without owning HTTP or global runtime state."""

    def __init__(
        self,
        *,
        data_dir: Path,
        workspace_root: Path,
        list_workflows: Callable[[], list[dict]],
        workflow_path: Callable[[str], Path | None],
        read_workflow: Callable[[Path], dict],
        repo_settings: Callable[[], dict],
        observation_lookup: Callable[[str, dict], dict | None] = lambda _recipe_id, _recipe: None,
        run_projector: Callable[[dict, BuildStore], dict] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.workspace_root = Path(workspace_root)
        self._list_workflows = list_workflows
        self._workflow_path = workflow_path
        self._read_workflow = read_workflow
        self._repo_settings = repo_settings
        self._observation_lookup = observation_lookup
        self._run_projector = run_projector

    @property
    def packages_file(self) -> Path:
        return self.data_dir / "packages.json"

    def load_overrides(self) -> dict[str, dict]:
        rows = storage.load_json(self.packages_file, [])
        return {str(row.get("name")): row for row in rows if row.get("name")}

    def save_overrides(self, packages: dict[str, dict]) -> None:
        storage.save_json(self.packages_file, sorted(packages.values(), key=lambda row: row.get("name", "")))

    def recipe_records_by_package(self) -> dict[str, dict]:
        """Index canonical Recipes by package name and expose active collisions."""
        candidates: dict[str, list[dict]] = {}
        for listed in self._list_workflows():
            recipe_id = str(listed.get("id") or "")
            if not recipe_id:
                continue
            path = self._workflow_path(recipe_id)
            if not path:
                continue
            try:
                workflow = self._read_workflow(path)
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue
            package_key = normalized_package_name(recipe_package_name(workflow))
            if package_key:
                candidates.setdefault(package_key, []).append({"id": recipe_id, "workflow": workflow})

        records: dict[str, dict] = {}
        for package_key, rows in candidates.items():
            active = [row for row in rows if row["workflow"].get("active") is not False]
            selectable = active if active else rows
            if len(selectable) == 1:
                records[package_key] = selectable[0]
            else:
                records[package_key] = {
                    "id": "",
                    "workflow": None,
                    "ambiguous": True,
                    "candidates": [row["id"] for row in selectable],
                }
        return records

    def merge_recipe_metadata(self, package: dict, record: dict | None) -> dict:
        if not record:
            return package
        if record.get("ambiguous"):
            return {
                **package,
                "recipe": "",
                "recipe_error": {"code": "ambiguous_recipe", "candidates": record["candidates"]},
            }
        recipe = record["workflow"]
        package_data = recipe["package"]
        source_data = recipe["source"]
        merged = {
            **package,
            "recipe": record["id"],
            "description": package_data["description"],
            "architecture": package_data["architecture"],
            "depends": ", ".join(package_data["runtime_dependencies"]),
            "tracking": source_data["tracking"],
            "source_ref": source_data["ref"],
        }
        repository = str(source_data.get("repository") or "").strip()
        if not repository:
            return merged
        old_source = dict(merged.get("source") or {})
        source = {
            **old_source,
            "type": "github",
            "repository": repository,
            "ref_type": "release",
        }
        upstream = ""
        observation = self._observation_lookup(str(record["id"]), recipe)
        if observation:
            success = observation.get("last_success") or {}
            attempt = observation.get("last_attempt") or {}
            upstream = str(success.get("display_version") or upstream)
            observed_ref = str(success.get("display_ref") or "")
            if observed_ref:
                source.update({"latest_release": observed_ref, "release": observed_ref})
            observation_projection = {
                "state": "failed" if attempt.get("status") == "failed" else "observed",
                "observed_at": success.get("observed_at"),
                "display_version": success.get("display_version", ""),
                "display_ref": success.get("display_ref", ""),
                "attempted_at": attempt.get("attempted_at"),
                "attempt_status": attempt.get("status", ""),
                "classification": attempt.get("classification", ""),
                "diagnostic_code": attempt.get("diagnostic_code"),
            }
        else:
            observation_projection = {
                "state": "unavailable", "observed_at": None, "display_version": "", "display_ref": "",
                "attempted_at": None, "attempt_status": "", "classification": "", "diagnostic_code": None,
            }
        merged.update({
            "source": source,
            "upstream_version": upstream,
            "observation": observation_projection,
            "version_strategy": f"github_{(source_data.get('version') or {}).get('source') or 'tag'}",
        })
        return merged

    def package_from_inventory(self, row: dict, recipes: dict[str, dict]) -> dict:
        name = row.get("Package", "")
        source_repo = github_repo_from_homepage(row.get("Homepage"))
        record = recipes.get(normalized_package_name(name))
        recipe_id = str(record.get("id") or "") if record else ""
        return {
            "name": name,
            "apt_version": row.get("Version"),
            "upstream_version": None,
            "source": {"type": "github", "repository": source_repo} if source_repo else {"type": "apt-inventory"},
            "architecture": row.get("Architecture") or "all",
            "status": "ready" if recipe_id else "recipe_missing",
            "recipe": recipe_id,
            "last_build": None,
            "description": row.get("Description") or "",
            "filename": row.get("Filename"),
            "depends": row.get("Depends"),
        }

    def list_packages(self, *, include_history: bool = False, live_rows: list[dict] | None = None) -> list[dict]:
        packages: dict[str, dict] = {}
        recipes = self.recipe_records_by_package()
        live_rows = [] if live_rows is None else live_rows
        for row in live_rows:
            key = normalized_package_name(row.get("Package"))
            if key:
                packages.setdefault(key, self.package_from_inventory(row, recipes))
        for key, record in recipes.items():
            workflow = record.get("workflow") if record else None
            name = recipe_package_name(workflow) if workflow else key
            packages.setdefault(key, self._empty_package(name))
        for name, override in self.load_overrides().items():
            package_key = normalized_package_name(name)
            if override.get("deleted"):
                packages.pop(package_key, None)
                continue
            merged = {**packages.get(package_key, self._empty_package(name, nullable=True)), **override}
            merged = self.merge_recipe_metadata(merged, recipes.get(package_key))
            if not merged.get("status") or merged.get("status") == "unknown":
                merged["status"] = "ready" if merged.get("recipe") else "recipe_missing"
            packages[package_key] = merged
        for package_key, package in list(packages.items()):
            packages[package_key] = self.merge_recipe_metadata(package, recipes.get(package_key))

        live_by_name = {
            normalized_package_name(row.get("Package")): row for row in live_rows if row.get("Package")
        }
        runs_by_package: dict[str, list[dict]] = {}
        build_store = BuildStore(self.data_dir / "builds")
        for stored_run in build_store.list(limit=1000):
            if self._run_projector is not None:
                stored_run = self._run_projector(stored_run, build_store)
            run = {
                **stored_run,
                "_execution_history_deleted": build_store.execution_history_deleted(str(stored_run["id"]), stored_run),
            }
            key = normalized_package_name(build_run_package(run))
            if key:
                packages.setdefault(key, self._empty_package(key))
                runs_by_package.setdefault(key, []).append(run)

        apt = self._repo_settings()
        enriched = []
        for package in packages.values():
            enriched.append(self._enrich_package(
                package,
                live_by_name.get(normalized_package_name(package.get("name"))) or {},
                runs_by_package.get(normalized_package_name(package.get("name")), []),
                apt,
                include_history,
            ))
        return sorted(enriched, key=lambda package: package.get("name", ""))

    @staticmethod
    def _empty_package(name: str, *, nullable: bool = False) -> dict:
        empty = None if nullable else ""
        return {
            "name": name,
            "apt_version": empty,
            "upstream_version": empty,
            "source": {"type": "manual"},
            "architecture": "all",
            "recipe": "",
            "last_build": None,
            "description": "",
            "depends": empty,
        }

    def _enrich_package(self, package: dict, live: dict, runs: list[dict], apt: dict, include_history: bool) -> dict:
        published_version = live.get("Version") or ""
        package = {**package, "apt_version": published_version}
        if live.get("Version"):
            package = {
                **package,
                "apt_version": live.get("Version"),
                "architecture": live.get("Architecture") or package.get("architecture"),
                "published_filename": live.get("Filename", ""),
            }
        run_state = package_store.summarize_runs(runs, execution_projection.public_summary, include_history=include_history)
        successful, resolved = run_state["successful"], run_state["resolved"]
        candidate = (successful.get("version") or {}).get("debian", "") if successful else ""
        latest_validation = run_state["latest_validation"]
        latest_publication = run_state["latest_publication"]
        verified = bool(
            successful
            and (successful.get("publication_insertion_eligibility") or {}).get("eligible") is True
        )
        if include_history:
            package["history"] = run_state["history"][:200]
        if successful:
            durable_artifact = successful["artifact"]
            artifact = execution_projection.public_artifact(durable_artifact) or {}
            inspection = artifact.get("inspection") or {}
            package = {
                **package,
                "last_artifact": artifact.get("name", ""),
                "last_build": execution_projection.public_summary(successful),
                "artifact_source": artifact.get("source", "local_build"),
                "artifact_sha256": artifact.get("sha256", ""),
                "artifact_filename": artifact.get("name", ""),
                "build_method": "upstream_artifact" if artifact.get("source") == "upstream_release" else "recipe",
                "depends": inspection.get("depends") or package.get("depends", ""),
                "description": inspection.get("description") or package.get("description", ""),
            }
        upstream = package.get("upstream_version", "")
        built_upstream = (successful.get("version") or {}).get("upstream", "") if successful else ""
        if resolved:
            observed_latest_release = (package.get("source") or {}).get("latest_release", "")
            source_step = next((step for step in resolved.get("steps", []) if step.get("name") == "source"), {})
            source_details = source_step.get("details") or {}
            public_source = execution_projection.public_source(source_details)
            selected_source_asset = public_source.get("asset") or {}
            package["source"] = {
                **(package.get("source") or {}),
                "type": "github",
                "repository": public_source.get("repository") or (package.get("source") or {}).get("repository", ""),
                "release": public_source.get("ref", ""),
                "tag": public_source.get("tag", ""),
                "latest_release": observed_latest_release,
                "release_id": public_source.get("release_id"),
                "payload_kind": public_source.get("payload_kind", ""),
                "file_count": public_source.get("file_count"),
                "extracted_file_count": public_source.get("extracted_file_count"),
            }
            if selected_source_asset:
                package["source"]["archive_format"] = selected_source_asset.get("archive_format", "")
            if selected_source_asset.get("source") == "release_asset":
                package["source"].update({
                    "type": "github_release_asset",
                    "asset_id": selected_source_asset.get("id"),
                    "asset_name": selected_source_asset.get("name", ""),
                    "asset_pattern": selected_source_asset.get("name", ""),
                    "content_type": selected_source_asset.get("content_type", ""),
                    "declared_size": selected_source_asset.get("declared_size"),
                    "download_size": selected_source_asset.get("size"),
                    "payload_kind": selected_source_asset.get("payload_kind", public_source.get("payload_kind", "")),
                    "file_count": selected_source_asset.get("file_count", public_source.get("file_count")),
                    "archive_format": selected_source_asset.get("archive_format", ""),
                    "sha256": selected_source_asset.get("sha256", ""),
                    "expected_sha256": selected_source_asset.get("expected_sha256", ""),
                    "checksum_verified": selected_source_asset.get("checksum_verified", False),
                })
            if (successful or {}).get("artifact", {}).get("source") == "upstream_release":
                package["source"].update({
                    "type": "github_release_asset",
                    "asset_pattern": ((successful["artifact"].get("release_asset") or {}).get("name", "")),
                })
        state_source = candidate if upstream and built_upstream == upstream else upstream
        candidate_newer = self._version_is_newer(candidate, published_version) if candidate and published_version and candidate != published_version else None
        item = package_store.enrich_package(
            package,
            published_version=published_version,
            source_version=upstream,
            built_version=candidate,
            has_verified_build=verified,
            state_source_version=state_source,
            candidate_is_newer=candidate_newer,
        )
        item["build"].update({
            "validated": verified,
            "latest_run": run_state["last_real"],
            "latest_run_id": (run_state["last_real"] or {}).get("id", ""),
            "latest_status": (run_state["last_real"] or {}).get("status", ""),
            "last_real": run_state["last_real"],
            "last_dry_run": run_state["last_dry_run"],
        })
        latest_real_run = next((run for run in runs if run.get("mode") == "build"), None)
        item["validation"] = execution_projection.public_validation(latest_validation) if latest_validation else None
        item["publication"] = (
            execution_projection.public_publication(latest_publication, run=latest_real_run)
            if latest_publication else None
        )
        item["lifecycle_display_status"] = (run_state["last_real"] or {}).get("lifecycle_status") or item["lifecycle_state"]
        item["build"]["ready_to_publish"] = item["lifecycle_display_status"] == "ready_to_publish"
        eligibility = (latest_real_run or {}).get("publication_insertion_eligibility") or {
            "eligible": False, "reasons": ["current_validation_required"], "validation_id": "",
        }
        item["publication_insertion_eligible"] = eligibility.get("eligible") is True
        item["publication_insertion_reasons"] = list(eligibility.get("reasons") or [])
        item["already_published"] = (latest_real_run or {}).get("already_published") is True
        item["publication_reconciliation_available"] = (
            (latest_real_run or {}).get("publication_reconciliation_available") is True
        )
        item["allowed_actions"] = package_store.allowed_actions(
            item["lifecycle_state"], str(item.get("recipe") or ""), latest_real_run,
        )
        item["repository"].update({
            "url": apt["repository"],
            "distribution": apt["distribution"],
            "component": apt["component"],
        })
        return item

    def _version_is_newer(self, candidate: str, published: str) -> bool:
        if not published:
            return True
        relation = apt_repo.debian_version_relation(candidate, published, workspace=self.workspace_root)
        return relation["relation"] == "newer"

    def normalize_package(self, data: dict, existing: dict | None = None) -> dict:
        name = require_safe_name(data.get("name") or (existing or {}).get("name"), "package")
        package = dict(existing or {"name": name})
        package["name"] = name
        if "architecture" in data:
            architecture = data.get("architecture") or "all"
            if architecture not in SAFE_ARCH:
                raise ValueError("unsupported architecture")
            package["architecture"] = architecture
        if "source" in data:
            source = data.get("source") or {"type": "manual"}
            if not isinstance(source, dict):
                raise ValueError("source must be an object")
            if source.get("repository") and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", str(source["repository"])):
                raise ValueError("invalid github repository")
            package["source"] = source
        if "recipe" in data:
            recipe_id = data.get("recipe") or ""
            if recipe_id:
                require_safe_name(recipe_id, "recipe")
                if not self._workflow_path(recipe_id):
                    raise ValueError("recipe not found")
            package["recipe"] = recipe_id
        for key in ("apt_version", "upstream_version", "status", "description"):
            if key in data:
                package[key] = data.get(key)
        if not package.get("status"):
            package["status"] = "ready" if package.get("recipe") else "recipe_missing"
        return package

    def create_or_update(self, data: dict, *, name: str | None = None, current: dict | None = None) -> dict:
        with storage.locked_path(self.packages_file):
            overrides = self.load_overrides()
            if name and not current and name not in overrides:
                raise KeyError("package not found")
            package = self.normalize_package({**data, **({"name": name} if name else {})}, current)
            overrides[package["name"]] = package
            self.save_overrides(overrides)
            return package

    def associate_workflow(self, recipe_id: str, workflow: dict, previous_id: str = "") -> None:
        package_name = recipe_package_name(workflow).strip()
        if not package_name:
            return
        require_safe_name(package_name, "package name")
        package_key = normalized_package_name(package_name)
        with storage.locked_path(self.packages_file):
            overrides = self.load_overrides()
            for name, row in list(overrides.items()):
                if isinstance(row, dict) and row.get("recipe") in {recipe_id, previous_id} and name != package_name:
                    updated = dict(row)
                    updated.pop("recipe", None)
                    updated["status"] = "recipe_missing"
                    overrides[name] = updated
            stored_name = next((name for name in overrides if normalized_package_name(name) == package_key), "")
            current = dict(overrides.get(stored_name) or {})
            if not current:
                current = {
                    "name": package_name,
                    "architecture": self._repo_settings()["architecture"],
                }
            canonical_name = str(current.get("name") or package_name)
            stored = dict(overrides.get(stored_name) or {})
            source = dict(stored.get("source") or current.get("source") or {})
            repository = str(((workflow.get("source") or {}).get("repository") or "")).strip()
            if repository:
                source.update({"type": "github", "repository": repository})
            if stored_name and stored_name != canonical_name:
                overrides.pop(stored_name, None)
            overrides[canonical_name] = {
                **current,
                **stored,
                "name": canonical_name,
                "recipe": recipe_id,
                "source": source,
                "status": "ready",
            }
            self.save_overrides(overrides)

    def unlink_recipe(self, recipe_id: str) -> None:
        with storage.locked_path(self.packages_file):
            overrides = self.load_overrides()
            changed = False
            for name, package in list(overrides.items()):
                if isinstance(package, dict) and package.get("recipe") == recipe_id:
                    updated = dict(package)
                    updated.pop("recipe", None)
                    updated["status"] = "recipe_missing"
                    overrides[name] = updated
                    changed = True
            if changed:
                self.save_overrides(overrides)

    def mark_deleted(self, name: str) -> None:
        require_safe_name(name, "package")
        with storage.locked_path(self.packages_file):
            overrides = self.load_overrides()
            overrides[name] = {"name": name, "deleted": True}
            self.save_overrides(overrides)
