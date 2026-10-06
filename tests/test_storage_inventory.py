import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from debbuilder import storage_inventory, workspace_cleanup
from debbuilder.build_store import BuildStore


def recipe(name="storage"):
    return {
        "schema_version": 5,
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
        (root / "toolchain/home/.local/share/pnpm/store/v10").mkdir(parents=True)
        (root / "toolchain/home/.local/share/pnpm/store/v10/package").write_bytes(b"pnpm-store")
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

    @staticmethod
    def write_cleanup_marker(root, run_id, *, reason="retention",
                             cleaned_at="2026-09-08T12:00:00+00:00",
                             removed=None, **extra):
        marker = {
            "id": run_id,
            "reason": reason,
            "removed": ["source"] if removed is None else removed,
            "cleaned_at": cleaned_at,
            **extra,
        }
        (root / workspace_cleanup.CLEANUP_MARKER).write_text(json.dumps(marker))

    @staticmethod
    def pressure_policy(**overrides):
        return {
            **workspace_cleanup.DEFAULT_POLICY,
            "pressure_minimum_free_bytes": 100,
            "pressure_minimum_free_percent": 10,
            "pressure_target_free_bytes": 200,
            "pressure_target_free_percent": 20,
            **overrides,
        }

    def test_filesystem_measurement_uses_exact_statvfs_values_and_bavail(self):
        capacity = SimpleNamespace(
            f_frsize=10,
            f_blocks=100,
            f_bfree=80,
            f_bavail=5,
        )
        with mock.patch.object(storage_inventory.os, "fstat", return_value=SimpleNamespace(st_dev=77)), \
                mock.patch.object(storage_inventory.os, "fstatvfs", return_value=capacity):
            measured = storage_inventory._filesystem_measurement(123, measured_scope="builds")

        self.assertEqual(measured["device_id"], 77)
        self.assertEqual(measured["total_bytes"], 1000)
        self.assertEqual(measured["used_bytes"], 200)
        self.assertEqual(measured["free_bytes"], 800)
        self.assertEqual(measured["available_bytes"], 50)
        self.assertEqual(measured["available_percent"], 5.0)
        self.assertEqual(measured["utilized_percent"], 20.0)
        state, start, target = storage_inventory.pressure_state_for_measurement(
            "normal", measured, self.pressure_policy(),
        )
        self.assertEqual((state, start, target), ("pressure", 100, 200))

    def test_effective_thresholds_use_integer_maximums(self):
        policy = self.pressure_policy(
            pressure_minimum_free_bytes=2_000,
            pressure_minimum_free_percent=10,
            pressure_target_free_bytes=2_500,
            pressure_target_free_percent=30,
        )
        self.assertEqual(
            storage_inventory.effective_pressure_thresholds(10_001, policy),
            (2_000, 3_001),
        )

    def test_pressure_state_cold_start_and_hysteresis(self):
        policy = self.pressure_policy()

        def measurement(available):
            return {
                "measurement_state": "ready",
                "total_bytes": 1000,
                "available_bytes": available,
            }

        # A cold start between start and target is normal, not pressure.
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "unknown", measurement(150), policy,
            )[0],
            "normal",
        )
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "normal", measurement(99), policy,
            )[0],
            "pressure",
        )
        display_only = measurement(99)
        display_only["available_percent"] = 99.99
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "normal", display_only, policy,
            )[0],
            "pressure",
        )
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "pressure", measurement(150), policy,
            )[0],
            "pressure",
        )
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "pressure", measurement(200), policy,
            )[0],
            "normal",
        )
        self.assertEqual(
            storage_inventory.pressure_state_for_measurement(
                "pressure", {"measurement_state": "error"}, policy,
            ),
            ("measurement_error", None, None),
        )

    def test_capacity_uses_data_parent_before_builds_exists_without_walk(self):
        scopes = []

        def measure(_fd, *, measured_scope):
            scopes.append(measured_scope)
            return {
                "measurement_state": "ready", "measured_scope": measured_scope,
                "device_id": 1, "total_bytes": 1000, "used_bytes": 100,
                "free_bytes": 900, "available_bytes": 900,
                "available_percent": 90.0, "utilized_percent": 10.0,
            }

        with mock.patch.object(storage_inventory, "_scan_root", side_effect=AssertionError("walk")) as walk:
            result = storage_inventory.collect_filesystem_capacity(
                self.data,
                self.repo,
                policy=self.pressure_policy(),
                measure_fd=measure,
            )

        walk.assert_not_called()
        self.assertEqual(scopes, ["data_parent", "repository"])
        self.assertEqual(result["builds"]["measured_scope"], "data_parent")
        self.assertTrue(result["repository"]["same_as_builds"])

    def test_repository_capacity_is_deduplicated_only_on_same_device(self):
        (self.data / "builds").mkdir()

        def capacities(devices):
            def measure(_fd, *, measured_scope):
                device = devices[measured_scope]
                return {
                    "measurement_state": "ready", "measured_scope": measured_scope,
                    "device_id": device, "total_bytes": 1000,
                    "used_bytes": 250, "free_bytes": 750,
                    "available_bytes": 700, "available_percent": 70.0,
                    "utilized_percent": 30.0,
                }
            return storage_inventory.collect_filesystem_capacity(
                self.data,
                self.repo,
                policy=self.pressure_policy(),
                measure_fd=measure,
            )

        same = capacities({"builds": 4, "repository": 4})
        self.assertTrue(same["repository"]["same_as_builds"])
        self.assertNotIn("total_bytes", same["repository"])

        separate = capacities({"builds": 4, "repository": 9})
        self.assertFalse(separate["repository"]["same_as_builds"])
        self.assertEqual(separate["repository"]["device_id"], 9)
        self.assertEqual(separate["repository"]["total_bytes"], 1000)

    def test_unverifiable_capacity_is_measurement_error(self):
        linked_data = self.base / "linked-data"
        linked_data.symlink_to(self.data, target_is_directory=True)

        result = storage_inventory.collect_filesystem_capacity(
            linked_data,
            self.repo,
            policy=self.pressure_policy(),
        )

        self.assertEqual(result["builds"]["measurement_state"], "error")
        self.assertEqual(result["builds"]["pressure_state"], "measurement_error")
        self.assertNotIn(str(self.base), result["builds"]["diagnostic"])

    def test_invalid_or_unrepresentable_capacity_fails_closed_and_closes_builds_fd(self):
        invalid = SimpleNamespace(f_frsize=0, f_blocks=100, f_bfree=80, f_bavail=70)
        with mock.patch.object(storage_inventory.os, "fstat", return_value=SimpleNamespace(st_dev=1)), \
                mock.patch.object(storage_inventory.os, "fstatvfs", return_value=invalid), \
                self.assertRaises(ValueError):
            storage_inventory._filesystem_measurement(123, measured_scope="builds")

        too_large = SimpleNamespace(
            f_frsize=1,
            f_blocks=workspace_cleanup.MAX_SAFE_JSON_INTEGER + 1,
            f_bfree=1,
            f_bavail=1,
        )
        with mock.patch.object(storage_inventory.os, "fstat", return_value=SimpleNamespace(st_dev=1)), \
                mock.patch.object(storage_inventory.os, "fstatvfs", return_value=too_large), \
                self.assertRaises(ValueError):
            storage_inventory._filesystem_measurement(123, measured_scope="builds")

        with mock.patch.object(storage_inventory, "directory_fd") as directory, \
                mock.patch.object(storage_inventory.os, "open", return_value=11), \
                mock.patch.object(storage_inventory.os, "close") as close:
            directory.return_value.__enter__.return_value = 10
            result = storage_inventory._measure_builds_filesystem(
                self.data,
                measure_fd=mock.Mock(side_effect=ValueError("invalid")),
            )
        self.assertEqual(result["measurement_state"], "error")
        close.assert_called_once_with(11)

    def test_inventory_preserves_hysteresis_across_real_capacity_refreshes(self):
        policy = self.pressure_policy()
        available_values = iter((150, 50, 150, 200))

        def builds(_root, **_kwargs):
            available = next(available_values)
            return {
                "measurement_state": "ready", "measured_scope": "builds",
                "device_id": 1, "total_bytes": 1000, "used_bytes": 100,
                "free_bytes": 900, "available_bytes": available,
                "available_percent": float(available) / 10,
                "utilized_percent": 10.0,
            }

        inventory = storage_inventory.StorageInventory(
            self.data, self.repo, policy_provider=lambda: policy,
        )
        with mock.patch.object(storage_inventory, "_measure_builds_filesystem", side_effect=builds), \
                mock.patch.object(
                    storage_inventory,
                    "_measure_repository_filesystem",
                    return_value={"measurement_state": "error", "pressure_state": "measurement_error"},
                ):
            states = [
                inventory.collect()["filesystems"]["builds"]["pressure_state"]
                for _ in range(4)
            ]

        self.assertEqual(states, ["normal", "pressure", "pressure", "normal"])

    def test_threshold_change_rebaselines_next_capacity_refresh(self):
        policy = self.pressure_policy()

        def builds(_root, **_kwargs):
            return {
                "measurement_state": "ready", "measured_scope": "builds",
                "device_id": 1, "total_bytes": 1000, "used_bytes": 100,
                "free_bytes": 900, "available_bytes": 150,
                "available_percent": 15.0, "utilized_percent": 10.0,
            }

        inventory = storage_inventory.StorageInventory(
            self.data, self.repo, policy_provider=lambda: policy,
        )
        with mock.patch.object(storage_inventory, "_measure_builds_filesystem", side_effect=builds), \
                mock.patch.object(
                    storage_inventory,
                    "_measure_repository_filesystem",
                    return_value={"measurement_state": "error", "pressure_state": "measurement_error"},
                ):
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "normal")
            policy["pressure_minimum_free_bytes"] = 160
            policy["pressure_target_free_bytes"] = 300
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "pressure")
            policy["pressure_minimum_free_bytes"] = 100
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "normal")

    def test_threshold_rebaseline_survives_an_initial_measurement_error(self):
        policy = self.pressure_policy()
        measurements = iter((
            {
                "measurement_state": "ready", "device_id": 1,
                "total_bytes": 1000, "available_bytes": 50,
            },
            {"measurement_state": "error", "pressure_state": "measurement_error"},
            {
                "measurement_state": "ready", "device_id": 1,
                "total_bytes": 1000, "available_bytes": 150,
            },
        ))
        inventory = storage_inventory.StorageInventory(
            self.data, self.repo, policy_provider=lambda: policy,
        )
        with mock.patch.object(
            storage_inventory, "_measure_builds_filesystem",
            side_effect=lambda *_args, **_kwargs: next(measurements),
        ), mock.patch.object(
            storage_inventory, "_measure_repository_filesystem",
            return_value={"measurement_state": "error", "pressure_state": "measurement_error"},
        ):
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "pressure")
            policy["pressure_target_free_bytes"] = 300
            self.assertEqual(
                inventory.collect_capacity()["builds"]["pressure_state"],
                "measurement_error",
            )
            self.assertEqual(inventory.collect_capacity()["builds"]["pressure_state"], "normal")

    def test_repository_error_and_pressure_do_not_change_builds_state(self):
        measurements = {
            "builds": {
                "measurement_state": "ready", "measured_scope": "builds",
                "device_id": 1, "total_bytes": 1000, "used_bytes": 100,
                "free_bytes": 900, "available_bytes": 700,
                "available_percent": 70.0, "utilized_percent": 10.0,
            },
            "repository": {
                "measurement_state": "ready", "measured_scope": "repository",
                "device_id": 2, "total_bytes": 1000, "used_bytes": 900,
                "free_bytes": 100, "available_bytes": 50,
                "available_percent": 5.0, "utilized_percent": 90.0,
            },
        }
        with mock.patch.object(
            storage_inventory, "_measure_builds_filesystem",
            return_value=measurements["builds"],
        ), mock.patch.object(
            storage_inventory, "_measure_repository_filesystem",
            return_value=measurements["repository"],
        ):
            separate = storage_inventory.collect_filesystem_capacity(
                self.data, self.repo, policy=self.pressure_policy(),
            )
        self.assertEqual(separate["builds"]["pressure_state"], "normal")
        self.assertEqual(separate["repository"]["pressure_state"], "pressure")

        with mock.patch.object(
            storage_inventory, "_measure_builds_filesystem",
            return_value=measurements["builds"],
        ), mock.patch.object(
            storage_inventory, "_measure_repository_filesystem",
            return_value={"measurement_state": "error", "pressure_state": "measurement_error"},
        ):
            failed = storage_inventory.collect_filesystem_capacity(
                self.data, self.repo, policy=self.pressure_policy(),
            )
        self.assertEqual(failed["builds"]["pressure_state"], "normal")
        self.assertEqual(failed["repository"]["pressure_state"], "measurement_error")

    def test_classifies_known_storage_and_unknown_without_following_symlinks(self):
        _run, root = self.make_run()
        global_cache = self.data / "toolchains/node/linux-x64/22.21.1"
        global_cache.mkdir(parents=True)
        (global_cache / "manifest.json").write_bytes(b"shared-cache")
        outside = self.base / "outside"
        outside.write_bytes(b"outside-secret")
        (root / "source-link").symlink_to(outside)

        result = self.collect()

        self.assertEqual(result["state"], "ready")
        self.assertGreater(result["categories"]["metadata"], 0)
        self.assertGreater(result["categories"]["logs_manifests"], 0)
        self.assertEqual(result["categories"]["artifacts"], len(b"artifact"))
        self.assertEqual(result["categories"]["validation_previous"], len(b"previous"))
        self.assertEqual(
            result["categories"]["disposable"],
            len(b"source") + len(b"stage") + len(b"download") + len(b"tar") + len(b"pnpm-store"),
        )
        self.assertEqual(result["categories"]["cache"], len(b"shared-cache"))
        self.assertGreaterEqual(result["categories"]["unknown"], len(b"unknown"))
        self.assertEqual(sum(result["categories"].values()), result["bytes"]["data_root"])
        self.assertEqual(result["runs"]["artifact_count"], 1)
        self.assertEqual(result["runs"]["artifact_bytes"], len(b"artifact"))

    def test_run_toolchain_cleanup_reduces_disposable_without_reclassifying_global_cache(self):
        run, _root = self.make_run("cleanup-accounting")
        global_cache = self.data / "toolchains/managers/pnpm/10.24.0"
        global_cache.mkdir(parents=True)
        (global_cache / "manifest.json").write_bytes(b"immutable-global-cache")
        before = self.collect()

        cleanup = workspace_cleanup.apply_retention(self.store)
        after = self.collect()

        self.assertEqual([row["id"] for row in cleanup["cleaned"]], [run["id"]])
        self.assertGreater(before["categories"]["disposable"], 0)
        self.assertEqual(after["categories"]["disposable"], 0)
        self.assertEqual(
            before["categories"]["cache"], after["categories"]["cache"],
        )
        self.assertEqual(after["categories"]["cache"], len(b"immutable-global-cache"))

    def test_projects_valid_cleanup_markers_and_their_exact_public_fields(self):
        expected = {
            "retention": ["source", "staging", "downloads", "source.tar.gz", "toolchain"],
            "storage_pressure": ["downloads"],
            "terminal_run": ["toolchain"],
        }
        for index, (reason, removed) in enumerate(expected.items()):
            run, root = self.make_run(f"cleanup-{index}")
            self.write_cleanup_marker(
                root, run["id"], reason=reason,
                cleaned_at=f"2026-09-0{index + 1}T12:00:00+02:00",
                removed=removed,
            )

        projected = self.collect()["recent_workspace_cleanups"]

        self.assertEqual(projected["total_marked"], 3)
        self.assertEqual(projected["omitted"], 0)
        by_reason = {row["reason"]: row for row in projected["entries"]}
        self.assertEqual(set(by_reason), set(expected))
        for reason, removed in expected.items():
            self.assertEqual(by_reason[reason]["removed"], removed)
            self.assertEqual(
                set(by_reason[reason]), {"run_id", "reason", "cleaned_at", "removed"},
            )
            self.assertTrue(by_reason[reason]["cleaned_at"].endswith("+00:00"))
        self.assertEqual(by_reason["terminal_run"]["cleaned_at"], "2026-09-03T10:00:00+00:00")

    def test_absent_cleanup_marker_is_normal_and_projects_no_entry(self):
        self.make_run("no-cleanup")

        result = self.collect()

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["recent_workspace_cleanups"], {
            "entries": [], "total_marked": 0, "omitted": 0,
        })

    def test_invalid_cleanup_markers_are_omitted_with_bounded_private_safe_diagnostics(self):
        invalid_markers = {
            "bad-json": "{",
            "bad-reason": {"reason": "secret-reason"},
            "bad-time": {"cleaned_at": "not-a-time"},
            "naive-time": {"cleaned_at": "2026-09-08T12:00:00"},
            "bad-removed-type": {"removed": "source"},
            "bad-removed-name": {"removed": ["/private/secret"]},
            "bad-pressure-target": {"reason": "storage_pressure", "removed": ["toolchain"]},
            "bad-terminal-target": {"reason": "terminal_run", "removed": ["source"]},
            "duplicate-removed": {"removed": ["source", "source"]},
            "bad-id": {"id": "another-run"},
            "extra-field": {"private_path": "/private/marker-secret"},
        }
        for name, change in invalid_markers.items():
            run, root = self.make_run(name)
            if isinstance(change, str):
                (root / workspace_cleanup.CLEANUP_MARKER).write_text(change)
            else:
                values = {
                    "id": run["id"], "reason": "retention", "removed": ["source"],
                    "cleaned_at": "2026-09-08T12:00:00+00:00", **change,
                }
                (root / workspace_cleanup.CLEANUP_MARKER).write_text(json.dumps(values))

        result = self.collect()

        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["runs"]["count"], len(invalid_markers))
        self.assertEqual(result["recent_workspace_cleanups"]["entries"], [])
        self.assertLessEqual(len(result["diagnostics"]), storage_inventory.MAX_DIAGNOSTICS)
        diagnostic = " ".join(result["diagnostics"])
        self.assertNotIn(str(self.base), diagnostic)
        self.assertNotIn("marker-secret", diagnostic)
        self.assertNotIn("secret-reason", diagnostic)

    def test_cleanup_marker_identity_must_be_a_safe_run_id(self):
        root = self.base / "unsafe-marker-id"
        root.mkdir()
        self.write_cleanup_marker(root, "unsafe marker id")

        with workspace_cleanup.directory_fd(root) as workspace_fd:
            with self.assertRaisesRegex(ValueError, "identity is invalid"):
                storage_inventory._read_workspace_cleanup_marker(
                    workspace_fd, "unsafe marker id",
                )

    def test_unsafe_or_oversized_cleanup_markers_are_not_read_or_projected(self):
        outside = self.base / "outside-marker"
        outside.write_text(json.dumps({
            "id": "symlink-marker", "reason": "retention", "removed": ["source"],
            "cleaned_at": "2026-09-08T12:00:00+00:00",
        }))
        _run, symlink_root = self.make_run("symlink-marker")
        (symlink_root / workspace_cleanup.CLEANUP_MARKER).symlink_to(outside)

        _run, hardlink_root = self.make_run("hardlink-marker")
        os.link(outside, hardlink_root / workspace_cleanup.CLEANUP_MARKER)

        _run, nonregular_root = self.make_run("nonregular-marker")
        (nonregular_root / workspace_cleanup.CLEANUP_MARKER).mkdir()

        _run, large_root = self.make_run("large-marker")
        (large_root / workspace_cleanup.CLEANUP_MARKER).write_bytes(
            b"x" * (storage_inventory.MAX_CLEANUP_MARKER_BYTES + 1)
        )

        result = self.collect()

        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["runs"]["count"], 4)
        self.assertEqual(result["recent_workspace_cleanups"]["total_marked"], 0)
        self.assertEqual(
            sum("cleanup marker is invalid" in row for row in result["diagnostics"]), 4,
        )

    def test_hidden_run_cleanup_marker_is_not_reexposed(self):
        run, root = self.make_run("hidden-cleanup")
        self.write_cleanup_marker(root, run["id"])
        self.store.clear_log_history(run["id"])

        result = self.collect()

        self.assertEqual(result["runs"]["count"], 0)
        self.assertEqual(result["recent_workspace_cleanups"]["entries"], [])
        self.assertFalse(any("cleanup marker" in row for row in result["diagnostics"]))

    def test_cleanup_projection_is_bounded_newest_first_with_run_id_tiebreaker(self):
        for index in range(23):
            run_id = f"bounded-{index:02d}"
            run, root = self.make_run(run_id)
            minute = 2 if index == 0 else 1
            self.write_cleanup_marker(
                root, run["id"], reason="storage_pressure",
                cleaned_at=f"2026-09-{index + 1:02d}T12:{minute:02d}:00+00:00",
            )
        for run_id in ("tie-a", "tie-b"):
            run, root = self.make_run(run_id)
            self.write_cleanup_marker(
                root, run["id"], reason="retention",
                cleaned_at="2026-10-01T00:00:00+00:00",
            )

        projected = self.collect()["recent_workspace_cleanups"]

        self.assertEqual(projected["total_marked"], 25)
        self.assertEqual(len(projected["entries"]), 20)
        self.assertEqual(projected["omitted"], 5)
        self.assertEqual([row["run_id"] for row in projected["entries"][:2]], ["tie-a", "tie-b"])
        dates = [row["cleaned_at"] for row in projected["entries"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_each_real_cleanup_writer_is_reflected_after_collection(self):
        retention_run, _retention_root = self.make_run(
            "writer-retention", status="success",
        )
        pressure_run, _pressure_root = self.make_run(
            "writer-pressure", status="failed",
        )
        terminal_run, _terminal_root = self.make_run(
            "writer-terminal", status="failed",
        )
        inventory = storage_inventory.StorageInventory(self.data, self.repo)

        normal = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 2},
        )
        pressure = workspace_cleanup.apply_storage_pressure(
            self.store,
            [pressure_run["id"]],
            {"builds": {
                "measurement_state": "ready",
                "pressure_state": "pressure",
                "available_bytes": 50,
                "effective_target_bytes": 200,
            }},
            lambda: {"builds": {
                "measurement_state": "ready",
                "pressure_state": "normal",
                "available_bytes": 200,
                "effective_target_bytes": 200,
            }},
            self.pressure_policy(failed_workspaces_to_retain=2),
        )
        projected = inventory.collect()["recent_workspace_cleanups"]

        self.assertEqual(
            {row["id"]: row["reason"] for row in normal["cleaned"]},
            {
                retention_run["id"]: "retention",
                pressure_run["id"]: "terminal_run",
                terminal_run["id"]: "terminal_run",
            },
        )
        self.assertEqual(
            pressure["cleaned"],
            [{
                "id": pressure_run["id"],
                "reason": "storage_pressure",
                "removed": ["source", "staging", "downloads", "source.tar.gz"],
            }],
        )
        self.assertEqual(projected["total_marked"], 3)
        by_reason = {row["reason"]: row for row in projected["entries"]}
        self.assertEqual(set(by_reason), {"retention", "storage_pressure", "terminal_run"})
        self.assertEqual(by_reason["retention"]["run_id"], retention_run["id"])
        self.assertEqual(
            by_reason["retention"]["removed"],
            ["source", "staging", "downloads", "toolchain", "source.tar.gz"],
        )
        self.assertEqual(by_reason["storage_pressure"]["run_id"], pressure_run["id"])
        self.assertEqual(
            by_reason["storage_pressure"]["removed"],
            ["source", "staging", "downloads", "source.tar.gz"],
        )
        self.assertEqual(by_reason["terminal_run"]["run_id"], terminal_run["id"])
        self.assertEqual(by_reason["terminal_run"]["removed"], ["toolchain"])

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

    def test_capacity_remains_available_when_recursive_inventory_fails(self):
        inventory = storage_inventory.StorageInventory(
            self.data,
            self.repo,
            policy_provider=self.pressure_policy,
        )
        capacity = {
            "builds": {"measurement_state": "ready", "pressure_state": "normal"},
            "repository": {
                "measurement_state": "ready",
                "pressure_state": "normal",
                "same_as_builds": True,
            },
        }
        with mock.patch(
            "debbuilder.storage_inventory.collect_filesystem_capacity",
            return_value=capacity,
        ), mock.patch(
            "debbuilder.storage_inventory.collect_storage_snapshot",
            side_effect=OSError("recursive inventory unavailable"),
        ):
            failed = inventory.collect()

        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["filesystems"], capacity)
        self.assertEqual(inventory.snapshot()["filesystems"], capacity)

    def test_history_deletion_invalidates_count_without_losing_repository_bytes(self):
        run, _root = self.make_run("history-to-clear")
        (self.repo / "published.deb").write_bytes(b"published")
        inventory = storage_inventory.StorageInventory(self.data, self.repo)
        before = inventory.collect()
        self.assertEqual(before["runs"]["count"], 1)
        repository_bytes = before["bytes"]["repository"]

        self.store.clear_log_history(run["id"])
        inventory.invalidate()
        pending = inventory.snapshot()
        self.assertEqual(pending["state"], "collecting")
        self.assertIsNone(pending["runs"]["count"])
        self.assertIsNone(pending["bytes"]["managed_total"])
        after = inventory.collect()
        self.assertEqual(after["runs"]["count"], 0)
        self.assertEqual(after["bytes"]["repository"], repository_bytes)
        self.assertEqual(after["bytes"]["managed_total"], after["bytes"]["data_root"] + repository_bytes)
        # History is hidden, while an artifact left on disk is still measured.
        self.assertEqual(after["runs"]["artifact_bytes"], len(b"artifact"))
        self.assertEqual(inventory.snapshot()["runs"]["count"], 0)

    def test_retention_projection_reports_fixed_active_schedule(self):
        result = self.collect()

        self.assertEqual(result["retention_policy"]["pressure_minimum_free_bytes"], 536_870_912)
        self.assertEqual(result["retention_policy"]["pressure_minimum_free_percent"], 10)
        self.assertEqual(result["retention_policy"]["pressure_target_free_bytes"], 1_073_741_824)
        self.assertEqual(result["retention_policy"]["pressure_target_free_percent"], 15)
        self.assertTrue(result["retention_policy"]["startup_destructive_cleanup"])
        self.assertTrue(result["retention_policy"]["periodic_destructive_cleanup"])
        self.assertEqual(result["retention_policy"]["cleanup_interval_seconds"], 300)
        self.assertTrue(result["retention_policy"]["lifecycle_destructive_cleanup"])
        self.assertIn("toolchain", result["retention_policy"]["scope"])
        self.assertEqual(result["retention_policy"]["terminal_run_disposal_scope"], ["toolchain"])
        self.assertEqual(
            result["retention_policy"]["published_run_artifact_pruning"],
            "exact_repository_proof_required",
        )
        self.assertTrue(result["retention_policy"]["artifact_manifests_preserved"])
        self.assertTrue(
            result["retention_policy"]["terminal_staging_manifests_pruned_without_retained_workspace_evidence"]
        )

    def test_pruned_artifact_metadata_is_counted_separately_from_local_files(self):
        run, root = self.make_run()
        artifact = root / "artifacts/package.deb"
        artifact.unlink()
        run = self.store.load(run["id"])
        run["artifact"].update({
            "size": 123, "sha256": "a" * 64,
            "inspection": {"package": "package", "version": "1.0", "architecture": "all"},
        })
        run["artifact"]["pruning"] = {
            "schema": "debbuilder.artifact-pruning.v1",
            "pruning_version": 1,
            "status": "pruned",
            "reason": "duplicate_after_exact_publication",
            "pruned_at": "2026-09-09T10:00:00+00:00",
            "source": {
                "path": str(artifact), "name": artifact.name, "size": 123,
                "sha256": "a" * 64,
            },
            "publication": {
                "attempt_id": "publication",
                "proof": {
                    "schema": "debbuilder.repository-publication-proof.v1",
                    "proof_version": 1,
                    "verified_at": "2026-09-09T09:59:00+00:00",
                    "repository": {"root": str(self.repo), "device": 1, "inode": 2},
                    "distribution": {"requested": "bookworm", "codename": "bookworm", "suite": "stable"},
                    "component": "main",
                    "package": "package",
                    "version": "1.0",
                    "architecture": "all",
                    "source": {
                        "path": str(artifact), "size": 123, "sha256": "a" * 64,
                        "device": 3, "inode": 4,
                    },
                    "targets": [{
                        "database_architecture": "amd64",
                        "index": {
                            "path": "dists/bookworm/main/binary-amd64/Packages",
                            "device": 5,
                            "inode": 6,
                            "filename": "pool/main/p/package.deb",
                            "size": 123,
                            "sha256": "a" * 64,
                        },
                        "pool": {
                            "path": "pool/main/p/package.deb",
                            "size": 123,
                            "sha256": "a" * 64,
                            "device": 7,
                            "inode": 8,
                        },
                    }],
                },
            },
        }
        proof = run["artifact"]["pruning"]["publication"]["proof"]
        run["publications"] = [{
            "id": "publication", "build_run_id": run["id"], "status": "success",
            "artifact": str(artifact), "package": "package", "version": "1.0",
            "architecture": "all",
            "repository": {"root": str(self.repo), "distribution": "bookworm", "component": "main"},
            "proof": proof,
        }]
        self.store.save(run)

        result = self.collect()

        self.assertEqual(result["runs"]["artifact_count"], 0)
        self.assertEqual(result["runs"]["artifact_bytes"], 0)
        self.assertEqual(result["runs"]["pruned_artifact_count"], 1)
        self.assertEqual(result["runs"]["pruned_artifact_bytes"], 123)

        run = self.store.load(run["id"])
        publication = run.pop("publications")
        self.store.save(run)
        self.assertEqual(self.collect()["runs"]["pruned_artifact_count"], 0)
        run["publications"] = publication
        run["artifact"]["sha256"] = "b" * 64
        self.store.save(run)
        self.assertEqual(self.collect()["runs"]["pruned_artifact_count"], 0)

    def test_local_artifact_or_malformed_proof_never_counts_as_pruned(self):
        run, root = self.make_run("forged-pruning")
        artifact = root / "artifacts/package.deb"
        run = self.store.load(run["id"])
        run["artifact"]["pruning"] = {
            "schema": "debbuilder.artifact-pruning.v1",
            "pruning_version": 1,
            "status": "pruned",
            "reason": "duplicate_after_exact_publication",
            "pruned_at": "2026-09-09T10:00:00+00:00",
            "source": {
                "path": str(artifact), "name": artifact.name,
                "size": 123, "sha256": "a" * 64,
            },
            "publication": {"attempt_id": "publication", "proof": {}},
        }
        self.store.save(run)

        local = self.collect()
        self.assertEqual(local["runs"]["artifact_count"], 1)
        self.assertEqual(local["runs"]["pruned_artifact_count"], 0)
        artifact.unlink()
        malformed = self.collect()
        self.assertEqual(malformed["runs"]["artifact_count"], 0)
        self.assertEqual(malformed["runs"]["pruned_artifact_count"], 0)

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
