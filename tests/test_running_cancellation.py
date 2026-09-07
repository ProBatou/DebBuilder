import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import automation_service, build_pipeline, deb_inspector, debian_packaging, dependency_checker, upstream_artifact
from debbuilder.build_store import BuildStore
from debbuilder.execution_cancellation import CancellationControl, ExecutionCancelled
from debbuilder.execution_manager import ExecutionManager
from debbuilder.recipe_schema import validate_recipe_metadata
from debbuilder.workspace_cleanup import _require_unused_workspace


def source_recipe(name="cancel-build", *, commands=None):
    return {
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Demo <demo@example.test>",
            "description": "Cancellation test package",
        },
        "source": {"repository": f"owner/{name}"},
        "build": {
            "commands": commands or [],
            "output": {"mode": "source"},
            "inactivity_timeout": None,
            "maximum_runtime": 10,
        },
    }


def upstream_deb_recipe():
    return validate_recipe_metadata({
        "name": "upstream-cancel",
        "package": {"name": "upstream-cancel", "architecture": "amd64"},
        "source": {"repository": "owner/upstream-cancel", "tracking": "latest_release"},
        "artifact": {"mode": "upstream_deb", "architecture": "amd64", "name_pattern": "*.deb"},
    })


def upstream_archive_recipe():
    return validate_recipe_metadata({
        "name": "archive-cancel",
        "package": {
            "name": "archive-cancel", "architecture": "all",
            "maintainer": "Demo <demo@example.test>",
        },
        "source": {"repository": "owner/archive-cancel", "tracking": "latest_release"},
        "artifact": {
            "mode": "upstream_archive", "type": "archive", "architecture": "amd64",
            "asset_name": "archive.tar.gz", "selected_files": ["app"],
        },
        "build": {"commands": [], "source_changes": [], "output": {"mode": "source"}},
        "install": {
            "content": {"source": "configured_files"},
            "owner": {"user": "root", "group": "root"},
            "config_files": [{
                "source": "app", "destination": "/usr/bin/app", "policy": "replace",
                "mode": "0755", "owner": "root", "group": "root",
            }],
        },
    })


def upstream_release():
    return {
        "repository": "owner/upstream-cancel",
        "tag": "v1.0.0",
        "ref": "v1.0.0",
        "url": "https://example.test/release",
        "upstream_version": "1.0.0",
        "assets": [{"name": "upstream-cancel_1.0.0_amd64.deb", "url": "https://example.test/demo.deb"}],
    }


def available_dependencies(detected, manual, **_kwargs):
    return {
        "detected": detected,
        "manually_added": manual,
        "required": detected + manual,
        "available": detected + manual,
        "missing": [],
        "checks": [],
        "installation_attempted": False,
    }


class RunningCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop(timeout=5)

    def manager(self, execute):
        manager = ExecutionManager(self.store, execute=execute)
        self.managers.append(manager)
        return manager

    def process_tree_command(self, workspace: Path, *, ignore_term: bool) -> tuple[str, Path]:
        script = workspace / "run-tree.py"
        marker = workspace / "run-tree-pids"
        script.write_text(
            "import os,signal,subprocess,sys,time\n"
            "level=int(sys.argv[1]); marker=sys.argv[2]; ignore=sys.argv[3] == 'ignore'\n"
            "fd=os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
            "os.write(fd, (str(os.getpid()) + '\\n').encode()); os.close(fd)\n"
            "print('tree-ready', os.getpid(), flush=True)\n"
            "if ignore: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "if level: subprocess.Popen([sys.executable, __file__, str(level-1), marker, sys.argv[3]])\n"
            "while True: time.sleep(0.05)\n"
        )
        return " ".join((
            shlex.quote(sys.executable), shlex.quote(str(script)), "2", shlex.quote(str(marker)),
            "ignore" if ignore_term else "default",
        )), marker

    def assert_process_tree_gone(self, pids: list[int]) -> None:
        deadline = time.monotonic() + 1
        remaining = set(pids)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    remaining.remove(pid)
            if remaining:
                time.sleep(0.01)
        self.assertEqual(remaining, set(), f"processes still exist: {sorted(remaining)}")
        with self.assertRaises(ProcessLookupError):
            os.killpg(pids[0], 0)

    @staticmethod
    def acquire_source(_recipe, workspace, token=""):
        source = Path(workspace) / "source"
        (source / "package.json").write_text("{}")
        return {
            "repository": "owner/cancel-build",
            "ref": "v1",
            "tag": "v1",
            "upstream_version": "1.0",
            "debian_version": "1.0-1",
            "source_directory": str(source),
        }

    def test_active_cancel_before_source_acquisition_is_worker_finalized(self):
        callback_entered = threading.Event()
        release_callback = threading.Event()
        self.addCleanup(release_callback.set)
        acquired = []
        results = []

        def acquire(*args, **kwargs):
            acquired.append((args, kwargs))
            return self.acquire_source(*args, **kwargs)

        def execute(run_id, *, cancellation_control, **kwargs):
            callback_entered.set()
            release_callback.wait(3)
            results.append(build_pipeline.execute_pipeline_run(
                run_id, cancellation_control=cancellation_control,
                acquire=acquire, dependency_check=available_dependencies, **kwargs,
            ))

        run = build_pipeline.create_pipeline_run(source_recipe(), store=self.store, dry_run=False)
        manager = self.manager(execute)
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(callback_entered.wait(2))

        requested = manager.cancel(run["id"])
        release_callback.set()
        manager.stop(timeout=4)

        persisted = self.store.load(run["id"])
        self.assertEqual(requested["outcome"], "active_cancel_requested")
        self.assertEqual(acquired, [])
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(results[0]["returncode"], None)
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation"]["stage"], "source")
        self.assertIsNone(persisted["error"])
        self.assertTrue(all(step["status"] == "pending" for step in persisted["steps"]))

    def test_cancel_after_source_acquisition_marks_source_step_cancelled(self):
        acquisition_complete = threading.Event()
        release_acquisition = threading.Event()
        self.addCleanup(release_acquisition.set)
        notifications = []

        def acquire(*args, **kwargs):
            result = self.acquire_source(*args, **kwargs)
            acquisition_complete.set()
            release_acquisition.wait(3)
            return result

        def execute(run_id, *, cancellation_control, **kwargs):
            return build_pipeline.execute_pipeline_run(
                run_id, cancellation_control=cancellation_control,
                acquire=acquire, dependency_check=available_dependencies,
                lifecycle_callback=lambda event, **_payload: notifications.append(event), **kwargs,
            )

        run = build_pipeline.create_pipeline_run(source_recipe(), store=self.store, dry_run=False)
        manager = self.manager(execute)
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(acquisition_complete.wait(2))
        manager.cancel(run["id"])
        release_acquisition.set()
        manager.stop(timeout=4)

        persisted = self.store.load(run["id"])
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["steps"][0]["status"], "cancelled")
        self.assertTrue(all(step["status"] == "pending" for step in persisted["steps"][1:]))
        self.assertEqual(persisted["cancellation"]["phase"], "pipeline")
        self.assertEqual(persisted["cancellation"]["stage"], "source")
        self.assertNotIn("build_failed", notifications)

    def test_cancel_during_second_build_command_preserves_partial_results(self):
        second_ready = threading.Event()
        original_log = self.store.append_log_line
        third_marker = Path(self.temporary.name) / "third-command-ran"
        commands = [
            f"{shlex.quote(sys.executable)} -c 'print(\"first-complete\")'",
            f"{shlex.quote(sys.executable)} -c 'import signal,time; signal.signal(signal.SIGTERM, lambda *_: (print(\"second-stopping\", flush=True), exit(0))); print(\"second-ready\", flush=True); time.sleep(10)'",
            f"{shlex.quote(sys.executable)} -c 'import pathlib; pathlib.Path({str(third_marker)!r}).write_text(\"ran\")'",
        ]

        def observe_log(run_id, message, **kwargs):
            original_log(run_id, message, **kwargs)
            if "second-ready" in message:
                second_ready.set()

        def execute(run_id, *, cancellation_control, **kwargs):
            return build_pipeline.execute_pipeline_run(
                run_id, cancellation_control=cancellation_control,
                acquire=self.acquire_source, dependency_check=available_dependencies, **kwargs,
            )

        run = build_pipeline.create_pipeline_run(source_recipe(commands=commands), store=self.store, dry_run=False)
        manager = self.manager(execute)
        with mock.patch.object(self.store, "append_log_line", side_effect=observe_log):
            manager.start()
            manager.submit(run["id"])
            self.assertTrue(second_ready.wait(4))
            self.assertEqual(manager.cancel(run["id"])["outcome"], "active_cancel_requested")
            manager.stop(timeout=5)

        persisted = self.store.load(run["id"])
        command_files = sorted((Path(run["workspace"]) / "logs/commands").glob("*.json"))
        commands_recorded = [json.loads(path.read_text()) for path in command_files]
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["steps"][4]["status"], "cancelled")
        self.assertTrue(all(step["status"] == "success" for step in persisted["steps"][:4]))
        self.assertEqual([row["status"] for row in commands_recorded], ["success", "cancelled"])
        self.assertIn("second-ready", commands_recorded[1]["stdout"])
        self.assertIn("second-stopping", commands_recorded[1]["stdout"])
        self.assertFalse(commands_recorded[1]["killed"])
        self.assertIsNone(commands_recorded[1]["termination_error"])
        self.assertFalse(third_marker.exists())

    def test_cancelled_run_force_kills_parent_child_grandchild_without_orphans(self):
        workspace = Path(self.temporary.name)
        command, marker = self.process_tree_command(workspace, ignore_term=True)
        tree_ready = threading.Event()
        original_log = self.store.append_log_line

        def observe_log(run_id, message, **kwargs):
            original_log(run_id, message, **kwargs)
            if marker.exists() and len(marker.read_text().splitlines()) == 3:
                tree_ready.set()

        def execute(run_id, *, cancellation_control, **kwargs):
            return build_pipeline.execute_pipeline_run(
                run_id, cancellation_control=cancellation_control,
                acquire=self.acquire_source, dependency_check=available_dependencies, **kwargs,
            )

        run = build_pipeline.create_pipeline_run(source_recipe(name="cancel-tree", commands=[command]), store=self.store, dry_run=False)
        manager = self.manager(execute)
        with mock.patch.object(self.store, "append_log_line", side_effect=observe_log):
            manager.start()
            manager.submit(run["id"])
            self.assertTrue(tree_ready.wait(4))
            self.assertEqual(manager.cancel(run["id"])["outcome"], "active_cancel_requested")
            manager.stop(timeout=5)

        pids = [int(row) for row in marker.read_text().splitlines()]
        self.assertEqual(len(pids), 3)
        self.assert_process_tree_gone(pids)
        _require_unused_workspace(Path(run["workspace"]))
        persisted = self.store.load(run["id"])
        command_record = json.loads(next((Path(run["workspace"]) / "logs/commands").glob("*.json")).read_text())
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(command_record["status"], "cancelled")
        self.assertTrue(command_record["killed"])
        self.assertIsNone(command_record["termination_error"])
        self.assertIn("tree-ready", command_record["stdout"])

    def test_cancel_before_first_build_command_starts_no_process(self):
        control = CancellationControl()
        runner = mock.Mock()
        original_execute = build_pipeline.build_executor.execute_build

        def request_at_build(*args, **kwargs):
            control.request_cancel()
            return original_execute(*args, runner=runner, **kwargs)

        run = build_pipeline.create_pipeline_run(
            source_recipe(commands=["should-never-start"]), store=self.store, dry_run=False,
        )
        with mock.patch.object(build_pipeline.build_executor, "execute_build", side_effect=request_at_build):
            result = build_pipeline.execute_pipeline_run(
                run["id"], store=self.store, cancellation_control=control,
                acquire=self.acquire_source, dependency_check=available_dependencies,
            )

        persisted = self.store.load(run["id"])
        runner.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(persisted["steps"][4]["status"], "cancelled")
        self.assertTrue(all(step["status"] == "success" for step in persisted["steps"][:4]))

    def test_upstream_archive_honors_pipeline_cancellation(self):
        control = CancellationControl()
        control.request_cancel()
        acquire = mock.Mock()
        run = build_pipeline.create_pipeline_run(upstream_archive_recipe(), store=self.store, dry_run=False)

        result = build_pipeline.execute_pipeline_run(
            run["id"], store=self.store, cancellation_control=control, acquire=acquire,
        )

        acquire.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.load(run["id"])["cancellation"]["stage"], "source")

    def _run_upstream_boundary_race(self, *, terminal_wins: bool, dry_run: bool = False):
        control = CancellationControl()
        boundary_reached = threading.Event()
        release_boundary = threading.Event()
        self.addCleanup(release_boundary.set)
        original_claim = control.claim_terminal
        registrations = []
        original_save = self.store.save

        def save(run):
            artifact_step = next(step for step in run["steps"] if step["name"] == "artifact")
            if run.get("artifact") and artifact_step["status"] == "success" and not registrations:
                registrations.append(run["artifact"]["path"])
            original_save(run)

        def claim_terminal():
            if terminal_wins:
                claimed = original_claim()
                boundary_reached.set()
                release_boundary.wait(3)
                return claimed
            boundary_reached.set()
            release_boundary.wait(3)
            return original_claim()

        def acquire(_recipe, workspace, token=""):
            artifact = Path(workspace) / "artifacts/upstream-cancel_1.0.0_amd64.deb"
            artifact.write_bytes(b"deb")
            return {
                "path": str(artifact), "name": artifact.name, "size": 3,
                "sha256": "a" * 64, "source": "upstream_release",
                "release_asset": upstream_release()["assets"][0],
                "inspection": {"ok": True, "package": "upstream-cancel", "version": "1.0.0", "architecture": "amd64"},
            }

        run = build_pipeline.create_pipeline_run(upstream_deb_recipe(), store=self.store, dry_run=dry_run)
        results = []
        errors = []

        def execute():
            try:
                results.append(build_pipeline.execute_pipeline_run(
                    run["id"], store=self.store, cancellation_control=control,
                    upstream_acquirer=acquire,
                ))
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.store, "save", side_effect=save), \
             mock.patch.object(control, "claim_terminal", side_effect=claim_terminal), \
             mock.patch("debbuilder.upstream_artifact.resolve_release", return_value=upstream_release()):
            thread = threading.Thread(target=execute)
            thread.start()
            self.assertTrue(boundary_reached.wait(3))
            cancellation = control.request_cancel()
            release_boundary.set()
            thread.join(4)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return run, results[0], cancellation, registrations

    def test_cancellation_wins_at_artifact_registration_boundary(self):
        run, result, cancellation, registrations = self._run_upstream_boundary_race(terminal_wins=False)

        self.assertTrue(cancellation["accepted"])
        self.assertEqual(registrations, [])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(self.store.load(run["id"])["artifact"])

    def test_terminal_wins_at_artifact_registration_boundary(self):
        run, result, cancellation, registrations = self._run_upstream_boundary_race(terminal_wins=True)

        self.assertFalse(cancellation["accepted"])
        self.assertEqual(len(registrations), 1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.store.load(run["id"])["status"], "success")

    def test_dry_run_prepared_boundary_honors_cancellation_gate(self):
        run, result, cancellation, registrations = self._run_upstream_boundary_race(
            terminal_wins=False, dry_run=True,
        )

        self.assertTrue(cancellation["accepted"])
        self.assertEqual(registrations, [])
        self.assertEqual(result["status"], "cancelled")
        self.assertNotEqual(self.store.load(run["id"])["status"], "prepared")

        terminal_run, terminal_result, rejected, _registrations = self._run_upstream_boundary_race(
            terminal_wins=True, dry_run=True,
        )
        self.assertFalse(rejected["accepted"])
        self.assertEqual(terminal_result["status"], "prepared")
        self.assertEqual(self.store.load(terminal_run["id"])["status"], "prepared")

    def test_source_build_cancellation_wins_after_local_artifact_before_registration(self):
        control = CancellationControl()
        boundary_reached = threading.Event()
        release_boundary = threading.Event()
        self.addCleanup(release_boundary.set)
        original_claim = control.claim_terminal

        def claim_terminal():
            boundary_reached.set()
            release_boundary.wait(3)
            return original_claim()

        def acquire(_recipe, workspace, token=""):
            source = Path(workspace) / "source"
            (source / "index.html").write_text("ok")
            return {
                "repository": "owner/cancel-build", "ref": "v1", "tag": "v1",
                "upstream_version": "1.0", "debian_version": "1.0-1",
                "source_directory": str(source),
            }

        detection = {
            "project_type": "static", "display_name": "Static site",
            "detected_files": ["index.html"], "build_dependencies": [],
            "system_build_dependencies": [], "build_tools": [],
            "tool_version_requirements": {}, "proposed_commands": [], "warnings": [],
        }

        def local_artifact(_recipe, _staging, workspace, **_kwargs):
            path = Path(workspace) / "artifacts/cancel-build_1.0-1_all.deb"
            path.write_bytes(b"local artifact")
            return {
                "path": str(path), "name": path.name, "size": path.stat().st_size,
                "sha256": "b" * 64, "build_command": {"status": "success"},
                "inspection": {"ok": True, "package": "cancel-build", "version": "1.0-1", "architecture": "all"},
            }

        run = build_pipeline.create_pipeline_run(source_recipe(), store=self.store, dry_run=False)
        results = []
        with mock.patch.object(control, "claim_terminal", side_effect=claim_terminal), \
             mock.patch.object(build_pipeline.debian_packaging, "build_deb", side_effect=local_artifact):
            thread = threading.Thread(target=lambda: results.append(build_pipeline.execute_pipeline_run(
                run["id"], store=self.store, cancellation_control=control,
                acquire=acquire, detector=lambda *_args, **_kwargs: detection,
                dependency_check=available_dependencies,
            )))
            thread.start()
            self.assertTrue(boundary_reached.wait(3))
            self.assertTrue(control.request_cancel()["accepted"])
            release_boundary.set()
            thread.join(4)

        self.assertFalse(thread.is_alive())
        persisted = self.store.load(run["id"])
        self.assertTrue((Path(run["workspace"]) / "artifacts/cancel-build_1.0-1_all.deb").is_file())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(persisted["steps"][8]["status"], "success")
        self.assertEqual(persisted["steps"][9]["status"], "cancelled")
        self.assertIsNone(persisted["artifact"])

    def test_dpkg_and_inspection_cancellation_are_not_domain_failures(self):
        cancelled_result = {
            "status": "cancelled", "cancellation_requested": True, "cancelled": True,
            "cancellation": {"stage": "package"}, "termination_error": None,
        }
        with self.assertRaises(ExecutionCancelled):
            dependency_checker.check_dependencies(
                ["demo"], [], workspace=self.temporary.name,
                runner=lambda *_args, **_kwargs: cancelled_result,
            )

        deb = Path(self.temporary.name) / "input.deb"
        deb.write_bytes(b"deb")
        with self.assertRaises(ExecutionCancelled):
            deb_inspector.inspect_deb(
                deb, workspace=self.temporary.name,
                runner=lambda *_args, **_kwargs: cancelled_result,
            )

        staging = {"version": "1.0", "staging_directory": str(Path(self.temporary.name) / "staging")}
        inspector = mock.Mock()

        def built(_command, *, workspace, **_kwargs):
            target = Path(workspace) / "artifacts/cancel-build_1.0_all.deb"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"deb")
            return {"status": "success", "stderr": ""}

        with mock.patch.object(debian_packaging, "validate_staging"), self.assertRaises(ExecutionCancelled):
            debian_packaging.build_deb(
                validate_recipe_metadata(source_recipe()), staging, self.temporary.name,
                runner=built, inspector=inspector,
                before_inspection=lambda: (_ for _ in ()).throw(ExecutionCancelled({"stage": "artifact"})),
            )
        inspector.assert_not_called()

        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(b"deb")
            return {"size": 3, "sha256": "a" * 64}

        with self.assertRaises(ExecutionCancelled):
            upstream_artifact.acquire(
                upstream_deb_recipe(), self.temporary.name,
                release_resolver=lambda *_args, **_kwargs: upstream_release(),
                downloader=downloader,
                inspector=lambda *_args, **_kwargs: (_ for _ in ()).throw(ExecutionCancelled({"stage": "artifact"})),
            )

    def test_termination_verification_failure_finalizes_run_as_failed(self):
        control = CancellationControl()
        control.request_cancel()
        run = build_pipeline.create_pipeline_run(source_recipe(), store=self.store, dry_run=False)

        with mock.patch.object(
            build_pipeline,
            "_run_pipeline_locked",
            side_effect=ExecutionCancelled(
                {"stage": "build", "requested_at": control.request["requested_at"]},
                command_result={"termination_error": "process group remained alive"},
            ),
        ):
            result = build_pipeline.execute_pipeline_run(
                run["id"], store=self.store, cancellation_control=control,
            )

        persisted = self.store.load(run["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(persisted["error"]["code"], "execution_cancellation_termination_failed")
        self.assertEqual(persisted["cancellation"]["reason"], "user_requested")

    def test_cancelled_result_skips_automation_and_completion_notification(self):
        automated = mock.Mock()
        notified = mock.Mock()
        result = {"run_id": "cancelled-run", "status": "cancelled"}

        completed = automation_service.complete_with_automation(
            result, dry_run=False, automate=automated, notify_completion=notified,
        )

        self.assertIs(completed, result)
        automated.assert_not_called()
        notified.assert_not_called()


if __name__ == "__main__":
    unittest.main()
