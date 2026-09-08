#!/usr/bin/env python3
"""Launch isolated DebBuilder scenarios for manual DEV review."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import showcase

WORKER = Path(__file__).with_name("cancellation_worker.py")


@dataclass
class ScenarioState:
    manager: object | None = None
    after_start: object | None = None
    cleanup: object | None = None
    canonical_recipe: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    seed: object
    setup: object | None = None
    allow_run: bool = False


@dataclass
class IsolatedRuntime:
    root: Path
    data_dir: Path
    repository_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def fixture_recipe(name: str, *, commands: list[str] | None = None) -> dict:
    return {
        "schema_version": 2, "name": name, "active": True,
        "package": {"name": name, "version_revision": "1", "architecture": "all", "maintainer": "DEV Review <dev@example.test>", "description": "Isolated Behavior Lab fixture"},
        "source": {"provider": "github", "repository": f"example/{name}", "tracking": "latest_release", "version": {"source": "tag"}},
        "artifact": {"mode": "source_build", "type": "deb", "architecture": "all"},
        "build": {"commands": commands or [], "working_directory": ".", "environment": {}, "extra_dependencies": [], "source_changes": [], "output": {"mode": "source"}, "inactivity_timeout": None, "maximum_runtime": 600},
        "install": {"destination": f"/opt/{name}", "content": {"source": "build_output", "path": ""}, "owner": {"user": "root", "group": "root"}, "config_files": [], "directories": []},
        "service": {"enabled": False},
    }


def seed_fixture(data_dir: Path, repository_root: Path, recipe: dict) -> None:
    (data_dir / "workflows").mkdir(parents=True)
    repository_root.mkdir(parents=True)
    (data_dir / "workflows" / f"{recipe['name']}.json").write_text(json.dumps(recipe, indent=2) + "\n")
    (data_dir / "repo-current-packages-inventory.json").write_text("[]\n")
    expiry = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
    release = {recipe["source"]["repository"]: {"expires_at": expiry, "release": {"tag": "v1.0.0", "name": "Behavior Lab 1.0.0", "url": "https://example.invalid/behavior-lab", "assets": []}}}
    (data_dir / "github-release-cache.json").write_text(json.dumps(release, indent=2) + "\n")


def seed_cancellation(data_dir: Path, repository_root: Path) -> None:
    seed_fixture(data_dir, repository_root, fixture_recipe("cancellation-running"))


def seed_queued(data_dir: Path, repository_root: Path) -> None:
    seed_fixture(data_dir, repository_root, fixture_recipe("queued-cancellable"))


def seed_failure(data_dir: Path, repository_root: Path) -> None:
    script = "import sys; print('behavior-lab stdout'); print('behavior-lab stderr', file=sys.stderr); raise SystemExit(7)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    seed_fixture(data_dir, repository_root, fixture_recipe("build-failure", commands=[command]))


def _seed_interrupted(data_dir: Path, *, blocked: bool) -> None:
    from debbuilder.build_store import BuildStore
    from debbuilder.command_identity import IDENTITY_SCHEMA_VERSION, persist_identity

    store = BuildStore(data_dir / "builds")
    run = store.create(fixture_recipe("recovery-run"), recipe_id="recovery-run", mode="build", run_id="behavior-lab-recovery-run")
    run.update({"status": "running", "started_at": run["created_at"]})
    run["steps"][0].update({"status": "running", "started_at": run["created_at"]})
    store.save(run)
    if not blocked:
        identity = {
            "schema_version": IDENTITY_SCHEMA_VERSION, "backend": "process_group", "containment_state": "active",
            "pid": 999999, "pgid": 999999, "start_time_ticks": 1,
            "boot_id": "00000000-0000-0000-0000-000000000000", "run_id": run["id"], "command_id": "behavior-lab-old-boot",
        }
        with store.locked_run(run["id"]) as fd:
            persist_identity(fd, identity)


def seed_recovery(data_dir: Path, repository_root: Path) -> None:
    seed_fixture(data_dir, repository_root, fixture_recipe("recovery"))
    _seed_interrupted(data_dir, blocked=False)


def seed_recovery_blocked(data_dir: Path, repository_root: Path) -> None:
    seed_fixture(data_dir, repository_root, fixture_recipe("recovery-blocked"))
    _seed_interrupted(data_dir, blocked=True)


def seed_graceful_shutdown(data_dir: Path, repository_root: Path) -> None:
    seed_fixture(data_dir, repository_root, fixture_recipe("graceful-shutdown"))


def fixture_acquire(*, running: bool):
    from debbuilder import command_runner
    from debbuilder.build_models import utc_now

    def acquire(_recipe, workspace, token="", cancellation_event=None, on_cancel=None):
        root = Path(workspace)
        source = root / "source"
        source.mkdir(exist_ok=True)
        (source / "requirements.txt").write_text("\n")
        if running:
            with (root / "logs/pipeline.log").open("a") as log:
                log.write(f"{utc_now()} INFO behavior fixture process tree started\n")
            shutil.copy2(WORKER, source / WORKER.name)
            command = shlex.join([sys.executable, str(source / WORKER.name), "2"])

            def retain_output(item: dict) -> None:
                text = str(item.get("text") or "").rstrip()
                if text:
                    with (root / "logs/pipeline.log").open("a") as log:
                        for line in text.splitlines():
                            log.write(f"{utc_now()} INFO behavior fixture {item.get('stream')}: {line}\n")

            command_runner.run_command(command, workspace=root, working_directory="source", inactivity_timeout=None, maximum_runtime=600, cancellation_event=cancellation_event, on_cancel=on_cancel, on_output=retain_output)
        else:
            (source / "README.txt").write_text("Behavior Lab source fixture.\n")
        return {"repository": "example/behavior-lab", "strategy": "latest_release", "ref": "v1.0.0", "tag": "v1.0.0", "release_name": "Behavior Lab 1.0.0", "release_url": "https://example.invalid/behavior-lab", "archive_url": "", "upstream_version": "1.0.0", "debian_version": "1.0.0-1", "source_directory": str(source)}
    return acquire


def pipeline_setup(*, running: bool):
    def setup(app, _runtime):
        from debbuilder import build_pipeline
        acquire = fixture_acquire(running=running)

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            def controlled(recipe, workspace, token=""):
                return acquire(recipe, workspace, token=token, cancellation_event=cancellation_control.event, on_cancel=lambda: {**(cancellation_control.request or {}), "phase": "pipeline", "stage": "source"})
            try:
                return build_pipeline.execute_pipeline_run(run_id, store=store, expected_initial_status=expected_initial_status, cancellation_control=cancellation_control, acquire=controlled, lifecycle_callback=app.notify_lifecycle)
            finally:
                app.cleanup_workspaces()

        app.execute_queued_recipe_run = execute
        return ScenarioState()
    return setup


def queued_setup(_app, runtime):
    from debbuilder.build_store import BuildStore
    from debbuilder.execution_manager import ExecutionManager
    released, started = threading.Event(), threading.Event()
    store = BuildStore(runtime.data_dir / "builds")
    blocker_id = "behavior-lab-queue-blocker"

    def execute(run_id, **kwargs):
        if run_id == blocker_id:
            started.set()
            cancellation = kwargs["cancellation_control"].event
            while not released.wait(.05):
                if cancellation.is_set():
                    return None

    manager = ExecutionManager(store, execute=execute)

    def after_start(_manager=None):
        blocker = store.create(fixture_recipe("queue-blocker"), recipe_id="queue-blocker", mode="build", run_id=blocker_id)
        manager.submit(blocker["id"])
        if not started.wait(5):
            raise RuntimeError("Behavior Lab queue blocker did not start")

    return ScenarioState(manager=manager, after_start=after_start, cleanup=released.set)


def graceful_shutdown_setup(app, runtime):
    state = pipeline_setup(running=True)(app, runtime)

    def after_start(manager):
        from debbuilder.build_store import BuildStore
        store = BuildStore(runtime.data_dir / "builds")
        active = store.create(fixture_recipe("graceful-shutdown"), recipe_id="graceful-shutdown", mode="build", run_id="behavior-lab-shutdown-active")
        queued = store.create(fixture_recipe("graceful-shutdown"), recipe_id="graceful-shutdown", mode="build", run_id="behavior-lab-shutdown-queued")
        manager.submit(active["id"])
        manager.submit(queued["id"])

    state.after_start = after_start
    return state


SCENARIOS = {
    "showcase": Scenario("showcase", "Deterministic static Runs, packages, and Recipes used by the UI showcase.", showcase.seed),
    "cancellation-running": Scenario("cancellation-running", "Running cancellable local process-tree fixture with live logs.", seed_cancellation, pipeline_setup(running=True), True),
    "queued-cancellable": Scenario("queued-cancellable", "Queued Run held behind a local blocker for real queue cancellation.", seed_queued, queued_setup, True),
    "build-failure": Scenario("build-failure", "Harmless local command that fails with captured stdout and stderr.", seed_failure, pipeline_setup(running=False), True),
    "prepared-test": Scenario("prepared-test", "Static canonical prepared Test and staging-preview fixture.", showcase.seed),
    "graceful-shutdown": Scenario("graceful-shutdown", "Active and queued local Runs; press Ctrl-C for real graceful shutdown.", seed_graceful_shutdown, graceful_shutdown_setup),
    "recovery": Scenario("recovery", "Startup recovery terminalizes an old-boot interrupted Run without replay.", seed_recovery),
    "recovery-blocked": Scenario("recovery-blocked", "Unresolved interrupted Run keeps admission fail-closed while history remains readable.", seed_recovery_blocked, allow_run=True),
}


def scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Behavior Lab scenario {name!r}; choose one of: {', '.join(SCENARIOS)}") from exc


def create_isolated_runtime(selected: Scenario) -> IsolatedRuntime:
    root = Path(tempfile.mkdtemp(prefix="debbuilder-behavior-lab-"))
    runtime = IsolatedRuntime(root, root / "data", root / "repository")
    try:
        selected.seed(runtime.data_dir, runtime.repository_root)
    except Exception:
        runtime.cleanup()
        raise
    return runtime


def configure_environment(runtime: IsolatedRuntime, *, host: str, port: int) -> None:
    os.environ.update({"DEBBUILDER_DATA_DIR": str(runtime.data_dir), "DEBBUILDER_REPO_ROOT": str(runtime.repository_root), "DEBBUILDER_REPO_URL": "https://repo.example.invalid/behavior-lab", "DEBBUILDER_HOST": host, "DEBBUILDER_PORT": str(port), "DEBBUILDER_AUTH_MODE": "none", "DEBBUILDER_GITHUB_TOKEN": "", "GITHUB_TOKEN": "", "DEBBUILDER_NTFY_TOKEN": ""})


def _canonical_json(value: object) -> str:
    """Stable, in-memory comparison form for a scenario's seeded fixture."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _behavior_lab_error(handler, scenario_name: str) -> None:
    """Return the deliberately small, non-sensitive mutation-boundary error."""
    from debbuilder import app

    app.json_response(handler, {"error": {
        "code": "behavior_lab_action_blocked",
        "message": "This operation is not available in the Behavior Lab scenario",
        "details": {"scenario": scenario_name},
    }}, 403)


