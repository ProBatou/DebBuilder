import gzip
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import artifact_publication, build_pipeline, workspace_cleanup
from debbuilder.build_store import BuildStore


class RepreproRunner:
    def __init__(self):
        self.published = False
        self.commands = []
        self.calls = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.calls.append((command, kwargs))
        if "dpkg --compare-versions 2.0-1 gt 10.0-1" in command:
            return {"command": command, "arguments": [], "working_directory": str(kwargs.get("workspace")), "status": "failed", "exit_code": 1, "stdout": "", "stderr": "", "duration": 0.01, "timed_out": False}
        if "dpkg --compare-versions 2.0-1 lt 10.0-1" in command:
            stdout = ""
        elif " includedeb " in command:
            self.published = True
            stdout = "Exporting indices...\n"
        elif " list " in command:
            stdout = "bookworm|main|amd64: old 1\n" + ("bookworm|main|amd64: demo 2.0-1\n" if self.published else "")
        else:
            stdout = ""
        return {"command": command, "arguments": [], "working_directory": str(kwargs.get("workspace")), "status": "success", "exit_code": 0, "stdout": stdout, "stderr": "", "duration": 0.01, "timed_out": False}


class ArtifactPublicationTests(unittest.TestCase):
    def test_publication_after_workspace_cleanup_uses_retained_artifact_and_holds_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            artifact = Path(run["artifact"]["path"])
            original = artifact.read_bytes()
            workspace_cleanup.clean_workspace(store, run["id"])
            runner = RepreproRunner()
            def locked_runner(*args, **kwargs):
                with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                    store.clear_log_history(run["id"])
                return runner(*args, **kwargs)
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.publish_artifact(
                    run["id"], store=store, repo_root=repo, distribution="bookworm", component="main",
                    confirm="publish:demo:2.0-1", runner=locked_runner,
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(artifact.read_bytes(), original)

    def make_run(self, root):
        store = BuildStore(Path(root) / "builds")
        recipe = {"name": "demo", "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>"}, "source": {"repository": "owner/demo"}}
        run = store.create(recipe, mode="build", run_id="run-one")
        artifact = Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb"
        artifact.write_bytes(b"deb")
        run["status"] = "success"
        run["version"] = {"upstream": "2.0", "debian": "2.0-1"}
        run["artifact"] = {"path": str(artifact), "inspection": {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}}
        run["artifact"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        run["validations"] = [{"id": "validation-one", "artifact": str(artifact), "status": "success"}]
        store.save(run)
        return store, run

    def make_repo(self, root):
        repo = Path(root) / "repo"
        (repo / "conf").mkdir(parents=True)
        config = "Suite: stable\nCodename: bookworm\nArchitectures: amd64\nComponents: main\nSignWith: yes\n"
        (repo / "conf/distributions").write_text(config)
        return repo, config

    def test_requires_exact_confirmation_and_preserves_build_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            result = artifact_publication.publish_artifact(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", confirm="", runner=RepreproRunner())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "publication_confirmation_required")
            persisted = store.load(run["id"])
            self.assertEqual(persisted["status"], "success")
            self.assertEqual(persisted["validations"][0]["status"], "success")
            self.assertEqual(persisted["publications"][0]["status"], "failed")

    def test_publishes_validated_all_package_without_changing_distribution_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, config = self.make_repo(temporary)
            runner = RepreproRunner()
            inspection = {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                result = artifact_publication.publish_artifact(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", confirm="publish:demo:2.0-1", runner=runner)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["published_version"], "2.0-1")
            self.assertIn("all accepted", result["preflight"]["architecture_policy"])
            self.assertEqual((repo / "conf/distributions").read_text(), config)
            self.assertTrue(any(" includedeb " in command for command in runner.commands))
            signing_call = next(kwargs for command, kwargs in runner.calls if " includedeb " in command)
            self.assertEqual(signing_call["environment"]["GNUPGHOME"], str(Path.home() / ".gnupg"))

    def test_publication_running_state_is_visible_until_repository_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            entered = threading.Event()
            release = threading.Event()
            result = {}

            class BlockingRunner(RepreproRunner):
                def __init__(self):
                    super().__init__()
                    self.blocked = False

                def __call__(self, command, **kwargs):
                    if not self.blocked:
                        self.blocked = True
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError("test repository runner was not released")
                    return super().__call__(command, **kwargs)

            inspection = {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}
            runner = BlockingRunner()
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                thread = threading.Thread(
                    target=lambda: result.setdefault("publication", artifact_publication.publish_artifact(
                        run["id"], store=store, repo_root=repo, distribution="bookworm", component="main",
                        confirm="publish:demo:2.0-1", runner=runner,
                    )),
                )
                thread.start()
                self.assertTrue(entered.wait(1))
                persisted = store.load(run["id"])
                self.assertEqual(persisted["publications"][-1]["status"], "running")
                self.assertEqual(build_pipeline.execution_summary(persisted)["lifecycle_status"], "publishing")
                self.assertTrue(build_pipeline.execution_summary(persisted)["lifecycle_active"])
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["publication"]["status"], "success")
            self.assertEqual(store.load(run["id"])["publications"][-1]["status"], "success")

    def test_unvalidated_artifact_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            run["validations"] = []
            store.save(run)
            repo, _ = self.make_repo(temporary)
            runner = RepreproRunner()
            result = artifact_publication.publish_artifact(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", confirm="publish:demo:2.0-1", runner=runner)
            self.assertEqual(result["error"]["code"], "artifact_not_ready")
            self.assertFalse(any(" includedeb " in command for command in runner.commands))

    def test_refuses_debian_downgrade_before_includedeb(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            run["version"]["debian"] = "2.0-1"
            store.save(run)
            repo, _ = self.make_repo(temporary)
            index = repo / "dists/bookworm/main/binary-amd64"
            index.mkdir(parents=True)
            (index / "Packages.gz").write_bytes(gzip.compress(b"Package: demo\nVersion: 10.0-1\nArchitecture: all\n\n"))
            runner = RepreproRunner()
            inspection = {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                result = artifact_publication.publish_artifact(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", confirm="publish:demo:2.0-1", runner=runner)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "downgrade_refused")
            self.assertFalse(any(" includedeb " in command for command in runner.commands))

    def test_reconciliation_requires_database_and_exported_index_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            run["publications"] = [{"id": "old-failure", "status": "failed", "error": {"code": "export_failed"}}]
            store.save(run)
            repo, _ = self.make_repo(temporary)
            runner = RepreproRunner()
            runner.published = True
            inspection = {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                missing = artifact_publication.reconcile_publication(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", runner=runner)
            self.assertEqual(missing["error"]["code"], "publication_not_exported")
            index = repo / "dists/bookworm/main/binary-amd64"
            index.mkdir(parents=True)
            (index / "Packages.gz").write_bytes(gzip.compress(b"Package: demo\nVersion: 2.0-1\nArchitecture: all\n\n"))
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                reconciled = artifact_publication.reconcile_publication(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", runner=runner)
            self.assertEqual(reconciled["status"], "success")
            self.assertEqual(reconciled["type"], "publication_reconciled")
            persisted = store.load(run["id"])
            self.assertEqual(persisted["publications"][0]["status"], "failed")
            self.assertEqual(persisted["publications"][-1]["status"], "success")
            self.assertIn("Publication reconciliation", persisted["events"][-1]["message"])


if __name__ == "__main__":
    unittest.main()
