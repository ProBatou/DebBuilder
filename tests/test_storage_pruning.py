import gzip
import hashlib
import shlex
import shutil
import tempfile
import threading
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest import mock

from debbuilder import artifact_publication, artifact_validation, package_store, storage_pruning, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.repository_lock import repository_lease, repository_lease_held


class RepositoryRunner:
    def __init__(self):
        self.published = False

    def __call__(self, command, **kwargs):
        repository = Path(kwargs["workspace"])
        if " includedeb " in command:
            self.published = True
            artifact = Path(shlex.split(command)[-1])
            pool = repository / "pool/main/d/demo" / "demo_2.0-1_all.deb"
            pool.parent.mkdir(parents=True, exist_ok=True)
            pool.write_bytes(artifact.read_bytes())
            index = repository / "dists/bookworm/main/binary-amd64"
            index.mkdir(parents=True, exist_ok=True)
            data = pool.read_bytes()
            text = (
                "Package: demo\nVersion: 2.0-1\nArchitecture: all\n"
                f"Filename: {pool.relative_to(repository).as_posix()}\n"
                f"Size: {len(data)}\nSHA256: {hashlib.sha256(data).hexdigest()}\n\n"
            )
            (index / "Packages.gz").write_bytes(gzip.compress(text.encode()))
            stdout = "Exporting indices\n"
        elif " list " in command:
            stdout = "bookworm|main|amd64: demo 2.0-1\n" if self.published else ""
        else:
            stdout = ""
        return {
            "command": command, "arguments": [], "working_directory": str(repository),
            "status": "success", "exit_code": 0, "stdout": stdout, "stderr": "",
            "duration": 0.01, "timed_out": False,
        }


def recipe():
    return {
        "name": "demo",
        "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>"},
        "source": {"repository": "owner/demo"},
    }


class StoragePruningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = BuildStore(self.base / "builds")
        self.repo = self.base / "repo"
        (self.repo / "conf").mkdir(parents=True)
        (self.repo / "conf/distributions").write_text(
            "Suite: stable\nCodename: bookworm\nArchitectures: amd64\nComponents: main\n"
        )
        self.runner = RepositoryRunner()
        self.inspection = {"ok": True, "package": "demo", "version": "2.0-1", "architecture": "all"}

    def make_published_run(self, run_id="published"):
        run = self.store.create(recipe(), mode="build", run_id=run_id)
        workspace = Path(run["workspace"])
        artifact = workspace / "artifacts/demo_2.0-1_all.deb"
        artifact.write_bytes(b"test deb payload")
        run.update({
            "status": "success",
            "finished_at": "2026-09-09T10:00:00+00:00",
            "artifact": {
                "path": str(artifact), "size": artifact.stat().st_size,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "inspection": dict(self.inspection),
            },
            "validations": [{"id": "validation", "artifact": str(artifact), "status": "success"}],
        })
        staging = next(step for step in run["steps"] if step["name"] == "staging")
        staging.update({
            "status": "success",
            "details": {"content_manifest": storage_pruning.STAGING_MANIFEST, "content_file_count": 42},
        })
        self.store.save_manifest(run_id, storage_pruning.STAGING_MANIFEST, ["one", "two"])
        self.store.save_manifest(run_id, "manifests/artifact-files.json", [{"path": "/usr/bin/demo"}])
        previous = workspace / "validation/old/previous.deb"
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"old")
        (workspace / "unknown.bin").write_bytes(b"unknown")
        self.store.save(run)
        with mock.patch.object(artifact_publication.deb_inspector, "inspect_deb", return_value=self.inspection):
            published = artifact_publication.publish_artifact(
                run_id, store=self.store, repo_root=self.repo, distribution="bookworm",
                component="main", confirm="publish:demo:2.0-1", runner=self.runner,
            )
        self.assertEqual(published["status"], "success", published.get("error"))
        return self.store.load(run_id), workspace, artifact

    def sweep(self, **kwargs):
        with mock.patch.object(artifact_publication.deb_inspector, "inspect_deb", return_value=self.inspection):
            return storage_pruning.apply_pruning(
                self.store, repo_root=self.repo, distribution="bookworm", component="main",
                runner=self.runner, **kwargs,
            )

    def make_terminal_manifest_run(self, run_id, *, mode="build", status="success", retained=False):
        run = self.store.create(recipe(), mode=mode, run_id=run_id)
        workspace = Path(run["workspace"])
        run["status"] = status
        run["finished_at"] = "2026-09-09T10:00:00+00:00"
        staging = next(step for step in run["steps"] if step["name"] == "staging")
        staging.update({
            "status": "success",
            "details": {"content_manifest": storage_pruning.STAGING_MANIFEST, "content_file_count": 7},
        })
        self.store.save_manifest(run_id, storage_pruning.STAGING_MANIFEST, ["one"])
        if retained:
            (workspace / "source/evidence").write_bytes(b"retain")
        self.store.save(run)
        return self.store.load(run_id), workspace

    def test_exactly_published_artifact_and_staging_manifest_are_pruned(self):
        run, workspace, artifact = self.make_published_run()

        result = self.sweep()

        self.assertEqual(result["pruned"], [run["id"]], result)
        self.assertEqual(result["manifests_pruned"], [run["id"]])
        self.assertFalse(artifact.exists())
        self.assertTrue((self.repo / run["publications"][-1]["proof"]["pool"]["path"]).is_file())
        self.assertFalse((workspace / storage_pruning.STAGING_MANIFEST).exists())
        self.assertTrue((workspace / "manifests/artifact-files.json").is_file())
        self.assertTrue((workspace / "validation/old/previous.deb").is_file())
        self.assertTrue((workspace / "unknown.bin").is_file())
        persisted = self.store.load(run["id"])
        self.assertEqual(persisted["artifact"]["pruning"]["status"], "pruned")
        self.assertEqual(persisted["publications"][-1]["proof"], run["publications"][-1]["proof"])
        staging = next(step for step in persisted["steps"] if step["name"] == "staging")
        self.assertNotIn("content_manifest", staging["details"])
        self.assertEqual(staging["details"]["content_manifest_pruning"]["content_file_count"], 42)
        self.assertFalse(package_store.allowed_actions("up_to_date", "demo", persisted)["validate"])
        self.assertFalse(package_store.allowed_actions("up_to_date", "demo", persisted)["publish"])
        with self.assertRaises(artifact_validation.ValidationError) as raised:
            artifact_validation.validate_artifact(run["id"], store=self.store)
        self.assertEqual(raised.exception.code, "artifact_not_available")
        self.assertFalse(artifact_publication.publication_readiness(persisted)["ready"])
        self.assertIn("artifact_unavailable", artifact_publication.publication_readiness(persisted)["reasons"])
        self.assertEqual(self.sweep()["already_pruned"], [run["id"]])
        artifact.write_bytes(b"test deb payload")
        reappeared = self.sweep()
        self.assertTrue(reappeared["errors"])
        self.assertTrue(artifact.is_file())

    def test_malformed_pruning_metadata_preserves_the_local_artifact(self):
        run, _workspace, artifact = self.make_published_run()
        persisted = self.store.load(run["id"])
        persisted["artifact"]["pruning"] = {"schema": storage_pruning.PRUNING_SCHEMA, "status": "pruned"}
        self.store.save(persisted)

        result = self.sweep()

        self.assertTrue(result["errors"])
        self.assertTrue(artifact.is_file())

    def test_unpublished_failed_and_malformed_proof_artifacts_are_preserved(self):
        run, workspace, artifact = self.make_published_run()
        persisted = self.store.load(run["id"])
        persisted["publications"][-1]["status"] = "failed"
        self.store.save(persisted)
        self.assertEqual(self.sweep()["pruned"], [])
        self.assertTrue(artifact.is_file())

        persisted = self.store.load(run["id"])
        persisted["publications"][-1]["status"] = "success"
        persisted["publications"][-1]["proof"]["schema"] = "unknown"
        self.store.save(persisted)
        result = self.sweep()
        self.assertEqual(result["pruned"], [])
        self.assertEqual(result["errors"][0]["id"], run["id"])
        self.assertTrue(artifact.is_file())
        self.assertFalse((workspace / storage_pruning.STAGING_MANIFEST).exists())

    def test_staging_manifest_prunes_independently_for_terminal_run_classes(self):
        fixtures = (
            ("prepared", "dry_run", "prepared"),
            ("failed", "build", "failed"),
            ("cancelled", "build", "cancelled"),
            ("unpublished", "build", "success"),
        )
        for run_id, mode, status in fixtures:
            self.make_terminal_manifest_run(run_id, mode=mode, status=status)

        result = self.sweep()

        self.assertEqual(result["manifests_pruned"], sorted(row[0] for row in fixtures))
        for run_id, _mode, _status in fixtures:
            persisted = self.store.load(run_id)
            workspace = Path(persisted["workspace"])
            staging = next(step for step in persisted["steps"] if step["name"] == "staging")
            self.assertFalse((workspace / storage_pruning.STAGING_MANIFEST).exists())
            self.assertEqual(staging["details"]["content_manifest_pruning"]["content_file_count"], 7)

    def test_staging_manifest_remains_with_retained_failed_workspace_evidence(self):
        run, workspace = self.make_terminal_manifest_run("retained-failed", status="failed", retained=True)

        result = self.sweep()

        self.assertNotIn(run["id"], result["manifests_pruned"])
        self.assertTrue((workspace / storage_pruning.STAGING_MANIFEST).is_file())
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertEqual(staging["details"]["content_manifest"], storage_pruning.STAGING_MANIFEST)

    def test_staging_manifest_crash_after_delete_recovers_from_intent(self):
        run, workspace = self.make_terminal_manifest_run("manifest-crash")
        original = storage_pruning._finish_staging_metadata

        with mock.patch.object(storage_pruning, "_finish_staging_metadata", side_effect=OSError("crash")):
            first = self.sweep()

        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in first["errors"]))
        self.assertFalse((workspace / storage_pruning.STAGING_MANIFEST).exists())
        self.assertTrue((workspace / storage_pruning.STAGING_PRUNING_INTENT).is_file())
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertIn("content_manifest", staging["details"])

        with mock.patch.object(storage_pruning, "_finish_staging_metadata", wraps=original):
            second = self.sweep()
        self.assertIn(run["id"], second["manifests_pruned"])
        self.assertFalse((workspace / storage_pruning.STAGING_PRUNING_INTENT).exists())

    def test_staging_manifest_stop_and_unlink_failure_are_retryable(self):
        stopped, stopped_workspace = self.make_terminal_manifest_run("manifest-stopped")
        intent_written = threading.Event()
        real_write = storage_pruning.write_json

        def observe_write(fd, name, value):
            real_write(fd, name, value)
            if name == storage_pruning.STAGING_PRUNING_INTENT:
                intent_written.set()

        with mock.patch.object(storage_pruning, "write_json", side_effect=observe_write):
            result = self.sweep(should_stop=intent_written.is_set)
        self.assertNotIn(stopped["id"], result["manifests_pruned"])
        self.assertTrue((stopped_workspace / storage_pruning.STAGING_MANIFEST).is_file())
        self.assertTrue((stopped_workspace / storage_pruning.STAGING_PRUNING_INTENT).is_file())
        staging = next(step for step in self.store.load(stopped["id"])["steps"] if step["name"] == "staging")
        self.assertNotIn("content_manifest_pruning", staging["details"])
        self.assertIn(stopped["id"], self.sweep()["manifests_pruned"])

        failed, failed_workspace = self.make_terminal_manifest_run("manifest-unlink-failed")
        real_unlink = storage_pruning.os.unlink

        def fail_manifest(name, **kwargs):
            if name == "staging-files.json":
                raise OSError("unlink failed")
            return real_unlink(name, **kwargs)

        with mock.patch.object(storage_pruning.os, "unlink", side_effect=fail_manifest):
            first = self.sweep()
        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in first["errors"]))
        self.assertTrue((failed_workspace / storage_pruning.STAGING_MANIFEST).is_file())
        self.assertTrue((failed_workspace / storage_pruning.STAGING_PRUNING_INTENT).is_file())
        self.assertIn(failed["id"], self.sweep()["manifests_pruned"])

    def test_stop_after_final_staging_hash_defers_unlink(self):
        run, workspace = self.make_terminal_manifest_run("manifest-stop-after-hash")
        stop = threading.Event()
        original = storage_pruning._hash_fd
        calls = 0

        def request_stop(fd, should_stop):
            nonlocal calls
            digest = original(fd, should_stop)
            calls += 1
            if calls == 2:
                stop.set()
            return digest

        with mock.patch.object(storage_pruning, "_hash_fd", side_effect=request_stop):
            result = self.sweep(should_stop=stop.is_set)

        self.assertNotIn(run["id"], result["manifests_pruned"])
        self.assertTrue((workspace / storage_pruning.STAGING_MANIFEST).is_file())
        self.assertTrue((workspace / storage_pruning.STAGING_PRUNING_INTENT).is_file())
        self.assertIn(run["id"], self.sweep()["manifests_pruned"])

    def test_pruned_staging_manifest_reappearance_fails_closed(self):
        run, workspace = self.make_terminal_manifest_run("manifest-reappeared")
        self.assertIn(run["id"], self.sweep()["manifests_pruned"])
        (workspace / storage_pruning.STAGING_MANIFEST).write_text("[]")

        result = self.sweep()

        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in result["errors"]))
        self.assertTrue((workspace / storage_pruning.STAGING_MANIFEST).is_file())

    def test_repository_or_local_identity_mismatch_preserves_artifact(self):
        run, _workspace, artifact = self.make_published_run()
        pool = self.repo / run["publications"][-1]["proof"]["pool"]["path"]
        pool.write_bytes(b"tampered repository bytes")
        result = self.sweep()
        self.assertEqual(result["pruned"], [])
        self.assertTrue(result["errors"])
        self.assertTrue(artifact.is_file())

        pool.write_bytes(artifact.read_bytes())
        artifact.write_bytes(b"tampered local bytes")
        result = self.sweep()
        self.assertEqual(result["pruned"], [])
        self.assertTrue(result["errors"])
        self.assertTrue(artifact.is_file())

    def test_component_architecture_and_artifact_hardlink_ambiguity_are_rejected(self):
        run, _workspace, artifact = self.make_published_run()
        original_proof = deepcopy(run["publications"][-1]["proof"])
        persisted = self.store.load(run["id"])
        persisted["publications"][-1]["proof"]["component"] = "other"
        self.store.save(persisted)
        self.assertTrue(self.sweep()["errors"])
        self.assertTrue(artifact.is_file())

        persisted = self.store.load(run["id"])
        persisted["publications"][-1]["proof"] = deepcopy(original_proof)
        persisted["publications"][-1]["proof"]["architecture"] = "amd64"
        self.store.save(persisted)
        self.assertTrue(self.sweep()["errors"])
        self.assertTrue(artifact.is_file())

        persisted["publications"][-1]["proof"] = deepcopy(original_proof)
        self.store.save(persisted)
        hardlink = self.base / "artifact-hardlink.deb"
        hardlink.hardlink_to(artifact)
        self.assertTrue(self.sweep()["errors"])
        self.assertTrue(artifact.is_file())
        self.assertTrue(hardlink.is_file())

    def test_global_and_per_run_recovery_blockers_preserve_artifact(self):
        run, _workspace, artifact = self.make_published_run()
        blocked = workspace_cleanup.CleanupAuthorization({"message": "recovery blocked"})
        result = self.sweep(authorization=blocked)
        self.assertEqual(result["blocked"]["scope"], "global")
        self.assertTrue(artifact.is_file())

        persisted = self.store.load(run["id"])
        persisted["recovery"] = {"status": "blocked"}
        self.store.save(persisted)
        result = self.sweep(authorization=workspace_cleanup.CleanupAuthorization())
        self.assertIn(run["id"], result["skipped"])
        self.assertEqual(result["errors"], [])
        self.assertTrue(artifact.is_file())

    def test_runtime_cleanup_blocker_preserves_prunable_artifact(self):
        run, _workspace, artifact = self.make_published_run("runtime-cleanup-blocked")
        with mock.patch(
            "debbuilder.command_containment.runtime_cleanup_blocker",
            return_value="runtime cgroup remains",
        ):
            result = self.sweep()
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue(artifact.is_file())

    def test_artifact_symlink_and_unexpected_missing_file_fail_closed(self):
        run, workspace, artifact = self.make_published_run()
        outside = self.base / "outside.deb"
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(outside)

        result = self.sweep()

        self.assertTrue(result["errors"])
        self.assertEqual(outside.read_bytes(), b"test deb payload")
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))
        artifact.unlink()
        result = self.sweep()
        self.assertTrue(result["errors"])
        self.assertFalse((workspace / storage_pruning.PRUNING_INTENT).exists())
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))

    def test_held_run_lock_and_running_publication_are_not_pruned(self):
        run, _workspace, artifact = self.make_published_run()
        locked = threading.Event()
        release = threading.Event()

        def hold_run():
            with self.store.locked_run(run["id"]):
                locked.set()
                release.wait(2)

        thread = threading.Thread(target=hold_run)
        thread.start()
        self.assertTrue(locked.wait(1))
        try:
            result = self.sweep()
        finally:
            release.set()
            thread.join(2)
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue(artifact.is_file())

        persisted = self.store.load(run["id"])
        persisted["publications"].append({"status": "running"})
        self.store.save(persisted)
        result = self.sweep()
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue(artifact.is_file())

    def test_malformed_run_isolated_from_eligible_run(self):
        run, _workspace, artifact = self.make_published_run("z-eligible")
        malformed = self.store.root / "a-malformed"
        malformed.mkdir()
        (malformed / ".workspace.lock").touch()
        (malformed / "run.json").write_text("{bad json")

        result = self.sweep()

        self.assertTrue(any(row["id"] == "a-malformed" for row in result["errors"]))
        self.assertIn(run["id"], result["pruned"])
        self.assertFalse(artifact.exists())

    def test_metadata_persisted_before_marker_cleanup_recovers_idempotently(self):
        run, workspace, artifact = self.make_published_run()
        real_unlink = storage_pruning.os.unlink

        def fail_marker(name, **kwargs):
            if name == storage_pruning.PRUNING_INTENT:
                raise OSError("marker cleanup failed")
            return real_unlink(name, **kwargs)

        with mock.patch.object(storage_pruning.os, "unlink", side_effect=fail_marker):
            first = self.sweep()
        self.assertTrue(first["errors"])
        self.assertFalse(artifact.exists())
        self.assertEqual(self.store.load(run["id"])["artifact"]["pruning"]["status"], "pruned")
        self.assertTrue((workspace / storage_pruning.PRUNING_INTENT).exists())

        second = self.sweep()
        self.assertEqual(second["already_pruned"], [run["id"]], second)
        self.assertFalse((workspace / storage_pruning.PRUNING_INTENT).exists())

    def test_crash_after_unlink_recovers_from_durable_intent(self):
        run, workspace, artifact = self.make_published_run()
        original = storage_pruning._finish_metadata
        with mock.patch.object(storage_pruning, "_finish_metadata", side_effect=RuntimeError("crash")):
            first = self.sweep()
        self.assertTrue(first["errors"])
        self.assertFalse(artifact.exists())
        self.assertTrue((workspace / storage_pruning.PRUNING_INTENT).is_file())
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))

        with mock.patch.object(storage_pruning, "_finish_metadata", wraps=original):
            second = self.sweep()
        self.assertEqual(second["recovered"], [run["id"]], second)
        self.assertEqual(self.store.load(run["id"])["artifact"]["pruning"]["status"], "pruned")
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertEqual(staging["details"]["content_manifest_pruning"]["status"], "pruned")
        self.assertFalse((workspace / storage_pruning.PRUNING_INTENT).exists())

    def test_unsafe_staging_manifest_is_preserved_without_blocking_artifact_pruning(self):
        run, workspace, artifact = self.make_published_run()
        outside = self.base / "outside-manifest.json"
        outside.write_text("secret")
        manifest = workspace / storage_pruning.STAGING_MANIFEST
        manifest.unlink()
        manifest.symlink_to(outside)

        result = self.sweep()

        self.assertIn(run["id"], result["pruned"])
        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in result["errors"]))
        self.assertFalse(artifact.exists())
        self.assertEqual(outside.read_text(), "secret")
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertEqual(staging["details"]["content_manifest"], storage_pruning.STAGING_MANIFEST)
        self.assertTrue((workspace / "manifests/artifact-files.json").is_file())

    def test_failure_before_unlink_and_stop_after_intent_are_retryable(self):
        run, workspace, artifact = self.make_published_run()
        real_unlink = storage_pruning.os.unlink

        def fail_artifact(name, **kwargs):
            if name == artifact.name:
                raise OSError("unlink failed")
            return real_unlink(name, **kwargs)

        with mock.patch.object(storage_pruning.os, "unlink", side_effect=fail_artifact):
            first = self.sweep()
        self.assertTrue(first["errors"])
        self.assertTrue(artifact.is_file())
        self.assertTrue((workspace / storage_pruning.PRUNING_INTENT).is_file())
        self.assertEqual(self.sweep()["pruned"], [run["id"]])

        second, second_workspace, second_artifact = self.make_published_run("stopped")
        intent_written = threading.Event()
        real_write = storage_pruning.write_json

        def observe_write(fd, name, value):
            real_write(fd, name, value)
            if name == storage_pruning.PRUNING_INTENT:
                intent_written.set()

        with mock.patch.object(storage_pruning, "write_json", side_effect=observe_write):
            stopped = self.sweep(should_stop=intent_written.is_set)
        self.assertEqual(stopped["pruned"], [])
        self.assertTrue(second_artifact.is_file())
        self.assertTrue((second_workspace / storage_pruning.PRUNING_INTENT).is_file())
        self.assertEqual(self.sweep()["pruned"], [second["id"]])

    def test_disabled_policy_starts_no_new_prune_but_finishes_an_intent(self):
        run, workspace, artifact = self.make_published_run()
        policy = {"enabled": False, "failed_workspaces_to_retain": 5}
        self.assertEqual(self.sweep(policy=policy)["pruned"], [])
        self.assertTrue(artifact.is_file())

        with mock.patch.object(storage_pruning, "_finish_metadata", side_effect=RuntimeError("crash")):
            self.sweep()
        self.assertFalse(artifact.exists())
        self.assertTrue((workspace / storage_pruning.PRUNING_INTENT).is_file())
        recovered = self.sweep(policy=policy)
        self.assertEqual(recovered["recovered"], [run["id"]])

    def test_repository_lease_contention_defers_without_deleting(self):
        run, _workspace, artifact = self.make_published_run()
        entered = threading.Event()
        release = threading.Event()

        def hold_repository():
            with repository_lease(self.repo, operation="test-holder"):
                entered.set()
                release.wait(2)

        thread = threading.Thread(target=hold_repository)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            result = self.sweep()
        finally:
            release.set()
            thread.join(2)
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue(artifact.is_file())

    def test_verification_failure_does_not_create_missing_repository_layout(self):
        _run, _workspace, artifact = self.make_published_run()
        shutil.rmtree(self.repo / "logs")

        result = self.sweep()

        self.assertTrue(result["errors"])
        self.assertTrue(artifact.is_file())
        self.assertFalse((self.repo / "logs").exists())

    def test_repository_lease_remains_held_through_delete_and_metadata_persist(self):
        run, _workspace, artifact = self.make_published_run()
        real_unlink = storage_pruning.os.unlink
        real_finish = storage_pruning._finish_metadata
        observed = []

        def observe_unlink(name, **kwargs):
            if name == artifact.name:
                observed.append(("delete", repository_lease_held()))
            return real_unlink(name, **kwargs)

        def observe_finish(*args, **kwargs):
            observed.append(("persist", repository_lease_held()))
            return real_finish(*args, **kwargs)

        with mock.patch.object(storage_pruning.os, "unlink", side_effect=observe_unlink), \
                mock.patch.object(storage_pruning, "_finish_metadata", side_effect=observe_finish):
            result = self.sweep()

        self.assertEqual(result["pruned"], [run["id"]], result)
        self.assertEqual(observed, [("delete", True), ("persist", True)])

    def test_run_lock_is_held_before_repository_lease(self):
        run, _workspace, _artifact = self.make_published_run("lock-order")
        original = artifact_publication.verified_published_run_artifact
        observed = []

        @contextmanager
        def observe(*args, **kwargs):
            with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                with self.store.locked_run(run["id"], blocking=False):
                    pass
            observed.append("run")
            with original(*args, **kwargs) as values:
                self.assertTrue(repository_lease_held())
                observed.append("repository")
                yield values

        with mock.patch.object(artifact_publication, "verified_published_run_artifact", side_effect=observe):
            result = self.sweep()

        self.assertEqual(result["pruned"], [run["id"]], result)
        self.assertEqual(observed, ["run", "repository"])

    def test_artifacts_directory_replacement_before_unlink_fails_closed(self):
        run, workspace, artifact = self.make_published_run("directory-swapped")
        original = artifact_publication.verify_source_artifact_fd
        old_artifacts = workspace / "artifacts-old"

        def swap_directory(source, source_fd, artifacts_fd, artifact_name):
            original(source, source_fd, artifacts_fd, artifact_name)
            (workspace / "artifacts").rename(old_artifacts)
            (workspace / "artifacts").mkdir()
            (workspace / "artifacts" / artifact_name).write_bytes(b"test deb payload")

        with mock.patch.object(artifact_publication, "verify_source_artifact_fd", side_effect=swap_directory):
            result = self.sweep()

        self.assertTrue(result["errors"])
        self.assertTrue((old_artifacts / artifact.name).is_file())
        self.assertTrue(artifact.is_file())
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))
        retry = self.sweep()
        self.assertTrue(retry["errors"])
        self.assertTrue((old_artifacts / artifact.name).is_file())
        self.assertTrue(artifact.is_file())
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))

    def test_stop_during_final_artifact_reverification_defers_unlink(self):
        run, workspace, artifact = self.make_published_run("stop-during-reverify")
        original = artifact_publication.verify_source_artifact_fd
        stop = threading.Event()

        def request_stop(source, source_fd, artifacts_fd, artifact_name):
            original(source, source_fd, artifacts_fd, artifact_name)
            stop.set()

        with mock.patch.object(artifact_publication, "verify_source_artifact_fd", side_effect=request_stop):
            result = self.sweep(should_stop=stop.is_set)

        self.assertNotIn(run["id"], result["pruned"])
        self.assertTrue(artifact.is_file())
        self.assertTrue((workspace / storage_pruning.PRUNING_INTENT).is_file())
        self.assertIsNone((self.store.load(run["id"])["artifact"]).get("pruning"))
        self.assertIn(run["id"], self.sweep()["pruned"])

    def test_run_directory_replacement_before_unlink_fails_closed(self):
        run, workspace, artifact = self.make_published_run("run-directory-swapped")
        original = artifact_publication.verify_source_artifact_fd
        moved_workspace = self.store.root / "detached-run"

        def swap_run_directory(source, source_fd, artifacts_fd, artifact_name):
            original(source, source_fd, artifacts_fd, artifact_name)
            workspace.rename(moved_workspace)
            workspace.mkdir()

        with mock.patch.object(artifact_publication, "verify_source_artifact_fd", side_effect=swap_run_directory):
            result = self.sweep()

        self.assertTrue(result["errors"])
        self.assertTrue((moved_workspace / "artifacts" / artifact.name).is_file())
        self.assertTrue((moved_workspace / storage_pruning.PRUNING_INTENT).is_file())
        self.assertFalse((workspace / "artifacts" / artifact.name).exists())

    def test_manifests_directory_replacement_after_intent_never_finalizes(self):
        run, workspace = self.make_terminal_manifest_run("manifest-directory-swapped")
        original_write = storage_pruning.write_json
        old_manifests = workspace / "manifests-old"

        def swap_directory(fd, name, value):
            original_write(fd, name, value)
            if name == storage_pruning.STAGING_PRUNING_INTENT:
                (workspace / "manifests").rename(old_manifests)
                (workspace / "manifests").mkdir()

        with mock.patch.object(storage_pruning, "write_json", side_effect=swap_directory):
            first = self.sweep()

        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in first["errors"]))
        self.assertTrue((old_manifests / "staging-files.json").is_file())
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertIn("content_manifest", staging["details"])
        retry = self.sweep()
        self.assertTrue(any(row.get("scope") == "staging_manifest" for row in retry["errors"]))
        staging = next(step for step in self.store.load(run["id"])["steps"] if step["name"] == "staging")
        self.assertIn("content_manifest", staging["details"])


if __name__ == "__main__":
    unittest.main()
