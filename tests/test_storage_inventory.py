import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import storage_inventory
from debbuilder.build_store import BuildStore


def recipe(name="storage"):
    return {
        "name": name,
        "package": {
            "name": name,
            "maintainer": "Storage <storage@example.test>",
            "description": "Storage fixture",
        },
        "source": {"repository": f"owner/{name}"},
    }


class StorageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data = self.base / "data"
        self.repo = self.base / "repo"
        self.data.mkdir()
        self.repo.mkdir()
        self.store = BuildStore(self.data / "builds")

    def make_run(self, run_id="run-one", *, mode="build", status="success"):
        run = self.store.create(recipe(run_id), mode=mode, run_id=run_id)
        root = Path(run["workspace"])
        (root / "source/payload").write_bytes(b"source")
        (root / "staging/payload").write_bytes(b"stage")
        (root / "downloads").mkdir(exist_ok=True)
        (root / "downloads/archive").write_bytes(b"download")
        (root / "source.tar.gz").write_bytes(b"tar")
        artifact = root / "artifacts/package.deb"
        artifact.write_bytes(b"artifact")
        (root / "manifests/files.json").write_bytes(b"manifest")
        commands = root / "validation/check/commands"
        commands.mkdir(parents=True)
        (commands / "001.json").write_bytes(b"command")
        (commands.parent / "previous.deb").write_bytes(b"previous")
        (root / "mystery.bin").write_bytes(b"unknown")
        run.update({"status": status, "artifact": {"path": str(artifact)}})
        self.store.save(run)
        return run, root

    def collect(self, repository=None):
        return storage_inventory.collect_storage_snapshot(
            self.data,
            repository or self.repo,
        )

    def test_classifies_known_storage_and_unknown_without_following_symlinks(self):
        _run, root = self.make_run()
        outside = self.base / "outside"
        outside.write_bytes(b"outside-secret")
        (root / "source-link").symlink_to(outside)

        result = self.collect()

        self.assertEqual(result["state"], "ready")
        self.assertGreater(result["categories"]["metadata"], 0)
        self.assertGreater(result["categories"]["logs_manifests"], 0)
        self.assertEqual(result["categories"]["artifacts"], len(b"artifact"))
        self.assertEqual(result["categories"]["validation_previous"], len(b"previous"))
        self.assertEqual(result["categories"]["disposable"], len(b"source") + len(b"stage") + len(b"download") + len(b"tar"))
        self.assertGreaterEqual(result["categories"]["unknown"], len(b"unknown"))
        self.assertEqual(result["runs"]["artifact_count"], 1)
        self.assertEqual(result["runs"]["artifact_bytes"], len(b"artifact"))

    def test_symlinked_configured_root_is_partial_without_fabricated_zero(self):
        actual = self.base / "actual-data"
        actual.mkdir()
        (actual / "secret").write_bytes(b"must-not-be-followed")
        linked = self.base / "linked-data"
        linked.symlink_to(actual, target_is_directory=True)

        result = storage_inventory.collect_storage_snapshot(linked, self.repo)

        self.assertEqual(result["state"], "partial")
        self.assertIsNone(result["bytes"]["data_root"])
        self.assertIsNone(result["bytes"]["managed_total"])
        self.assertTrue(any("cannot be opened safely" in row for row in result["diagnostics"]))

    def test_missing_configured_roots_are_partial_without_fabricated_zero(self):
        result = storage_inventory.collect_storage_snapshot(
            self.base / "missing-data", self.base / "missing-repository",
        )

        self.assertEqual(result["state"], "partial")
        self.assertIsNone(result["bytes"]["data_root"])
        self.assertIsNone(result["bytes"]["repository"])
        self.assertIsNone(result["bytes"]["managed_total"])

    def test_nested_and_external_repositories_are_never_double_counted(self):
        self.make_run()
        nested = self.data / "repository"
        nested.mkdir()
        (nested / "package.deb").write_bytes(b"repository")

        nested_result = self.collect(nested)
        self.assertTrue(nested_result["roots"]["repository_within_data"])
        self.assertEqual(nested_result["bytes"]["managed_total"], nested_result["bytes"]["data_root"])
        self.assertEqual(nested_result["bytes"]["repository"], len(b"repository"))

        (self.repo / "package.deb").write_bytes(b"external")
        external_result = self.collect(self.repo)
        self.assertFalse(external_result["roots"]["repository_within_data"])
        self.assertEqual(
            external_result["bytes"]["managed_total"],
            external_result["bytes"]["data_root"] + len(b"external"),
        )

    def test_repository_equal_to_data_root_is_scanned_and_counted_once(self):
        self.make_run()

        result = self.collect(self.data)

        self.assertTrue(result["roots"]["repository_within_data"])
        self.assertEqual(result["bytes"]["repository"], result["bytes"]["data_root"])
        self.assertEqual(result["bytes"]["managed_total"], result["bytes"]["data_root"])
        self.assertEqual(result["runs"]["count"], 1)

    def test_active_or_malformed_run_produces_partial_snapshot(self):
        active, _root = self.make_run("active", status="running")
        result = self.collect()
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["partial"])
        self.assertTrue(any(active["id"] in row for row in result["diagnostics"]))

        active["status"] = "success"
        self.store.save(active)
        (self.store.run_dir(active["id"]) / "run.json").write_text("{")
        malformed = self.collect()
        self.assertEqual(malformed["state"], "partial")
        self.assertTrue(any("metadata is unavailable" in row for row in malformed["diagnostics"]))

    def test_run_revision_change_during_walk_produces_partial_snapshot(self):
        _run, root = self.make_run("changing")
        original = storage_inventory._read_run_metadata

        def finish_mutation(data_root, run_id, scan):
            current = self.store.load(run_id)
            current["status"] = "running"
            self.store.save(current)
            (root / "source/payload").write_bytes(b"grown after accounting")
            current["status"] = "success"
            self.store.save(current)
            return original(data_root, run_id, scan)

        with mock.patch(
            "debbuilder.storage_inventory._read_run_metadata",
            side_effect=finish_mutation,
        ):
            result = self.collect()

        self.assertEqual(result["state"], "partial")
        self.assertTrue(any("changed while storage" in row for row in result["diagnostics"]))

    def test_nested_mount_boundary_is_partial_and_not_descended(self):
        mounted = self.data / "mounted"
        mounted.mkdir()
        (mounted / "outside-size").write_bytes(b"not-counted")
        with mock.patch("debbuilder.storage_inventory._mount_points", return_value={mounted.absolute()}):
            result = self.collect()
        self.assertEqual(result["state"], "partial")
        self.assertTrue(any("mount boundary" in row for row in result["diagnostics"]))
        self.assertEqual(result["bytes"]["data_root"], 0)

    def test_largest_runs_are_bounded_and_stats_use_canonical_metadata(self):
        for index in range(8):
            run, root = self.make_run(
                f"run-{index}",
                mode="dry_run" if index % 2 else "build",
                status="failed" if index == 7 else "success",
            )
            (root / "mystery.bin").write_bytes(b"x" * (index + 1))
        result = self.collect()
        self.assertEqual(result["runs"]["count"], 8)
        self.assertEqual(result["runs"]["by_mode"], {"build": 4, "dry_run": 4})
        self.assertEqual(result["runs"]["failed_count"], 1)
        self.assertEqual(result["runs"]["test_count"], 4)
        self.assertEqual(len(result["runs"]["largest"]), storage_inventory.MAX_LARGEST_RUNS)

    def test_cached_states_never_collect_from_snapshot_reader(self):
        inventory = storage_inventory.StorageInventory(self.data, self.repo, stale_after=0)
        with mock.patch("debbuilder.storage_inventory.collect_storage_snapshot") as collect:
            initial = inventory.snapshot()
        collect.assert_not_called()
        self.assertEqual(initial["state"], "collecting")

        ready = inventory.collect()
        self.assertIn(ready["state"], {"ready", "partial"})
        self.assertEqual(inventory.snapshot()["state"], "stale")

    def test_collection_error_is_error_then_stale_after_a_success(self):
        provider = mock.Mock(side_effect=RuntimeError("policy unavailable"))
        inventory = storage_inventory.StorageInventory(
            self.data, self.repo, policy_provider=provider,
        )
        self.assertEqual(inventory.collect()["state"], "error")

        provider.side_effect = None
        provider.return_value = {"enabled": True, "failed_workspaces_to_retain": 5}
        inventory.collect()
        provider.side_effect = RuntimeError("policy unavailable again")
        stale = inventory.collect()
        self.assertEqual(stale["state"], "stale")
        self.assertTrue(stale["partial"])


if __name__ == "__main__":
    unittest.main()
