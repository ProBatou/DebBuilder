import gzip
import hashlib
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import artifact_publication, build_pipeline, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.repository_lock import repository_lease


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
            artifact = Path(shlex.split(command)[-1])
            repo = Path(kwargs["workspace"])
            pool = repo / "pool/main/d/demo" / artifact.name
            pool.parent.mkdir(parents=True, exist_ok=True)
            pool.write_bytes(artifact.read_bytes())
            index = repo / "dists/bookworm/main/binary-amd64"
            index.mkdir(parents=True, exist_ok=True)
            index_text = (
                "Package: demo\nVersion: 2.0-1\nArchitecture: all\n"
                f"Filename: {pool.relative_to(repo).as_posix()}\nSize: {artifact.stat().st_size}\n"
                f"SHA256: {hashlib.sha256(artifact.read_bytes()).hexdigest()}\n\n"
            )
            (index / "Packages.gz").write_bytes(gzip.compress(index_text.encode()))
            stdout = "Exporting indices...\n"
        elif " list " in command:
            stdout = "bookworm|main|amd64: old 1\n" + ("bookworm|main|amd64: demo 2.0-1\n" if self.published else "")
        else:
            stdout = ""
        return {"command": command, "arguments": [], "working_directory": str(kwargs.get("workspace")), "status": "success", "exit_code": 0, "stdout": stdout, "stderr": "", "duration": 0.01, "timed_out": False}


class ArtifactPublicationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("reprepro") and shutil.which("dpkg-deb"), "Debian repository tools unavailable")
    def test_real_reprepro_accepts_pinned_descriptors_and_creates_exact_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = BuildStore(base / "builds")
            recipe = {"name": "demo", "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>"}, "source": {"repository": "owner/demo"}}
            run = store.create(recipe, mode="build", run_id="real-reprepro")
            package = base / "package"
            (package / "DEBIAN").mkdir(parents=True)
            (package / "usr/share/demo").mkdir(parents=True)
            (package / "DEBIAN/control").write_text(
                "Package: demo\nVersion: 2.0-1\nArchitecture: all\nSection: utils\n"
                "Priority: optional\nMaintainer: Demo <demo@example.test>\nDescription: test\n"
            )
            (package / "usr/share/demo/data").write_text("payload")
            artifact = Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb"
            subprocess.run(["dpkg-deb", "--build", str(package), str(artifact)], check=True, capture_output=True)
            inspection = artifact_publication.deb_inspector.inspect_deb(artifact)
            run.update({
                "status": "success",
                "artifact": {"path": str(artifact), "size": artifact.stat().st_size, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "inspection": inspection},
                "validations": [{"id": "validation", "artifact": str(artifact), "status": "success"}],
            })
            store.save(run)
            repo = base / "repo"
            (repo / "conf").mkdir(parents=True)
            (repo / "conf/distributions").write_text("Suite: stable\nCodename: bookworm\nArchitectures: amd64 arm64\nComponents: main\n")

            result = artifact_publication.publish_artifact(
                run["id"], store=store, repo_root=repo, distribution="bookworm",
                component="main", confirm="publish:demo:2.0-1",
            )

            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertEqual(result["proof"]["pool"]["sha256"], run["artifact"]["sha256"])
            self.assertEqual(result["proof"]["database_architectures"], ["amd64", "arm64"])
            self.assertEqual(len(result["proof"]["targets"]), 2)
            self.assertTrue((repo / result["proof"]["pool"]["path"]).is_file())

    def test_publication_after_workspace_cleanup_uses_retained_artifact_and_holds_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            artifact = Path(run["artifact"]["path"])
            original = artifact.read_bytes()
            workspace_cleanup.clean_workspace(store, run["id"])
            runner = RepreproRunner()
            def locked_runner(*args, **kwargs):
                with self.assertRaisesRegex(RuntimeError, "Run lock cannot be acquired"):
                    store.clear_log_history(run["id"])
                return runner(*args, **kwargs)
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.publish_artifact(
                    run["id"], store=store, repo_root=repo, distribution="bookworm", component="main",
                    confirm="publish:demo:2.0-1", runner=locked_runner,
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(artifact.read_bytes(), original)

    def make_run(self, root, run_id="run-one"):
        store = BuildStore(Path(root) / "builds")
        recipe = {"name": "demo", "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>"}, "source": {"repository": "owner/demo"}}
        run = store.create(recipe, mode="build", run_id=run_id)
        artifact = Path(run["workspace"]) / "artifacts/demo_2.0-1_all.deb"
        artifact.write_bytes(b"deb")
        run["status"] = "success"
        run["version"] = {"upstream": "2.0", "debian": "2.0-1"}
        run["artifact"] = {"path": str(artifact), "size": artifact.stat().st_size, "inspection": {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}}
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

    def populate_exact(self, repo, run, *, payload=None, filename=None, size=None, sha256=None, duplicate=False):
        artifact = Path(run["artifact"]["path"])
        content = artifact.read_bytes() if payload is None else payload
        relative = filename or f"pool/main/d/demo/{artifact.name}"
        if not relative.startswith("/") and ".." not in relative.split("/"):
            pool = repo / relative
            pool.parent.mkdir(parents=True, exist_ok=True)
            pool.write_bytes(content)
        index = repo / "dists/bookworm/main/binary-amd64"
        index.mkdir(parents=True, exist_ok=True)
        paragraph = (
            "Package: demo\nVersion: 2.0-1\nArchitecture: all\n"
            f"Filename: {relative}\nSize: {len(content) if size is None else size}\n"
            f"SHA256: {hashlib.sha256(content).hexdigest() if sha256 is None else sha256}\n\n"
        )
        (index / "Packages.gz").write_bytes(gzip.compress((paragraph * (2 if duplicate else 1)).encode()))

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
            include_command = next(command for command in runner.commands if " includedeb " in command)
            self.assertIn("/proc/self/fd/", include_command)
            self.assertEqual(len(signing_call["pass_fds"]), len(set(signing_call["pass_fds"])))
            self.assertGreaterEqual(len(signing_call["pass_fds"]), 3)
            persisted = store.load(run["id"])["publications"][-1]
            self.assertEqual(persisted["status"], "success")
            self.assertEqual(persisted["proof"], result["proof"])

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

    def test_reprepro_failure_and_stale_export_never_claim_success(self):
        class IncludeFailureRunner(RepreproRunner):
            def __call__(self, command, **kwargs):
                if " includedeb " in command:
                    self.commands.append(command)
                    return {"command": command, "arguments": [], "working_directory": str(kwargs.get("workspace")), "status": "failed", "exit_code": 1, "stdout": "", "stderr": "include failed", "duration": 0.01, "timed_out": False}
                return super().__call__(command, **kwargs)

        class StaleExportRunner(RepreproRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                if " includedeb " in command:
                    (Path(kwargs["workspace"]) / "dists/bookworm/main/binary-amd64/Packages.gz").unlink()
                return result

        for expected_code, runner in (("reprepro_include_failed", IncludeFailureRunner()), ("publication_proof_failed", StaleExportRunner())):
            with self.subTest(code=expected_code), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.publish_artifact(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", confirm="publish:demo:2.0-1", runner=runner,
                    )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], expected_code)

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
            self.assertEqual(missing["error"]["code"], "publication_proof_failed")
            index = repo / "dists/bookworm/main/binary-amd64"
            index.mkdir(parents=True)
            artifact = Path(run["artifact"]["path"])
            pool = repo / "pool/main/d/demo" / artifact.name
            pool.parent.mkdir(parents=True)
            pool.write_bytes(artifact.read_bytes())
            index_text = (
                "Package: demo\nVersion: 2.0-1\nArchitecture: all\n"
                f"Filename: {pool.relative_to(repo).as_posix()}\nSize: {artifact.stat().st_size}\n"
                f"SHA256: {hashlib.sha256(artifact.read_bytes()).hexdigest()}\n\n"
            )
            (index / "Packages.gz").write_bytes(gzip.compress(index_text.encode()))
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                reconciled = artifact_publication.reconcile_publication(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", runner=runner)
            self.assertEqual(reconciled["status"], "success")
            self.assertEqual(reconciled["type"], "publication_reconciled")
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=inspection):
                repeated = artifact_publication.reconcile_publication(run["id"], store=store, repo_root=repo, distribution="bookworm", component="main", runner=runner)
            self.assertEqual(repeated["status"], "success")
            self.assertEqual(repeated["proof"]["source"]["sha256"], run["artifact"]["sha256"])
            persisted = store.load(run["id"])
            self.assertEqual(persisted["publications"][0]["status"], "failed")
            self.assertEqual(persisted["publications"][-1]["status"], "success")
            self.assertIn("Publication reconciliation", persisted["events"][-1]["message"])

    def test_fail_fast_busy_is_persisted_for_a_different_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            acquired = threading.Event()
            release = threading.Event()
            owner = threading.Thread(target=lambda: self._hold_repository(repo, acquired, release))
            owner.start()
            self.assertTrue(acquired.wait(1))
            result = artifact_publication.publish_artifact(
                run["id"], store=store, repo_root=repo, distribution="bookworm",
                component="main", confirm="publish:demo:2.0-1", runner=RepreproRunner(),
            )
            release.set()
            owner.join(1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "repository_mutation_busy")

    def test_different_run_publications_cannot_enter_repository_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, first = self.make_run(temporary, "run-first")
            _store, second = self.make_run(temporary, "run-second")
            repo, _ = self.make_repo(temporary)
            entered = threading.Event()
            release = threading.Event()
            first_result = {}

            class HoldingRunner(RepreproRunner):
                def __call__(self, command, **kwargs):
                    if not entered.is_set():
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError("publication hold timed out")
                    return super().__call__(command, **kwargs)

            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", side_effect=lambda path, **kwargs: first["artifact"]["inspection"]):
                owner = threading.Thread(target=lambda: first_result.setdefault("value", artifact_publication.publish_artifact(
                    first["id"], store=store, repo_root=repo, distribution="bookworm", component="main",
                    confirm="publish:demo:2.0-1", runner=HoldingRunner(),
                )))
                owner.start()
                self.assertTrue(entered.wait(1))
                contender = artifact_publication.publish_artifact(
                    second["id"], store=store, repo_root=repo, distribution="bookworm", component="main",
                    confirm="publish:demo:2.0-1", runner=RepreproRunner(),
                )
                release.set()
                owner.join(2)
            self.assertFalse(owner.is_alive())
            self.assertEqual(contender["error"]["code"], "repository_mutation_busy")
            self.assertEqual(first_result["value"]["status"], "success")

    @staticmethod
    def _hold_repository(repo, acquired, release):
        with repository_lease(repo, operation="test-owner"):
            acquired.set()
            release.wait(2)

    def test_same_identity_and_digest_is_idempotent_without_includedeb(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            self.populate_exact(repo, run)
            runner = RepreproRunner()
            runner.published = True
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.publish_artifact(
                    run["id"], store=store, repo_root=repo, distribution="bookworm",
                    component="main", confirm="publish:demo:2.0-1", runner=runner,
                )
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["proof"]["proof_version"], 1)
            self.assertEqual(result["proof"]["index"]["sha256"], run["artifact"]["sha256"])
            self.assertFalse(any(" includedeb " in command for command in runner.commands))

    def test_same_identity_with_different_content_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            self.populate_exact(repo, run, payload=b"different")
            runner = RepreproRunner()
            runner.published = True
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.publish_artifact(
                    run["id"], store=store, repo_root=repo, distribution="bookworm",
                    component="main", confirm="publish:demo:2.0-1", runner=runner,
                )
            self.assertEqual(result["error"]["code"], "publication_identity_conflict")
            self.assertFalse(any(" includedeb " in command for command in runner.commands))

    def test_database_distribution_component_and_architecture_must_match_exactly(self):
        replacements = {
            "distribution": ("bookworm|main|amd64:", "testing|main|amd64:"),
            "component": ("bookworm|main|amd64:", "bookworm|contrib|amd64:"),
            "architecture": ("bookworm|main|amd64:", "bookworm|main|arm64:"),
        }
        for dimension, (before, after) in replacements.items():
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                self.populate_exact(repo, run)

                class WrongIdentityRunner(RepreproRunner):
                    def __call__(self, command, **kwargs):
                        result = super().__call__(command, **kwargs)
                        if " list " in command:
                            result["stdout"] = result["stdout"].replace(before, after)
                        return result

                runner = WrongIdentityRunner()
                runner.published = True
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.reconcile_publication(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", runner=runner,
                    )
                self.assertEqual(result["error"]["code"], "publication_proof_failed")

    def test_architecture_all_proves_every_configured_binary_architecture(self):
        class MultiArchitectureRunner(RepreproRunner):
            def __init__(self, *, export_arm64=True):
                super().__init__()
                self.export_arm64 = export_arm64

            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                root = Path(kwargs["workspace"])
                if " includedeb " in command and self.export_arm64:
                    source = root / "dists/bookworm/main/binary-amd64/Packages.gz"
                    target = root / "dists/bookworm/main/binary-arm64/Packages.gz"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                if " list " in command and self.published:
                    result["stdout"] += "bookworm|main|arm64: demo 2.0-1\n"
                return result

        for export_arm64, expected in ((True, "success"), (False, "failed")):
            with self.subTest(export_arm64=export_arm64), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                (repo / "conf/distributions").write_text("Suite: stable\nCodename: bookworm\nArchitectures: amd64 arm64\nComponents: main\n")
                runner = MultiArchitectureRunner(export_arm64=export_arm64)
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.publish_artifact(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", confirm="publish:demo:2.0-1", runner=runner,
                    )
                self.assertEqual(result["status"], expected)
                if expected == "success":
                    self.assertEqual(result["proof"]["database_architectures"], ["amd64", "arm64"])
                    self.assertEqual(len(result["proof"]["targets"]), 2)
                else:
                    self.assertEqual(result["error"]["code"], "publication_proof_failed")

    def test_source_is_rehashed_after_repository_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            self.populate_exact(repo, run)
            artifact = Path(run["artifact"]["path"])

            class MutatingRunner(RepreproRunner):
                def __call__(self, command, **kwargs):
                    result = super().__call__(command, **kwargs)
                    if " list " in command:
                        artifact.write_bytes(b"bad")
                    return result

            runner = MutatingRunner()
            runner.published = True
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.reconcile_publication(
                    run["id"], store=store, repo_root=repo, distribution="bookworm",
                    component="main", runner=runner,
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "artifact_identity_mismatch")

    def test_symlinked_standard_layout_directory_fails_before_reprepro(self):
        for name in ("db", "dists", "pool", "lists", "logs", "morgue"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                outside = Path(temporary) / f"outside-{name}"
                outside.mkdir()
                (repo / name).symlink_to(outside, target_is_directory=True)
                runner = RepreproRunner()
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.publish_artifact(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", confirm="publish:demo:2.0-1", runner=runner,
                    )
                self.assertEqual(result["error"]["code"], "repository_configuration_unsupported")
                self.assertEqual(runner.commands, [])
                self.assertEqual(list(outside.iterdir()), [])

    def test_exact_verifier_rejects_unsafe_or_malformed_index_entries(self):
        cases = (
            ("duplicate", {"duplicate": True}),
            ("absolute", {"filename": "/tmp/demo.deb"}),
            ("traversal", {"filename": "pool/../demo.deb"}),
            ("outside_pool", {"filename": "dists/demo.deb"}),
            ("invalid_size", {"size": "not-a-size"}),
            ("invalid_hash", {"sha256": "xyz"}),
        )
        for label, options in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                self.populate_exact(repo, run, **options)
                runner = RepreproRunner()
                runner.published = True
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.reconcile_publication(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", runner=runner,
                    )
                self.assertIn(result["error"]["code"], {"publication_proof_failed", "publication_identity_conflict"})

    def test_exact_verifier_rejects_missing_required_index_fields(self):
        for missing in ("Filename", "Size", "SHA256"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                artifact = Path(run["artifact"]["path"])
                pool = repo / "pool/main/d/demo" / artifact.name
                pool.parent.mkdir(parents=True)
                pool.write_bytes(artifact.read_bytes())
                fields = {
                    "Package": "demo", "Version": "2.0-1", "Architecture": "all",
                    "Filename": pool.relative_to(repo).as_posix(), "Size": str(artifact.stat().st_size),
                    "SHA256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
                fields.pop(missing)
                index = repo / "dists/bookworm/main/binary-amd64"
                index.mkdir(parents=True)
                text = "".join(f"{key}: {value}\n" for key, value in fields.items()) + "\n"
                (index / "Packages.gz").write_bytes(gzip.compress(text.encode()))
                runner = RepreproRunner()
                runner.published = True
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.reconcile_publication(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", runner=runner,
                    )
                self.assertEqual(result["error"]["code"], "publication_proof_failed")

    def test_exact_verifier_rejects_duplicate_fields_within_one_stanza(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            self.populate_exact(repo, run)
            index = repo / "dists/bookworm/main/binary-amd64/Packages.gz"
            text = gzip.decompress(index.read_bytes()).decode()
            index.write_bytes(gzip.compress(text.replace("Size: 3\n", "Size: 3\nSize: 3\n").encode()))
            runner = RepreproRunner()
            runner.published = True
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.reconcile_publication(
                    run["id"], store=store, repo_root=repo, distribution="bookworm",
                    component="main", runner=runner,
                )
            self.assertEqual(result["error"]["code"], "publication_proof_failed")

    def test_exact_verifier_rejects_missing_symlinked_and_mismatched_pool(self):
        cases = (("missing", "missing"), ("symlink", "symlink"), ("wrong-content", "wrong"))
        for label, mode in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                store, run = self.make_run(temporary)
                repo, _ = self.make_repo(temporary)
                self.populate_exact(repo, run)
                pool = next((repo / "pool").rglob("*.deb"))
                if mode == "missing":
                    pool.unlink()
                elif mode == "symlink":
                    pool.unlink()
                    pool.symlink_to(Path(run["artifact"]["path"]))
                else:
                    pool.write_bytes(b"bad")
                runner = RepreproRunner()
                runner.published = True
                with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                    result = artifact_publication.reconcile_publication(
                        run["id"], store=store, repo_root=repo, distribution="bookworm",
                        component="main", runner=runner,
                    )
                self.assertIn(result["error"]["code"], {"publication_proof_failed", "publication_identity_conflict"})

    def test_source_tamper_and_repository_redirection_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            Path(run["artifact"]["path"]).write_bytes(b"tampered")
            result = artifact_publication.publish_artifact(
                run["id"], store=store, repo_root=repo, distribution="bookworm",
                component="main", confirm="publish:demo:2.0-1", runner=RepreproRunner(),
            )
            self.assertEqual(result["error"]["code"], "artifact_identity_mismatch")
        with tempfile.TemporaryDirectory() as temporary:
            store, run = self.make_run(temporary)
            repo, _ = self.make_repo(temporary)
            (repo / "conf/options").write_text("--outdir /tmp/external\n")
            with mock.patch("debbuilder.artifact_publication.deb_inspector.inspect_deb", return_value=run["artifact"]["inspection"]):
                result = artifact_publication.publish_artifact(
                    run["id"], store=store, repo_root=repo, distribution="bookworm",
                    component="main", confirm="publish:demo:2.0-1", runner=RepreproRunner(),
                )
            self.assertEqual(result["error"]["code"], "repository_configuration_unsupported")


if __name__ == "__main__":
    unittest.main()