def serve(selected: Scenario, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    runtime = create_isolated_runtime(selected)
    http_server = state = None
    try:
        # This is deliberately captured before application startup and before
        # any HTTP request can modify the disposable workflow store.
        fixture_path = runtime.data_dir / "workflows" / f"{selected.name}.json"
        if selected.allow_run:
            # validate_recipe_metadata is the established canonicalizer used
            # by Recipe reads to add schema defaults. Apply it to the seeded
            # document now, not to any later mutable on-disk document.
            from debbuilder.recipe_schema import validate_recipe_metadata

            canonical_recipe = _canonical_json(validate_recipe_metadata(json.loads(fixture_path.read_text())))
        else:
            canonical_recipe = None
        configure_environment(runtime, host=host, port=port)
        from debbuilder import app
        from server import Handler
        state = selected.setup(app, runtime) if selected.setup else ScenarioState()
        state.canonical_recipe = canonical_recipe

        class BehaviorLabHandler(Handler):
            def _canonical_run(self, data: object) -> bool:
                if not selected.allow_run or not state.canonical_recipe or not isinstance(data, dict):
                    return False
                if set(data) - {"workflow", "dry_run"} or not isinstance(data.get("workflow"), dict):
                    return False
                if "dry_run" in data and not isinstance(data["dry_run"], bool):
                    return False
                return _canonical_json(data["workflow"]) == state.canonical_recipe

            def _owned_run(self, run_id: str) -> bool:
                # Runs are created only through the canonical fixture above.
                # Keep the ownership check explicit in case a client supplies
                # a guessed ID to a mutating Run route.
                from debbuilder.build_store import BuildStore

                try:
                    record = BuildStore(runtime.data_dir / "builds").load(run_id)
                except (FileNotFoundError, ValueError):
                    return False
                if not record:
                    return False
                return (
                    record.get("recipe_id") == selected.name
                    and Path(str(record.get("workspace") or "")).resolve().is_relative_to(
                        (runtime.data_dir / "builds").resolve()
                    )
                )

            def _post(self, data: dict):
                path = urlparse(self.path).path
                if path == "/api/run":
                    if not self._canonical_run(data):
                        _behavior_lab_error(self, selected.name)
                        return
                    super()._post(data)
                    return
                if path.startswith("/api/executions/") and path.endswith("/cancel"):
                    run_id = path[len("/api/executions/"):-len("/cancel")].strip("/")
                    if not isinstance(data, dict) or data or not self._owned_run(run_id):
                        _behavior_lab_error(self, selected.name)
                        return
                    super()._post(data)
                    return
                # All other POST routes are durable mutations, external
                # actions, or execution-input changes.  The Lab is default
                # deny even though its data root is temporary.
                _behavior_lab_error(self, selected.name)

            def do_DELETE(self):
                if not self._authorized():
                    return
                _behavior_lab_error(self, selected.name)

        class BehaviorLabServer(app.ThreadingHTTPServer):
            announced = False

            def serve_forever(self, *args, **kwargs):
                if not self.announced:
                    self.announced = True
                    if state.after_start:
                        state.after_start(self.execution_manager)
                    print("Behavior Lab", flush=True)
                    print(f"Scenario: {selected.name}", flush=True)
                    print(f"Description: {selected.description}", flush=True)
                    print(f"Bind: {host}:{self.server_port}", flush=True)
                    print(f"URL: http://127.0.0.1:{self.server_port}", flush=True)
                    print(f"Open from this machine: http://127.0.0.1:{self.server_port}", flush=True)
                    if host == "0.0.0.0":
                        print("From another machine: use the Repo VM hostname or IP with this port", flush=True)
                    print(f"Runtime: {runtime.root}", flush=True)
                    if selected.name == "graceful-shutdown":
                        print("Action: press Ctrl-C to exercise production graceful shutdown", flush=True)
                return super().serve_forever(*args, **kwargs)

        def server_factory(address, handler):
            nonlocal http_server
            http_server = BehaviorLabServer(address, handler)
            app.RUNTIME = replace(app.RUNTIME, port=http_server.server_port)
            return http_server

        def manager_factory():
            return state.manager or app.create_execution_manager()

        app.serve_application(BehaviorLabHandler, server_factory=server_factory, manager_factory=manager_factory, retention_target=None)
        if selected.name == "graceful-shutdown":
            from debbuilder.build_store import BuildStore
            runs = BuildStore(runtime.data_dir / "builds").list()
            summary = [{"id": row["id"], "status": row["status"], "reason": (row.get("cancellation") or {}).get("reason")} for row in runs]
            print("Graceful shutdown Runs: " + json.dumps(summary, sort_keys=True), flush=True)
    finally:
        if state and state.cleanup:
            state.cleanup()
        runtime.cleanup()


def tcp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def preflight_bind(host: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        message = exc.strerror or str(exc)
        raise OSError(f"Behavior Lab: cannot bind {host}:{port}: {message}") from exc


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="showcase", choices=sorted(SCENARIOS))
    parser.add_argument("--port", type=tcp_port, default=8765, help="TCP port (1-65535; default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--list-scenarios", action="store_true")
    args = parser.parse_args(argv)
    if args.list_scenarios:
        for item in SCENARIOS.values():
            print(f"{item.name}: {item.description}")
        return
    try:
        preflight_bind(args.host, args.port)
        serve(scenario(args.scenario), host=args.host, port=args.port)
    except OSError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
