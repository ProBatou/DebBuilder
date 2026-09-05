#!/usr/bin/env python3
"""Populate an isolated DebBuilder runtime with deterministic UI showcase data."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from debbuilder import storage
from debbuilder.build_store import BuildStore
from debbuilder.recipe_schema import recipe_for_storage


ROOT = Path(__file__).resolve().parents[2]
RECIPE_FIXTURES = ROOT / "tests" / "fixtures" / "recipes"
FIXED_RELEASE_EXPIRY = 4_102_444_800


def load_recipe(name: str) -> dict:
    return recipe_for_storage(json.loads((RECIPE_FIXTURES / f"{name}.json").read_text()))


def showcase_recipes() -> dict[str, dict]:
    recipes = {name: load_recipe(name) for name in ("bashrc", "debbuilder", "seerr", "ssh-notify")}
    recipes["debbuilder"]["build"].update({
        "extra_dependencies": ["python3", "dpkg-dev"],
        "source_changes": [{
            "operation": "create_file",
            "path": "debbuilder/ui-showcase.conf",
            "content": "UI_SHOWCASE=1\n",
        }],
    })
    archive = copy.deepcopy(recipes["ssh-notify"])
    archive.update({"name": "archive-agent"})
    archive["package"].update({
        "name": "archive-agent", "version_revision": "3", "architecture": "amd64",
        "description": "Prebuilt archive installed with explicit file mappings",
        "runtime_dependencies": ["ca-certificates", "libssl3"],
    })
    archive["source"].update({"repository": "example/archive-agent"})
    archive["artifact"] = {
        "mode": "upstream_archive", "type": "archive", "architecture": "amd64",
        "archive_source": "release_asset", "asset_selection": "pattern",
        "name_pattern": "archive-agent-linux-amd64\\.tar\\.gz", "asset_name": "",
        "archive_format": "tar.gz", "selected_files": ["bin/archive-agent", "share/defaults.yml"],
        "match_package": True, "match_version": True,
    }
    archive["install"].update({
        "destination": "/opt/archive-agent",
        "content": {"source": "configured_files", "path": ""},
        "config_files": [
            {"source": "bin/archive-agent", "destination": "/usr/bin/archive-agent", "policy": "replace", "mode": "0755"},
            {"source": "share/defaults.yml", "destination": "/etc/archive-agent/config.yml", "policy": "dpkg_conffile"},
        ],
        "directories": [{"path": "/var/lib/archive-agent", "owner": "archive-agent", "group": "archive-agent", "mode": "0750"}],
    })
    archive["service"] = {
        "enabled": True, "name": "archive-agent.service", "type": "simple",
        "user": "archive-agent", "group": "archive-agent", "command": "/usr/bin/archive-agent serve",
        "restart": "on-failure", "after": ["network-online.target"], "wants": ["network-online.target"],
    }
    recipes["archive-agent"] = recipe_for_storage(archive)

    upstream_deb = copy.deepcopy(recipes["bashrc"])
    upstream_deb.update({"name": "vendor-cli"})
    upstream_deb["package"].update({
        "name": "vendor-cli", "version_revision": "1", "architecture": "arm64",
        "description": "Vendor-provided Debian command-line client",
    })
    upstream_deb["source"].update({"repository": "example/vendor-cli"})
    upstream_deb["artifact"] = {
        "mode": "upstream_deb", "type": "deb", "architecture": "arm64",
        "name_pattern": "vendor-cli_.*_arm64\\.deb", "match_package": True, "match_version": True,
    }
    upstream_deb["install"] = {}
    upstream_deb["service"] = {"enabled": False}
    recipes["vendor-cli"] = recipe_for_storage(upstream_deb)

    worker = copy.deepcopy(recipes["seerr"])
    worker.update({"name": "worker-agent"})
    worker["package"].update({"name": "worker-agent", "version_revision": "1", "architecture": "amd64"})
    worker["source"].update({"repository": "example/worker-agent"})
    worker["service"].update({"name": "worker-agent.service", "command": "/usr/bin/node /opt/worker-agent/dist/worker.js"})
    recipes["worker-agent"] = recipe_for_storage(worker)

    release_tool = copy.deepcopy(recipes["bashrc"])
    release_tool.update({"name": "release-tool"})
    release_tool["package"].update({"name": "release-tool", "version_revision": "4", "architecture": "all"})
    release_tool["source"].update({"repository": "example/release-tool"})
    recipes["release-tool"] = recipe_for_storage(release_tool)
    return {name: recipe_for_storage(recipe) for name, recipe in recipes.items()}


def fixed_time(index: int) -> tuple[str, float]:
    epoch = 1_788_544_800 + index * 300
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(), float(epoch)


def complete_step(step: dict, status: str, index: int, summary: str = "") -> None:
    timestamp, _ = fixed_time(index)
    step.update({
        "status": status,
        "started_at": timestamp if status != "pending" else None,
        "finished_at": timestamp if status not in {"pending", "running"} else None,
        "duration": 0.42 if status not in {"pending", "running"} else None,
        "summary": summary or f"{step['name'].replace('_', ' ').title()} {status}",
        "details": {},
        "error": None,
    })


def seed_run(
    store: BuildStore,
    recipe: dict,
    run_id: str,
    index: int,
    *,
    mode: str = "build",
    status: str,
    upstream: str,
    validation: str = "not_run",
    publication: str = "not_run",
) -> None:
    run = store.create(recipe, recipe_id=recipe["name"], mode=mode, run_id=run_id)
    timestamp, epoch = fixed_time(index)
    run.update({
        "created_at": timestamp,
        "created_at_epoch": epoch,
        "started_at": timestamp,
        "finished_at": None if status == "running" else timestamp,
        "duration": None if status == "running" else 12.75,
        "status": status,
        "version": {"upstream": upstream, "debian": f"{upstream}-{recipe['package']['version_revision']}"},
        "events": [
            {"at": timestamp, "level": "info", "message": "Build tools: deterministic showcase toolchain"},
            {"at": timestamp, "level": "info", "message": "Dependencies: resolved from the isolated fixture"},
        ],
    })
    if status == "prepared":
        for step in run["steps"]:
            complete_step(step, "success" if step["name"] in {"source", "detection", "dependencies", "source_changes", "staging", "debian_metadata"} else "skipped", index)
    elif status == "running":
        complete_step(run["steps"][0], "success", index, "Fetched example/worker-agent at v2.4.0")
        complete_step(run["steps"][1], "success", index, "Detected Node.js project")
        complete_step(run["steps"][2], "running", index, "Checking build dependencies")
    elif status == "failed":
        complete_step(run["steps"][0], "success", index, f"Fetched {recipe['source']['repository']}")
        complete_step(run["steps"][1], "failed", index, "Project detection failed")
        error = {"stage": "detection", "code": "project_not_detected", "message": "No supported project marker was found in the source archive."}
        run["steps"][1]["error"] = error
        run["error"] = error
    elif status == "success":
        for step in run["steps"]:
            step_status = "skipped" if step["name"] == "systemd" and not recipe["service"]["enabled"] else "success"
            complete_step(step, step_status, index)
        artifact_name = f"{recipe['package']['name']}_{run['version']['debian']}_{recipe['package']['architecture']}.deb"
        artifact_path = Path(run["workspace"]) / "artifacts" / artifact_name
        artifact_path.write_bytes(b"deterministic UI showcase artifact\n")
        run["artifact"] = {
            "path": str(artifact_path), "name": artifact_name, "size": artifact_path.stat().st_size,
            "sha256": "8d42585d4a877e05b642aa89e623a4b45884c65ddf5c24402c1b78d173b17a8c",
            "source": "upstream_release" if recipe["artifact"]["mode"] == "upstream_deb" else "local_build",
            "inspection": {
                "package": recipe["package"]["name"], "version": run["version"]["debian"],
                "architecture": recipe["package"]["architecture"], "description": recipe["package"]["description"],
                "depends": ", ".join(recipe["package"]["runtime_dependencies"]),
            },
        }
        if validation != "not_run":
            validation_record = {
                "id": f"validation-{run_id}", "status": validation, "artifact": str(artifact_path),
                "started_at": timestamp, "finished_at": timestamp, "profile": {"name": "bookworm"},
                "backend": {"runtime": "podman", "network": "disabled"},
                "checks": [{"name": "package_metadata", "status": "success"}],
            }
            if validation == "failed":
                validation_record.update({
                    "error": {"code": "validation_failed", "message": "The isolated upgrade scenario reported a configuration conflict."},
                    "checks": [{"name": "upgrade", "status": "failed", "summary": "Configuration conflict detected"}],
                })
            run["validations"] = [validation_record]
        if publication != "not_run":
            run["publications"] = [{
                "id": f"publication-{run_id}", "status": publication, "artifact": str(artifact_path),
                "requested_at": timestamp, "finished_at": timestamp,
                "published_version": run["version"]["debian"], "distribution": "stable", "component": "main",
            }]
    store.save(run)
    store.append_log_line(run_id, f"Run {run_id} entered canonical state {status}")
    if validation != "not_run":
        store.append_log_line(run_id, f"validation {validation}")
    if publication != "not_run":
        store.append_log_line(run_id, f"publication {publication}")


def seed(data_dir: Path, repo_root: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=False)
    repo_root.mkdir(parents=True, exist_ok=False)
    workflows = data_dir / "workflows"
    workflows.mkdir()
    recipes = showcase_recipes()
    for name, recipe in recipes.items():
        storage.save_json(workflows / f"{name}.json", recipe)

    releases = {
        recipe["source"]["repository"]: {
            "expires_at": FIXED_RELEASE_EXPIRY,
            "release": {
                "tag": f"v{version}", "name": f"Release {version}",
                "url": f"https://github.com/{recipe['source']['repository']}/releases/tag/v{version}",
                "assets": [],
            },
        }
        for name, recipe, version in (
            ("bashrc", recipes["bashrc"], "1.4.0"),
            ("debbuilder", recipes["debbuilder"], "0.1.9"),
            ("seerr", recipes["seerr"], "2.0.0"),
            ("ssh-notify", recipes["ssh-notify"], "2.1.0"),
            ("archive-agent", recipes["archive-agent"], "5.0.0"),
            ("vendor-cli", recipes["vendor-cli"], "3.3.0"),
            ("worker-agent", recipes["worker-agent"], "2.4.0"),
            ("release-tool", recipes["release-tool"], "7.1.0"),
        )
    }
    storage.save_json(data_dir / "github-release-cache.json", releases)
    storage.save_json(data_dir / "repo-current-packages-inventory.json", [
        {"Package": "bashrc", "Version": "1.4.0-1", "Architecture": "all", "Description": "Managed shell defaults"},
        {"Package": "debbuilder", "Version": "0.1.8-2", "Architecture": "all", "Description": "Debian package build console"},
        {"Package": "release-tool", "Version": "7.1.0-4", "Architecture": "all", "Description": "Published release helper"},
    ])

    store = BuildStore(data_dir / "builds")
    seed_run(store, recipes["seerr"], "ui-01-prepared", 1, mode="dry_run", status="prepared", upstream="2.0.0")
    seed_run(store, recipes["worker-agent"], "ui-02-running", 2, status="running", upstream="2.4.0")
    seed_run(store, recipes["ssh-notify"], "ui-03-validation-needed", 3, status="success", upstream="2.1.0")
    seed_run(store, recipes["seerr"], "ui-04-build-failed", 4, status="failed", upstream="2.0.0")
    seed_run(store, recipes["archive-agent"], "ui-05-validation-failed", 5, status="success", upstream="5.0.0", validation="failed")
    seed_run(store, recipes["vendor-cli"], "ui-06-ready-to-publish", 6, status="success", upstream="3.3.0", validation="success")
    seed_run(store, recipes["release-tool"], "ui-07-published", 7, status="success", upstream="7.1.0", validation="success", publication="success")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    seed(args.data_dir.resolve(), args.repo_root.resolve())
    print(f"Seeded isolated UI showcase in {args.data_dir}")


if __name__ == "__main__":
    main()
