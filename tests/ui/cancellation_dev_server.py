#!/usr/bin/env python3
"""Launch an isolated, deterministic DEV server for manual cancellation review."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


WORKER = Path(__file__).with_name("cancellation_worker.py")


def recipe() -> dict:
    return {
        "schema_version": 1,
        "name": "cancellation-review",
        "active": True,
        "package": {
            "name": "cancellation-review",
            "version_revision": "1",
            "architecture": "all",
            "maintainer": "DEV Review <dev@example.test>",
            "description": "Isolated deterministic cancellation review fixture",
        },
        "source": {
            "provider": "github",
            "repository": "example/cancellation-review",
            "tracking": "latest_release",
            "version": {"source": "tag"},
        },
        "artifact": {"mode": "source_build", "type": "deb", "architecture": "all"},
        "build": {
            "commands": [],
            "working_directory": ".",
            "environment": {},
            "extra_dependencies": [],
            "source_changes": [],
            "output": {"mode": "source"},
            "inactivity_timeout": None,
            "maximum_runtime": 600,
        },
        "install": {
            "destination": "/opt/cancellation-review",
            "content": {"source": "build_output", "path": ""},
            "owner": {"user": "root", "group": "root"},
            "permissions": {"directories": "0755", "files": "0644"},
            "config_files": [],
            "directories": [],
        },
        "service": {"enabled": False},
    }


def prepare_runtime(data_dir: Path, repo_root: Path) -> None:
    if data_dir.exists() or repo_root.exists():
        raise SystemExit("Refusing to reuse an existing data or repository directory")
    (data_dir / "workflows").mkdir(parents=True)
    repo_root.mkdir(parents=True)
    (data_dir / "workflows/cancellation-review.json").write_text(json.dumps(recipe(), indent=2) + "\n")
    (data_dir / "repo-current-packages-inventory.json").write_text("[]\n")
    expiry = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    cache = {
        "example/cancellation-review": {
            "expires_at": expiry,
            "release": {
                "tag": "v1.0.0", "name": "Cancellation review 1.0.0",
                "url": "https://example.invalid/cancellation-review/v1.0.0", "assets": [],
            },
        },
    }
    (data_dir / "github-release-cache.json").write_text(json.dumps(cache, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    runtime_root = Path(tempfile.mkdtemp(prefix="debbuilder-cancellation-review-"))
    data_dir = runtime_root / "data"
    repo_root = runtime_root / "repository"
    prepare_runtime(data_dir, repo_root)
    os.environ.update({
        "DEBBUILDER_DATA_DIR": str(data_dir),
        "DEBBUILDER_REPO_ROOT": str(repo_root),
        "DEBBUILDER_REPO_URL": "https://repo.example.invalid/cancellation-review",
        "DEBBUILDER_HOST": "127.0.0.1",
        "DEBBUILDER_PORT": str(args.port),
        "DEBBUILDER_AUTH_MODE": "none",
    })

    from debbuilder import app, build_pipeline, command_runner
    from debbuilder.build_models import utc_now

    def acquire(_recipe, workspace, token="", cancellation_event=None, on_cancel=None):
        root = Path(workspace)
        source = root / "source"
        source.mkdir(exist_ok=True)
        shutil.copy2(WORKER, source / WORKER.name)
        command = shlex.join([sys.executable, str(source / WORKER.name), "2"])

        def retain_output(item: dict) -> None:
            text = str(item.get("text") or "").rstrip()
            if text:
                with (root / "logs/pipeline.log").open("a") as log:
                    for line in text.splitlines():
                        log.write(f"{utc_now()} INFO cancellation fixture {item.get('stream')}: {line}\n")

        command_runner.run_command(
            command,
            workspace=root,
            working_directory="source",
            inactivity_timeout=None,
            maximum_runtime=600,
            cancellation_event=cancellation_event,
            on_cancel=on_cancel,
            on_output=retain_output,
        )
        (source / "README.txt").write_text("Cancellation fixture completed.\n")
        return {
            "repository": "example/cancellation-review", "strategy": "latest_release",
            "ref": "v1.0.0", "tag": "v1.0.0", "release_name": "Cancellation review 1.0.0",
            "release_url": "https://example.invalid/cancellation-review/v1.0.0",
            "archive_url": "", "upstream_version": "1.0.0", "debian_version": "1.0.0-1",
            "source_directory": str(source),
        }

    def execute_queued(run_id, *, store, expected_initial_status, cancellation_control):
        def controlled_acquire(selected_recipe, workspace, token=""):
            return acquire(
                selected_recipe,
                workspace,
                token=token,
                cancellation_event=cancellation_control.event,
                on_cancel=lambda: {
                    **(cancellation_control.request or {}), "phase": "pipeline", "stage": "source",
                },
            )

        try:
            return build_pipeline.execute_pipeline_run(
                run_id,
                store=store,
                expected_initial_status=expected_initial_status,
                cancellation_control=cancellation_control,
                acquire=controlled_acquire,
                lifecycle_callback=app.notify_lifecycle,
            )
        finally:
            app.cleanup_workspaces()

    app.execute_queued_recipe_run = execute_queued
    from server import main as serve

    print(f"Isolated cancellation review data: {runtime_root}")
    print(f"Open http://127.0.0.1:{args.port} and select the cancellation-review Recipe")
    try:
        try:
            serve()
        except KeyboardInterrupt:
            print("Stopping isolated cancellation review server")
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    main()
