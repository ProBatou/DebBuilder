import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import build_pipeline, execution_service, source_acquisition, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.settings_store import default_settings, validate_settings


def recipe():
    return {"name": "demo", "package": {"name": "demo", "maintainer": "Demo <demo@example.test>", "description": "Demo"}, "source": {"repository": "owner/demo"}}


class WorkspaceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = BuildStore(self.base / "builds")

    def make_run(self, run_id="run-one", status="success", mode="build"):
        run = self.store.create(recipe(), mode=mode, run_id=run_id)
        root = Path(run["workspace"])
        for name in ("source", "staging", "downloads"):
            (root / name).mkdir(exist_ok=True)
            (root / name / "large-data").write_text("disposable")
        (root / "source.tar.gz").write_bytes(b"archive")
        artifact = root / "artifacts/demo.deb"
        artifact.write_bytes(b"final deb")
        run.update({"status": status, "artifact": {"path": str(artifact)}, "finished_at": "2026-09-05T12:00:00+00:00"})
        self.store.save(run)
        self.store.append_log_line(run_id, "persistent log")
        self.store.save_manifest(run_id, "manifests/staging-files.json", ["demo"])
        return run, root

    def test_automatic_cleanup_preserves_history_metadata_logs_manifests_and_artifact(self):
        run, root = self.make_run()
        before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file() and path.parts[-2] in {"artifacts", "manifests", "logs"}}
        metadata = (root / "run.json").read_bytes()
        snapshot = (root / "recipe.json").read_bytes()
        result = workspace_cleanup.apply_retention(self.store)
        self.assertEqual([row["id"] for row in result["cleaned"]], [run["id"]])
        for name in workspace_cleanup.DISPOSABLE_DIRECTORIES + workspace_cleanup.DISPOSABLE_FILES:
            self.assertFalse((root / name).exists())
        self.assertEqual((root / "run.json").read_bytes(), metadata)
        self.assertEqual((root / "recipe.json").read_bytes(), snapshot)
        for name, content in before.items():
            self.assertEqual((root / name).read_bytes(), content)
        self.assertIsNotNone(execution_service.get_execution(self.store, run["id"]))
        self.assertIn("persistent log", execution_service.get_log(self.store, run["id"], verbosity="raw")["text"])
        self.assertEqual(workspace_cleanup.apply_retention(self.store)["cleaned"], [])

    def test_default_retains_only_five_recent_failed_workspaces_across_restarts(self):
        roots = []
        for index in range(7):
            run, root = self.make_run(f"failed-{index}", status="failed")
            run["finished_at"] = f"2026-09-0{index + 1}T12:00:00+00:00"
            self.store.save(run)
            roots.append(root)
        result = workspace_cleanup.apply_retention(BuildStore(self.store.root))
        self.assertEqual(set(result["retained"]), {f"failed-{index}" for index in range(2, 7)})
        for index, root in enumerate(roots):
            self.assertEqual((root / "source").exists(), index >= 2)
            self.assertTrue((root / "run.json").exists())
        self.assertEqual(workspace_cleanup.apply_retention(BuildStore(self.store.root))["cleaned"], [])

    def test_active_build_pending_validation_publication_and_steps_are_never_cleaned_or_deleted(self):
        for phase in ("pending", "running", "validation", "publication", "step"):
            with self.subTest(phase=phase):
                run, root = self.make_run(phase)
                if phase in {"pending", "running"}:
                    run["status"] = phase
                elif phase == "step":
                    run["steps"][4]["status"] = "running"
                else:
                    run[f"{phase}s"] = [{"status": "running"}]
                self.store.save(run)
                with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                    execution_service.delete_log(self.store, run["id"])
                self.assertTrue((root / "source/large-data").exists())
                self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())
        result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
        self.assertEqual(result["cleaned"], [])
        self.assertEqual(execution_service.delete_logs(self.store, all_runs=True, dry_run=True)["count"], 0)

    def test_dry_runs_clean_prepared_and_retain_recent_failures(self):
        _run, prepared = self.make_run("prepared", status="prepared", mode="dry_run")
        _run, failed = self.make_run("failed", status="failed", mode="dry_run")
        workspace_cleanup.apply_retention(self.store)
        self.assertFalse((prepared / "source").exists())
        self.assertTrue((failed / "source").exists())

    def test_zero_retention_reclaims_failed_validation_publication_and_cancelled_runs(self):
        for phase in ("validation", "publication", "cancelled"):
            run, root = self.make_run(phase)
            if phase == "cancelled":
                run["status"] = "cancelled"
            else:
                run[f"{phase}s"] = [{"status": "failed", "finished_at": "2026-09-05T12:01:00+00:00"}]
                if phase == "publication":
                    run["validations"] = [{"status": "success"}]
            self.store.save(run)
        self.assertEqual(len(workspace_cleanup.apply_retention(self.store)["retained"]), 3)
        result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
        self.assertEqual(len(result["cleaned"]), 3)
        self.assertEqual(len(execution_service.list_executions(self.store, lambda run: "demo")), 3)

    def test_missing_runs_keep_existing_validation_and_publication_errors(self):
        from debbuilder import artifact_publication, artifact_validation
        with self.assertRaises(artifact_validation.ValidationError) as validation:
            artifact_validation.validate_artifact("missing", store=self.store)
        self.assertEqual(validation.exception.code, "build_run_not_found")
        with self.assertRaises(artifact_publication.PublicationError) as publication:
            artifact_publication.publish_artifact("missing", store=self.store, repo_root=self.base / "repo", distribution="stable", component="main", confirm="")
        self.assertEqual(publication.exception.code, "build_run_not_found")

    def test_manual_delete_overrides_disabled_retention_and_clears_validation_output(self):
        run, root = self.make_run(status="failed")
        run["validations"] = [{"status": "failed", "commands": [{"stdout": "secret output", "stderr": "error"}]}]
        self.store.save(run)
        commands = root / "validation/attempt-one/commands"
        commands.mkdir(parents=True)
        (commands / "001.json").write_text("detailed validation log")
        (commands.parent / "previous.deb").write_bytes(b"previous artifact")
        workspace_cleanup.apply_retention(self.store, {"enabled": False})
        self.assertTrue((root / "source").exists())
        deletion = execution_service.delete_log(self.store, run["id"])
        self.assertTrue(deletion["history_deleted"])
        self.assertIn("source", deletion["workspace_cleanup"]["removed"])
        self.assertFalse(commands.exists())
        self.assertTrue((commands.parent / "previous.deb").exists())
        self.assertEqual(self.store.load(run["id"])["validations"][0]["commands"][0]["stdout"], "")
        self.assertEqual(execution_service.list_executions(self.store, lambda run: "demo"), [])
        self.assertTrue(execution_service.delete_log(BuildStore(self.store.root), run["id"])["already_deleted"])

    def test_traversal_and_forged_runtime_workspace_are_refused(self):
        run, root = self.make_run()
        for run_id in (".", "..", "../run-one", "/tmp/outside"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                workspace_cleanup.clean_workspace(self.store, run_id)
        run["workspace"] = str(self.base)
        self.store.save(run)
        with self.assertRaisesRegex(ValueError, "canonical builds root"):
            execution_service.delete_log(self.store, run["id"])
        self.assertTrue((root / "source/large-data").exists())
        self.assertFalse((self.base / ".workspace.lock").exists())

    def test_symlink_run_root_metadata_and_cleanup_target_are_refused(self):
        for target in ("run", "root", "source", "logs", "run.json", ".workspace.lock", "marker"):
            with self.subTest(target=target), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                self.store = BuildStore(Path(temporary) / "builds")
                run, root = self.make_run()
                if target == "run":
                    actual = root.with_name("outside")
                    root.rename(actual)
                    root.symlink_to(actual, target_is_directory=True)
                elif target == "root":
                    actual_root = self.store.root.with_name("outside-root")
                    self.store.root.rename(actual_root)
                    self.store.root.symlink_to(actual_root, target_is_directory=True)
                    actual = actual_root / run["id"]
                else:
                    actual = root
                    name = workspace_cleanup.HISTORY_MARKER if target == "marker" else target
                    path = root / name
                    outside = Path(temporary) / f"outside-{target}"
                    if path.exists():
                        path.rename(outside)
                    else:
                        outside.write_text("protected")
                    path.symlink_to(outside)
                with self.assertRaises((OSError, ValueError)):
                    execution_service.delete_log(self.store, run["id"])
                if target in {"root", "run"}:
                    self.assertTrue((actual / "source/large-data").exists())
                elif target == "source":
                    self.assertEqual((outside / "large-data").read_text(), "disposable")
                elif target not in {"logs", "run.json"}:
                    self.assertEqual(outside.read_text(), "protected")

    def test_nested_symlink_is_unlinked_without_following_it(self):
        run, root = self.make_run()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("protected")
        (root / "source/link").symlink_to(outside, target_is_directory=True)
        workspace_cleanup.clean_workspace(self.store, run["id"])
        self.assertEqual((outside / "keep").read_text(), "protected")

    def test_bind_mount_below_disposable_directory_is_refused(self):
        run, root = self.make_run()
        mountinfo = f"1 2 0:1 / {root}/source/mounted rw - ext4 /dev/example rw\n"
        with mock.patch("debbuilder.workspace_cleanup.Path.read_text", return_value=mountinfo):
            with self.assertRaisesRegex(ValueError, "Mounted workspace"):
                workspace_cleanup.clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").exists())

    def test_symlink_swap_during_removal_cannot_delete_outside_data(self):
        run, root = self.make_run()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("protected")
        original_rmtree = workspace_cleanup.shutil.rmtree
        def swapped(name, **kwargs):
            if name == "source":
                (root / "source").rename(root / "source-original")
                (root / "source").symlink_to(outside, target_is_directory=True)
            return original_rmtree(name, **kwargs)
        swapped.avoids_symlink_attacks = True
        with mock.patch("debbuilder.workspace_cleanup.shutil.rmtree", swapped):
            with self.assertRaises(OSError):
                workspace_cleanup.clean_workspace(self.store, run["id"])
        self.assertEqual((outside / "keep").read_text(), "protected")

    def test_retention_rechecks_run_after_candidate_scan(self):
        run, root = self.make_run()
        original_read = workspace_cleanup.read_run
        calls = 0
        def changed(fd, build_root, run_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                current = self.store.load(run_id)
                current["validations"] = [{"status": "running"}]
                self.store.save(current)
            return original_read(fd, build_root, run_id)
        with mock.patch("debbuilder.workspace_cleanup.read_run", side_effect=changed):
            result = workspace_cleanup.apply_retention(self.store)
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue((root / "source/large-data").exists())

    def test_artifact_in_disposable_data_is_never_deleted(self):
        run, root = self.make_run()
        run["artifact"]["path"] = str(root / "source/large-data")
        self.store.save(run)
        with self.assertRaisesRegex(ValueError, "Final artifact"):
            execution_service.delete_log(self.store, run["id"])
        self.assertTrue((root / "source/large-data").exists())
        self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())

    def test_manual_log_deletion_does_not_remove_a_misplaced_final_artifact(self):
        run, root = self.make_run()
        artifact = root / "logs/demo.deb"
        artifact.write_bytes(b"final artifact")
        run["artifact"]["path"] = str(artifact)
        self.store.save(run)
        with self.assertRaisesRegex(ValueError, "Final artifact"):
            execution_service.delete_log(self.store, run["id"])
        self.assertTrue(artifact.exists())
        self.assertTrue((root / "source").exists())

    def test_cleanup_refuses_workspace_lease_across_threads_and_processes(self):
        run, root = self.make_run()
        results = []
        def cleanup():
            try:
                workspace_cleanup.clean_workspace(BuildStore(self.store.root), run["id"])
            except workspace_cleanup.WorkspaceBusyError:
                results.append("busy")
        with self.store.locked_run(run["id"]):
            thread = threading.Thread(target=cleanup)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            code = "from pathlib import Path; import sys; from debbuilder.build_store import BuildStore; from debbuilder.workspace_cleanup import clean_workspace, WorkspaceBusyError\ntry: clean_workspace(BuildStore(Path(sys.argv[1])), sys.argv[2])\nexcept WorkspaceBusyError: sys.exit(0)\nsys.exit(1)"
            child = subprocess.run([sys.executable, "-c", code, str(self.store.root), run["id"]], timeout=5, capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(results, ["busy"])
        self.assertTrue((root / "source").exists())
        workspace_cleanup.clean_workspace(self.store, run["id"])
        self.assertFalse((root / "source").exists())

    def test_failed_run_with_live_process_is_protected_until_process_exits(self):
        run, root = self.make_run(status="failed")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            cwd=root / "source", stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "process still uses"):
                execution_service.delete_log(self.store, run["id"])
            result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
            self.assertIn(run["id"], result["skipped"])
            self.assertTrue((root / "source/large-data").exists())
            self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.assertIn("source", workspace_cleanup.clean_workspace(self.store, run["id"])["removed"])

    def test_build_pipeline_holds_workspace_lease_even_before_running_status(self):
        def acquire(_recipe, workspace, token=""):
            with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                workspace_cleanup.clean_workspace(self.store, Path(workspace).name)
            raise source_acquisition.SourceError("test_failure", "test failure")
        result = build_pipeline.run_pipeline(recipe(), store=self.store, dry_run=True, acquire=acquire)
        self.assertEqual(result["status"], "failed")
        workspace_cleanup.clean_workspace(self.store, result["run_id"])

    def test_already_absent_workspace_and_disabled_policy_are_safe(self):
        self.assertEqual(workspace_cleanup.apply_retention(self.store)["cleaned"], [])
        run, root = self.make_run()
        self.assertEqual(workspace_cleanup.apply_retention(self.store, {"enabled": False})["cleaned"], [])
        self.assertTrue((root / "source").exists())
        workspace_cleanup.clean_workspace(self.store, run["id"])
        self.assertEqual(workspace_cleanup.clean_workspace(self.store, run["id"])["removed"], [])
        with self.assertRaises(FileNotFoundError):
            workspace_cleanup.clean_workspace(self.store, "unknown")

    def test_policy_validates_types_and_does_not_change_recipe_schema(self):
        defaults = default_settings("https://repo.example.test", "stable", "main")
        self.assertEqual(defaults["workspace_cleanup"], {"enabled": True, "failed_workspaces_to_retain": 5})
        updated = validate_settings({"workspace_cleanup": {"failed_workspaces_to_retain": 0}}, defaults)
        self.assertEqual(updated["workspace_cleanup"], {"enabled": True, "failed_workspaces_to_retain": 0})
        for policy in ({"enabled": "false"}, {"failed_workspaces_to_retain": True}, {"failed_workspaces_to_retain": -1}, {"failed_workspaces_to_retain": 1.5}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_settings({"workspace_cleanup": policy}, defaults)
