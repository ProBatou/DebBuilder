#!/usr/bin/env python3
"""Isolated process fixture for exercising DebBuilder's real signal lifecycle."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from debbuilder import app, build_pipeline, command_runner, settings_store
from debbuilder.build_store import BuildStore
from debbuilder.command_containment import ContainmentCapability


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--shutdown-timeout", type=float, default=10.0)
    parser.add_argument("--stuck-worker", action="store_true")
    parser.add_argument("--ignore-command-cancellation", action="store_true")
    parser.add_argument("--force-process-group", action="store_true")
    parser.add_argument(
        "--pause-phase",
        choices=("directories", "recovery", "authentication", "migration", "reconciliation", "manager_start", "retention"),
    )
    args = parser.parse_args()
    if args.force_process_group:
        command_runner.containment_capability = lambda: ContainmentCapability(
            "process_group", False, "forced fallback for shutdown fixture",
        )
    store = BuildStore(app.DATA / "builds")
    active_announced: set[str] = set()

    def execute(run_id, *, cancellation_control, **kwargs):
        if args.stuck_worker:
            kwargs["store"].transition_status(
                run_id, expected=kwargs["expected_initial_status"], status="running",
            )
            print("ACTIVE " + json.dumps({"run_id": run_id}), flush=True)
            threading.Event().wait(60)
            return None

        def acquire(_recipe, workspace, token=""):
            source = Path(workspace) / "source"
            source.mkdir(exist_ok=True)
            command_script = "import time; print('command-ready', flush=True); time.sleep(60)"
            if args.ignore_command_cancellation:
                command_script = (
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('command-ready', flush=True); time.sleep(60)"
                )
            command = shlex.join([
                sys.executable,
                "-c",
                command_script,
            ])

            def announce(item):
                if "command-ready" in str(item.get("text") or "") and run_id not in active_announced:
                    active_announced.add(run_id)
                    print("ACTIVE " + json.dumps({"run_id": run_id}), flush=True)

            command_runner.run_command(
                command,
                workspace=workspace,
                working_directory="source",
                inactivity_timeout=None,
                maximum_runtime=120,
                cancellation_event=None if args.ignore_command_cancellation else cancellation_control.event,
                on_cancel=lambda: {
                    **(cancellation_control.request or {}),
                    "phase": "pipeline",
                    "stage": "source",
                },
                on_output=announce,
            )
            return {
                "repository": "owner/signal-fixture",
                "ref": "v1.0.0",
                "tag": "v1.0.0",
                "upstream_version": "1.0.0",
                "debian_version": "1.0.0-1",
                "source_directory": str(source),
            }

        return build_pipeline.execute_pipeline_run(
            run_id,
            cancellation_control=cancellation_control,
            acquire=acquire,
            **kwargs,
        )

    class ReadyServer(ThreadingHTTPServer):
        closed = False

        def serve_forever(self, poll_interval=0.5):
            print("READY " + json.dumps({
                "port": self.server_address[1],
                "recovery": self.execution_recovery,
            }), flush=True)
            return super().serve_forever(poll_interval=poll_interval)

        def server_close(self):
            self.closed = True
            return super().server_close()

    def phase_barrier(name):
        if args.pause_phase == name:
            os.write(sys.stdout.fileno(), ("PHASE " + json.dumps({"name": name}) + "\n").encode())
            if sys.stdin.buffer.read(1) != b"x":
                raise RuntimeError("startup phase barrier was not released")

    original_recovery = app.execution_recovery.recover_startup
    original_migration = app.recipe_store.migrate_recipe_directory
    original_reconciliation = app.builtin_recipe.reconcile_builtin_recipe
    original_directories = app.prepare_application_directories
    original_atomic_write = settings_store.storage.atomic_write_text

    def prepare_directories():
        phase_barrier("directories")
        return original_directories()

    def recover_startup(*call_args, **call_kwargs):
        phase_barrier("recovery")
        return original_recovery(*call_args, **call_kwargs)

    def migrate_recipes(*call_args, **call_kwargs):
        phase_barrier("migration")
        return original_migration(*call_args, **call_kwargs)

    def reconcile_builtin(*call_args, **call_kwargs):
        phase_barrier("reconciliation")
        return original_reconciliation(*call_args, **call_kwargs)

    def atomic_write_text(path, value):
        if Path(path) == app.DATA / "secrets.json":
            phase_barrier("authentication")
        return original_atomic_write(path, value)

    app.execution_recovery.recover_startup = recover_startup
    app.recipe_store.migrate_recipe_directory = migrate_recipes
    app.builtin_recipe.reconcile_builtin_recipe = reconcile_builtin
    app.prepare_application_directories = prepare_directories
    settings_store.storage.atomic_write_text = atomic_write_text

    def manager_factory():
        manager = app.create_execution_manager(store=store, execute=execute)
        original_start = manager.start

        def start(*call_args, **call_kwargs):
            phase_barrier("manager_start")
            return original_start(*call_args, **call_kwargs)

        manager.start = start
        return manager

    def retention_target(stop):
        phase_barrier("retention")
        stop.wait()

    server_holder = []

    def server_factory(address, handler):
        selected = ReadyServer((address[0], args.port), handler)
        server_holder.append(selected)
        return selected

    outcome = app.serve_application(
        app.Handler,
        server_factory=server_factory,
        manager_factory=manager_factory,
        retention_target=retention_target,
        shutdown_timeout=args.shutdown_timeout,
    )
    print("SUMMARY " + json.dumps({
        "outcome": outcome,
        "runs": store.list(),
        "listener_closed": bool(server_holder and server_holder[0].closed),
        "lifecycle_threads": sorted(
            thread.name for thread in threading.enumerate()
            if thread.name in {
                "debbuilder-execution", "storage-maintenance", "signal-shutdown-coordinator",
            }
        ),
    }), flush=True)
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
