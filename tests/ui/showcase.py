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
    recipes["debbuilder"]["install"]["directories"] = [
        {"path": "/var/lib/debbuilder/builds", "owner": "root", "group": "root", "mode": "0750"},
    ]
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
        "archive_format": "tar.gz",
        "payload": {"mode": "paths", "include": ["bin/archive-agent", "share/defaults.yml"], "exclude": []},
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


def run_step(run: dict, name: str) -> dict:
    return next(step for step in run["steps"] if step["name"] == name)


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
    run_step(run, "source")["details"] = {
        "repository": recipe["source"]["repository"], "strategy": recipe["source"].get("tracking", "latest_release"),
        "ref": f"v{upstream}", "tag": f"v{upstream}", "upstream_version": upstream,
        "debian_version": run["version"]["debian"], "source_directory": "source",
    }
    if status == "prepared":
        for step in run["steps"]:
            complete_step(step, "success" if step["name"] in {"source", "detection", "dependencies", "source_changes", "staging", "debian_metadata"} else "skipped", index)
        run_step(run, "source")["details"] = {
            "repository": recipe["source"]["repository"], "strategy": "latest_release", "ref": f"v{upstream}",
            "tag": f"v{upstream}", "upstream_version": upstream, "debian_version": run["version"]["debian"],
            "source_directory": "source",
        }
        run_step(run, "detection")["details"] = {
            "project_type": "python", "display_name": "Python · pyproject.toml", "detected_files": ["pyproject.toml", "debbuilder/__init__.py"],
            "build_tools": ["python3"], "system_build_dependencies": ["python3", "python3-build"],
            "proposed_commands": ["python3 -m build --wheel"], "suggested_output_paths": ["dist"],
            "warnings": ["No locked Python dependency file was detected."],
        }
        run_step(run, "dependencies")["details"] = {
            "tools": ["python3"], "detected_tools": ["python3"], "available_tools": ["python3"], "missing_tools": [],
            "detected": ["python3", "python3-build"], "manually_added": ["dpkg-dev"],
            "required": ["python3", "python3-build", "dpkg-dev"], "available": ["python3", "python3-build", "dpkg-dev"], "missing": [],
        }
        source_change = recipe["build"]["source_changes"][0]
        run_step(run, "source_changes")["details"] = {
            "requested": 1, "applied_count": 1,
            "applied": [{"index": 1, "operation": source_change["operation"], "path": source_change["path"], "matches": None, "status": "applied", "anchor": "", "anchor_truncated": False}],
        }
        run_step(run, "build")["details"] = {
            "executed": False, "reason": "dry_run", "commands": [],
            "plan": {
                "selection": {"source": "detection_proposal", "commands": ["python3 -m build --wheel"], "confirmed": False},
                "commands": [{"command": "python3 -m build --wheel", "arguments": ["python3", "-m", "build", "--wheel"]}],
                "working_directory": "/showcase/builds/ui-01-prepared/source", "configured_working_directory": ".",
                "environment_keys": ["PYTHONUNBUFFERED"], "inactivity_timeout": 300, "maximum_runtime": 1200,
                "output": {"mode": "paths", "paths": [
                    {"mode": "path", "configured_path": "debbuilder", "path": "/showcase/source/debbuilder", "exists": True, "kind": "directory"},
                    {"mode": "path", "configured_path": "server.py", "path": "/showcase/source/server.py", "exists": True, "kind": "file"},
                    {"mode": "path", "configured_path": "static", "path": "/showcase/source/static", "exists": True, "kind": "directory"},
                ]},
            },
            "output": {"mode": "paths", "paths": [{"configured_path": "debbuilder"}, {"configured_path": "server.py"}, {"configured_path": "static"}]},
        }
        staging = {
            "preview": True, "version": run["version"]["debian"], "install_destination": "/opt/debbuilder",
            "include_output": True, "content_available": True, "content_file_count": 38, "content_manifest": "manifests/staging-files.json",
            "warnings": ["Build output is unavailable because build commands are not executed during dry-run"],
            "ownership": {"user": "root", "group": "root", "applied_by": "postinst"},
            "permissions": {"directories": "0755", "files": "0644"},
            "account": {"user": "root", "group": "root", "create_user": False, "create_group": False},
            "configurations": [{"source": "packaging/debbuilder.env", "destination": "/etc/debbuilder/debbuilder.env", "staged_path": "/usr/share/debbuilder/config-templates/etc/debbuilder/debbuilder.env", "policy": "create_if_missing", "mode": "0644", "owner": "root", "group": "root"}],
            "directories": recipe["install"]["directories"], "conffiles": [],
            "control": f"Package: debbuilder\nVersion: {run['version']['debian']}\nArchitecture: all\nMaintainer: DebBuilder UI <ui@example.test>\nDescription: Debian package build console\n",
            "maintainer_scripts": {"preinst": "#!/bin/sh\nset -e\ninstall -d -m 0750 -o root -g root /var/lib/debbuilder/builds\n", "postinst": "#!/bin/sh\nset -e\nsystemctl daemon-reload || true\nsystemctl enable debbuilder.service || true\n"},
            "systemd": {"configured": True, "enabled": True, "path": "/usr/lib/systemd/system/debbuilder.service", "content": "[Unit]\nDescription=debbuilder\n\n[Service]\nType=simple\nUser=root\nGroup=root\nEnvironment=\"PYTHONUNBUFFERED=1\"\nExecStart=/usr/bin/python3 /opt/debbuilder/server.py\nRestart=on-failure\n\n[Install]\nWantedBy=multi-user.target\n"},
        }
        run_step(run, "staging")["details"] = staging
        run_step(run, "debian_metadata")["details"] = {key: staging[key] for key in ("control", "conffiles", "configurations", "maintainer_scripts")}
        run_step(run, "systemd")["details"] = staging["systemd"]
    elif status == "running":
        complete_step(run["steps"][0], "success", index, "Fetched example/worker-agent at v2.4.0")
        complete_step(run["steps"][1], "success", index, "Detected Node.js project")
        complete_step(run["steps"][2], "running", index, "Checking build dependencies")
    elif status == "failed":
        complete_step(run_step(run, "source"), "success", index, f"Fetched {recipe['source']['repository']}")
        complete_step(run_step(run, "detection"), "success", index, "Detected Node.js project")
        run_step(run, "detection")["details"] = {"project_type": "nodejs", "display_name": "Node.js", "detected_files": ["package.json", "pnpm-lock.yaml"]}
        complete_step(run_step(run, "dependencies"), "success", index, "Build dependencies available")
        complete_step(run_step(run, "source_changes"), "success", index, "No source changes configured")
        complete_step(run_step(run, "build"), "failed", index, "Build command 2 failed with exit code 2")
        failed_command = {
            "index": 2, "command": "pnpm build --filter @example/application-with-a-deliberately-long-workspace-name", "arguments": ["pnpm", "build"],
            "working_directory": "/showcase/builds/ui-04-build-failed/source", "configured_working_directory": ".",
            "status": "failed", "exit_code": 2, "stdout": "Compiling application…\n", "stderr": "Error: module @example/runtime-config could not be resolved\n", "duration": 18.4, "timed_out": False, "timeout_reason": "",
        }
        error = {"stage": "build", "code": "build_command_failed", "message": "Build command 2 failed with exit code 2", "details": {"failed_command": failed_command, "commands": [failed_command], "plan": {"inactivity_timeout": 300, "maximum_runtime": 1200}}}
        run_step(run, "build")["details"] = error["details"]
        run_step(run, "build")["error"] = error
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
                    "error": {"code": "validation_failed", "message": "The isolated service check reported an inactive unit.", "details": {}},
                    "checks": [{"name": "systemd_active_after_grace", "status": "failed", "error": "archive-agent.service exited during startup grace"}],
                    "commands": [{"command": "systemctl is-active --quiet archive-agent.service", "arguments": ["systemctl", "is-active", "--quiet", "archive-agent.service"], "accepted": False, "status": "failed", "exit_code": 3, "stdout": "", "stderr": "inactive", "duration": 0.04}],
                })
            run["validations"] = [validation_record]
        if publication != "not_run":
            publication_record = {
                "id": f"publication-{run_id}", "status": publication, "artifact": str(artifact_path),
                "requested_at": timestamp, "finished_at": timestamp,
                "published_version": run["version"]["debian"], "distribution": "stable", "component": "main",
                "repository": {"root": "/var/lib/debbuilder/repository", "distribution": "stable", "component": "main"},
                "readiness": {"ready": True, "reasons": []}, "preflight": {"architecture_policy": "explicitly configured"},
            }
            if publication == "failed":
                publication_record.update({
                    "published_version": "",
                    "command": {"command": "reprepro -b /var/lib/debbuilder/repository includedeb stable artifact.deb", "status": "failed", "exit_code": 1, "stdout": "", "stderr": "No matching signing key was available", "duration": 0.2},
                    "error": {"code": "reprepro_include_failed", "message": "APT publication failed because reprepro could not sign the exported index.", "details": {}},
                })
            run["publications"] = [publication_record]
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
    seed_run(store, recipes["debbuilder"], "ui-01-prepared", 1, mode="dry_run", status="prepared", upstream="0.1.9")
    seed_run(store, recipes["worker-agent"], "ui-02-running", 2, status="running", upstream="2.4.0")
    seed_run(store, recipes["ssh-notify"], "ui-03-validation-needed", 3, status="success", upstream="2.1.0")
    seed_run(store, recipes["seerr"], "ui-04-build-failed", 4, status="failed", upstream="2.0.0")
    seed_run(store, recipes["archive-agent"], "ui-05-validation-failed", 5, status="success", upstream="5.0.0", validation="failed")
    seed_run(store, recipes["vendor-cli"], "ui-06-ready-to-publish", 6, status="success", upstream="3.3.0", validation="success")
    seed_run(store, recipes["release-tool"], "ui-07-published", 7, status="success", upstream="7.1.0", validation="success", publication="success")
    seed_run(store, recipes["release-tool"], "ui-00-publication-failed", 0, status="success", upstream="6.9.0", validation="success", publication="failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    seed(args.data_dir.resolve(), args.repo_root.resolve())
    print(f"Seeded isolated UI showcase in {args.data_dir}")


if __name__ == "__main__":
    main()
