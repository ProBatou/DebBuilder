from tests.lifecycle_helpers import clean_workspace
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from debbuilder import build_pipeline, execution_service, source_acquisition, validation_service, workspace_cleanup
from debbuilder.build_store import BuildStore
from debbuilder.command_containment import expected_control_group, starting_metadata
from debbuilder.command_containment import ContainmentError
from debbuilder.command_identity import persist_identity
from debbuilder.settings_store import default_settings, validate_settings
from debbuilder import storage


def recipe():
    return {"schema_version": 5, "name": "demo", "package": {"name": "demo", "maintainer": "Demo <demo@example.test>", "description": "Demo"}, "source": {"repository": "owner/demo"}}


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
        toolchain = root / "toolchain"
        (toolchain / "bin").mkdir(parents=True)
        (toolchain / "bin/node").write_text("run-local entry point")
        (toolchain / "home/.local/share/pnpm/store/v10/files").mkdir(parents=True)
        (toolchain / "home/.local/share/pnpm/store/v10/files/package").write_text("large pnpm store fixture")
        (toolchain / "corepack").mkdir()
        (toolchain / "corepack/state").write_text("recreatable")
        (toolchain / "npm-cache").mkdir()
        (toolchain / "npm-cache/index").write_text("recreatable")
        identity = {
            "schema_version": 1,
            "requested_node_range": "^22.19.0",
            "node": {
                "version": "22.21.1", "platform": "linux", "architecture": "x64",
                "archive": "node-v22.21.1-linux-x64.tar.xz", "sha256": "a" * 64,
                "source": "https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-x64.tar.xz",
            },
            "package_manager": {
                "name": "pnpm", "version": "10.24.0", "requested_range": "10.24.0",
                "integrity": "sha512-durable", "source": "https://registry.npmjs.org/pnpm/-/pnpm-10.24.0.tgz",
            },
        }
        (toolchain / "manifest.json").write_text(json.dumps(identity))
        artifact = root / "artifacts/demo.deb"
        artifact.write_bytes(b"final deb")
        run.update({
            "status": status,
            "artifact": {"path": str(artifact)},
            "finished_at": "2026-09-05T12:00:00+00:00",
            "toolchain": identity,
        })
        next(step for step in run["steps"] if step["name"] == "dependencies")["details"]["toolchain"] = identity
        self.store.save(run)
        self.store.append_log_line(run_id, "persistent log")
        self.store.save_manifest(run_id, "manifests/staging-files.json", ["demo"])
        return run, root

    def save_validation_attempt(self, run: dict, attempt_id: str, status: str) -> None:
        root = validation_service.attempt_root(self.store, run["id"], attempt_id)
        root.mkdir(parents=True, exist_ok=True)
        storage.save_json(root / "automation.json", {
            "automatic": False,
            "publish_after_success": False,
            "publication_state": "not_requested",
        })
        active = status in {"running", "cancelling"}
        terminal = status in {"failed", "cancelled"}
        validation_service._save_attempt(root / "attempt.json", {
            "contract_version": 1,
            "id": attempt_id,
            "build_run_id": run["id"],
            "inputs": {"profile": "bookworm", "artifact": {
                "package": "demo", "version": "1.0-1", "architecture": "all",
                "size": 9, "sha256": "a" * 64,
            }, "previous_artifact": None},
            "selected_profile": {"name": "bookworm", "image": {
                "name": "debbuilder-validation:bookworm", "id": "sha256:" + "b" * 64, "digest": None,
            }},
            "created_at": "2026-09-14T10:00:00+00:00",
            "started_at": "2026-09-14T10:00:01+00:00" if active or terminal else None,
            "finished_at": "2026-09-14T10:00:02+00:00" if terminal else None,
            "status": status,
            "result": None,
            "error": ({
                "code": "validation_failed" if status == "failed" else "validation_cancelled",
                "message": "Validation did not complete",
            } if terminal else None),
        })

    @staticmethod
    def pressure_capacity(available, *, state="pressure", target=200):
        if state == "measurement_error":
            return {"builds": {
                "measurement_state": "error",
                "pressure_state": "measurement_error",
            }}
        return {"builds": {
            "measurement_state": "ready",
            "pressure_state": state,
            "available_bytes": available,
            "effective_target_bytes": target,
        }}

    def apply_pressure(self, candidate_ids, measurements, *, policy=None, authorization=None):
        values = iter(measurements)
        initial = next(values)
        measure = mock.Mock(side_effect=lambda: next(values))
        result = workspace_cleanup.apply_storage_pressure(
            self.store,
            candidate_ids,
            initial,
            measure,
            policy or {"enabled": True, "failed_workspaces_to_retain": len(candidate_ids)},
            authorization=authorization or workspace_cleanup.CleanupAuthorization(),
        )
        return result, measure

    def test_pressure_cleanup_ignores_normal_and_initial_measurement_error(self):
        run, root = self.make_run("pressure-not-active", status="failed")
        for capacity in (
            self.pressure_capacity(200, state="normal"),
            self.pressure_capacity(None, state="measurement_error"),
        ):
            with self.subTest(state=capacity["builds"]["pressure_state"]):
                result, measure = self.apply_pressure([run["id"]], [capacity])
                self.assertEqual(result["cleaned"], [])
                measure.assert_not_called()
                self.assertTrue((root / "source/large-data").is_file())

    def test_pressure_cleanup_overrides_retention_oldest_first_and_stops_at_target(self):
        roots = {}
        for index, run_id in enumerate(("newest", "oldest", "middle")):
            run, roots[run_id] = self.make_run(run_id, status="failed")
            run["finished_at"] = f"2026-09-0{3 - index}T12:00:00+00:00"
            self.store.save(run)
        retained = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 3},
        )

        result, measure = self.apply_pressure(
            retained["pressure_candidates"],
            [
                self.pressure_capacity(50),
                self.pressure_capacity(150),
                self.pressure_capacity(200, state="normal"),
            ],
        )

        self.assertEqual(
            [row["id"] for row in result["cleaned"]], ["middle", "oldest"],
        )
        # Dates above deliberately make middle the oldest and oldest second;
        # candidate input order is the newer-first retention order.
        self.assertEqual(measure.call_count, 2)
        self.assertTrue(result["target_reached"])
        self.assertEqual(result["final_state"], "normal")
        self.assertFalse((roots["middle"] / "source").exists())
        self.assertFalse((roots["oldest"] / "source").exists())
        self.assertTrue((roots["newest"] / "source").exists())
        marker = json.loads((roots["middle"] / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(marker["reason"], "storage_pressure")
        self.assertEqual(
            set(marker["removed"]),
            set(workspace_cleanup.RETENTION_DISPOSABLE_DIRECTORIES + workspace_cleanup.DISPOSABLE_FILES),
        )
        for root in (roots["middle"], roots["oldest"]):
            for name in ("run.json", "recipe.json", "logs", "artifacts"):
                self.assertTrue((root / name).exists())
            self.assertFalse((root / "toolchain").exists())

    def test_pressure_cleanup_stops_fail_closed_on_intermediate_measurement_error(self):
        first, first_root = self.make_run("first-pressure", status="failed")
        second, second_root = self.make_run("second-pressure", status="failed")
        first["finished_at"] = "2026-09-01T12:00:00+00:00"
        second["finished_at"] = "2026-09-02T12:00:00+00:00"
        self.store.save(first)
        self.store.save(second)

        result, measure = self.apply_pressure(
            [second["id"], first["id"]],
            [
                self.pressure_capacity(50),
                self.pressure_capacity(None, state="measurement_error"),
            ],
        )

        self.assertEqual([row["id"] for row in result["cleaned"]], [first["id"]])
        self.assertEqual(result["final_state"], "measurement_error")
        self.assertFalse(result["target_reached"])
        self.assertEqual(measure.call_count, 1)
        self.assertFalse((first_root / "source").exists())
        self.assertTrue((second_root / "source").exists())

    def test_pressure_cleanup_disabled_and_exhausted_candidates_preserve_other_data(self):
        run, root = self.make_run("pressure-exhausted", status="failed")
        global_cache = self.store.root.parent / "toolchains/node/cache"
        global_cache.mkdir(parents=True)
        (global_cache / "keep").write_text("immutable")
        disabled, disabled_measure = self.apply_pressure(
            [run["id"]], [self.pressure_capacity(50)],
            policy={"enabled": False, "failed_workspaces_to_retain": 1},
        )
        self.assertEqual(disabled["initial_state"], "pressure")
        self.assertEqual(disabled["cleaned"], [])
        disabled_measure.assert_not_called()

        result, measure = self.apply_pressure(
            [run["id"]],
            [self.pressure_capacity(50), self.pressure_capacity(80)],
        )
        self.assertEqual(result["final_state"], "pressure")
        self.assertFalse(result["target_reached"])
        self.assertEqual(measure.call_count, 1)
        self.assertEqual((global_cache / "keep").read_text(), "immutable")
        self.assertTrue((root / "run.json").is_file())
        self.assertTrue((root / "artifacts/demo.deb").is_file())
        self.assertTrue((root / "logs").is_dir())

    def test_pressure_cleanup_is_driven_only_by_builds_not_repository_projection(self):
        run, root = self.make_run("repository-pressure", status="failed")
        repository_only = self.pressure_capacity(300, state="normal")
        repository_only["repository"] = {
            "measurement_state": "ready",
            "pressure_state": "pressure",
            "same_as_builds": False,
        }
        result, measure = self.apply_pressure([run["id"]], [repository_only])
        self.assertEqual(result["cleaned"], [])
        measure.assert_not_called()
        self.assertTrue((root / "source").is_dir())

        shared = self.pressure_capacity(50)
        shared["repository"] = {
            "measurement_state": "ready",
            "pressure_state": "pressure",
            "same_as_builds": True,
        }
        result, measure = self.apply_pressure(
            [run["id"]],
            [shared, self.pressure_capacity(200, state="normal")],
        )
        self.assertEqual([row["id"] for row in result["cleaned"]], [run["id"]])
        self.assertEqual(measure.call_count, 1)
        marker_before = (root / workspace_cleanup.CLEANUP_MARKER).read_bytes()
        repeated, repeated_measure = self.apply_pressure(
            [run["id"]], [self.pressure_capacity(50)],
        )
        self.assertEqual(repeated["cleaned"], [])
        repeated_measure.assert_not_called()
        self.assertEqual(
            (root / workspace_cleanup.CLEANUP_MARKER).read_bytes(), marker_before,
        )

    def test_pressure_only_reconsiders_runs_retained_by_normal_cleanup(self):
        old, old_root = self.make_run("normally-evicted", status="failed")
        kept, kept_root = self.make_run("normally-retained", status="failed")
        old["finished_at"] = "2026-09-01T12:00:00+00:00"
        kept["finished_at"] = "2026-09-02T12:00:00+00:00"
        self.store.save(old)
        self.store.save(kept)
        normal = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 1},
        )
        self.assertEqual(normal["retained"], [kept["id"]])
        self.assertFalse((old_root / "source").exists())

        result, measure = self.apply_pressure(
            normal["pressure_candidates"],
            [self.pressure_capacity(50), self.pressure_capacity(200, state="normal")],
        )

        self.assertEqual([row["id"] for row in result["cleaned"]], [kept["id"]])
        self.assertEqual(measure.call_count, 1)
        self.assertEqual(
            json.loads((old_root / workspace_cleanup.CLEANUP_MARKER).read_text())["reason"],
            "retention",
        )
        self.assertEqual(
            json.loads((kept_root / workspace_cleanup.CLEANUP_MARKER).read_text())["reason"],
            "storage_pressure",
        )

    def test_mixed_retention_evicts_a_b_then_pressure_c_only(self):
        roots = {}
        for index, run_id in enumerate(("A", "B", "C", "D"), start=1):
            run, roots[run_id] = self.make_run(run_id, status="failed")
            run["finished_at"] = f"2026-09-0{index}T12:00:00+00:00"
            self.store.save(run)

        normal = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 2},
        )
        result, measure = self.apply_pressure(
            normal["pressure_candidates"],
            [self.pressure_capacity(50), self.pressure_capacity(200, state="normal")],
        )

        self.assertEqual(set(normal["retained"]), {"C", "D"})
        self.assertEqual([row["id"] for row in result["cleaned"]], ["C"])
        self.assertEqual(measure.call_count, 1)
        for run_id in ("A", "B"):
            self.assertEqual(
                json.loads((roots[run_id] / workspace_cleanup.CLEANUP_MARKER).read_text())["reason"],
                "retention",
            )
        self.assertEqual(
            json.loads((roots["C"] / workspace_cleanup.CLEANUP_MARKER).read_text())["reason"],
            "storage_pressure",
        )
        self.assertEqual(
            json.loads((roots["D"] / workspace_cleanup.CLEANUP_MARKER).read_text())["reason"],
            "terminal_run",
        )
        self.assertTrue((roots["D"] / "source").is_dir())

    def test_blocked_normal_cleanup_is_not_a_pressure_candidate(self):
        run, root = self.make_run("blocked-normal", status="failed")
        with self.store.locked_run(run["id"]) as fd:
            persist_identity(fd, starting_metadata(run["id"], "b" * 32))

        normal = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 1},
        )

        self.assertEqual(normal["retained"], [run["id"]])
        self.assertIn(run["id"], normal["skipped"])
        self.assertEqual(normal["pressure_candidates"], [])
        result, measure = self.apply_pressure(
            normal["pressure_candidates"], [self.pressure_capacity(50)],
        )
        self.assertEqual(result["candidates_considered"], 0)
        measure.assert_not_called()
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain").is_dir())

    def test_pressure_revalidates_each_selected_candidate_and_continues(self):
        blocked, blocked_root = self.make_run("activity-after-selection", status="failed")
        safe, safe_root = self.make_run("safe-after-blocked", status="failed")
        blocked["finished_at"] = "2026-09-01T12:00:00+00:00"
        safe["finished_at"] = "2026-09-02T12:00:00+00:00"
        self.store.save(blocked)
        self.store.save(safe)
        original_read = workspace_cleanup.read_run
        reads = 0

        def become_active(fd, root, run_id):
            nonlocal reads
            reads += 1
            run = original_read(fd, root, run_id)
            if reads == 3:
                self.assertEqual(run_id, blocked["id"])
                run["publications"] = [{"status": "running"}]
            return run

        with mock.patch(
            "debbuilder.workspace_cleanup.read_run", side_effect=become_active,
        ):
            result, measure = self.apply_pressure(
                [safe["id"], blocked["id"]],
                [self.pressure_capacity(50), self.pressure_capacity(200, state="normal")],
            )

        self.assertIn(blocked["id"], result["skipped"])
        self.assertEqual([row["id"] for row in result["cleaned"]], [safe["id"]])
        self.assertTrue((blocked_root / "source").is_dir())
        self.assertFalse((safe_root / "source").exists())
        self.assertEqual(measure.call_count, 1)

    def test_pressure_timestamp_fallback_unknown_and_run_id_order(self):
        specifications = (
            ("tie-b", "2026-09-02T12:00:00+00:00", None),
            ("unknown", None, "unknown"),
            ("known-old", "2026-09-01T12:00:00+00:00", None),
            ("fallback", "malformed", 2_000_000_000.0),
            ("tie-a", "2026-09-02T12:00:00+00:00", None),
        )
        roots = {}
        ids = []
        for run_id, finished_at, created_fallback in specifications:
            run, roots[run_id] = self.make_run(run_id, status="failed")
            run["finished_at"] = finished_at
            if created_fallback == "unknown":
                run["created_at_epoch"] = "invalid"
                run["created_at"] = "malformed"
            elif created_fallback is not None:
                run["created_at_epoch"] = created_fallback
                run["created_at"] = "malformed"
            self.store.save(run)
            ids.append(run_id)
        measurements = [self.pressure_capacity(50)]
        measurements.extend(self.pressure_capacity(100 + index) for index in range(4))
        measurements.append(self.pressure_capacity(200, state="normal"))

        result, measure = self.apply_pressure(ids, measurements)

        self.assertEqual(
            [row["id"] for row in result["cleaned"]],
            ["known-old", "tie-a", "tie-b", "fallback", "unknown"],
        )
        self.assertEqual(measure.call_count, 5)

    def test_pressure_marker_write_failure_stops_before_next_candidate(self):
        first, first_root = self.make_run("marker-first", status="failed")
        second, second_root = self.make_run("marker-second", status="failed")
        first["finished_at"] = "2026-09-01T12:00:00+00:00"
        second["finished_at"] = "2026-09-02T12:00:00+00:00"
        self.store.save(first)
        self.store.save(second)
        original_write = workspace_cleanup.write_json

        def fail_marker(fd, name, value):
            if name == workspace_cleanup.CLEANUP_MARKER:
                raise OSError("marker filesystem failure")
            return original_write(fd, name, value)

        with mock.patch(
            "debbuilder.workspace_cleanup.write_json", side_effect=fail_marker,
        ):
            result, measure = self.apply_pressure(
                [second["id"], first["id"]], [self.pressure_capacity(50)],
            )

        self.assertEqual(result["cleaned"], [])
        self.assertEqual(result["errors"][0]["id"], first["id"])
        self.assertIn("marker filesystem failure", result["errors"][0]["error"])
        measure.assert_not_called()
        self.assertFalse((first_root / "source").exists())
        self.assertFalse((first_root / workspace_cleanup.CLEANUP_MARKER).exists())
        self.assertTrue((second_root / "source").is_dir())

    def test_pressure_cleanup_reuses_recovery_identity_artifact_and_target_guards(self):
        cases = (
            "active", "recovery", "validation", "publication", "identity", "cgroup",
            "process", "artifact", "symlink", "mount",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                self.store = BuildStore(Path(temporary) / "builds")
                run, root = self.make_run(f"pressure-{case}", status="failed")
                patcher = nullcontext()
                if case == "active":
                    run["status"] = "running"
                    self.store.save(run)
                elif case == "recovery":
                    run["recovery"] = {"status": "pending"}
                    self.store.save(run)
                elif case == "validation":
                    self.save_validation_attempt(run, "pressure-active", "running")
                elif case == "publication":
                    run["publications"] = [{"status": "running"}]
                    self.store.save(run)
                elif case == "identity":
                    with self.store.locked_run(run["id"]) as fd:
                        persist_identity(fd, starting_metadata(run["id"], "a" * 32))
                elif case == "cgroup":
                    patcher = mock.patch(
                        "debbuilder.command_containment.matching_run_command_units",
                        return_value={"debbuilder-command.scope"},
                    )
                elif case == "process":
                    patcher = mock.patch(
                        "debbuilder.workspace_cleanup._require_unused_workspace",
                        side_effect=workspace_cleanup.WorkspaceBusyError(
                            "A process still uses this workspace",
                        ),
                    )
                elif case == "artifact":
                    run["artifact"]["path"] = str(root / "source/large-data")
                    self.store.save(run)
                elif case == "symlink":
                    outside = Path(temporary) / "outside"
                    (root / "source").rename(outside)
                    (root / "source").symlink_to(outside, target_is_directory=True)
                else:
                    patcher = mock.patch(
                        "debbuilder.workspace_cleanup.Path.read_text",
                        return_value=f"1 2 0:1 / {root}/source/mounted rw - ext4 /dev/example rw\n",
                    )
                with patcher:
                    result, measure = self.apply_pressure(
                        [run["id"]], [self.pressure_capacity(50)],
                    )
                self.assertEqual(result["cleaned"], [])
                measure.assert_not_called()
                self.assertFalse((root / workspace_cleanup.CLEANUP_MARKER).exists())

    def test_automatic_cleanup_preserves_history_metadata_logs_manifests_and_artifact(self):
        run, root = self.make_run()
        previous = root / "validation/check/previous.deb"
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"previous artifact")
        unknown = root / "unknown.bin"
        unknown.write_bytes(b"unknown")
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
        self.assertEqual(previous.read_bytes(), b"previous artifact")
        self.assertEqual(unknown.read_bytes(), b"unknown")
        self.assertIsNotNone(execution_service.get_execution(self.store, run["id"]))
        self.assertIn("persistent log", execution_service.get_log(self.store, run["id"], verbosity="raw")["text"])
        marker = json.loads((root / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(marker["reason"], "retention")
        self.assertIn("toolchain", marker["removed"])
        marker_before = (root / workspace_cleanup.CLEANUP_MARKER).read_bytes()
        self.assertEqual(workspace_cleanup.apply_retention(self.store)["cleaned"], [])
        self.assertEqual((root / workspace_cleanup.CLEANUP_MARKER).read_bytes(), marker_before)

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
            self.assertFalse((root / "toolchain").exists())
            self.assertTrue((root / "run.json").exists())
        retained_marker = json.loads((roots[-1] / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(retained_marker["reason"], "terminal_run")
        self.assertEqual(retained_marker["removed"], ["toolchain"])
        self.assertEqual(workspace_cleanup.apply_retention(BuildStore(self.store.root))["cleaned"], [])

    def test_retained_toolchain_cleanup_does_not_block_later_workspace_eviction(self):
        run, root = self.make_run("retained-then-evicted", status="failed")
        expected_identity = json.loads(json.dumps(run["toolchain"]))

        retained = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 1},
        )

        self.assertEqual(retained["retained"], [run["id"]])
        self.assertEqual(retained["cleaned"], [{
            "id": run["id"], "removed": ["toolchain"], "reason": "terminal_run",
        }])
        self.assertFalse((root / "toolchain").exists())
        for name in workspace_cleanup.RETENTION_DISPOSABLE_DIRECTORIES + workspace_cleanup.DISPOSABLE_FILES:
            self.assertTrue((root / name).exists())
        first_marker = json.loads((root / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(first_marker["reason"], "terminal_run")

        evicted = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 0},
        )

        self.assertEqual(evicted["retained"], [])
        self.assertEqual([row["id"] for row in evicted["cleaned"]], [run["id"]])
        self.assertEqual(
            set(evicted["cleaned"][0]["removed"]),
            set(workspace_cleanup.RETENTION_DISPOSABLE_DIRECTORIES + workspace_cleanup.DISPOSABLE_FILES),
        )
        for name in workspace_cleanup.DISPOSABLE_DIRECTORIES + workspace_cleanup.DISPOSABLE_FILES:
            self.assertFalse((root / name).exists())
        second_marker = json.loads((root / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(second_marker["reason"], "retention")
        self.assertEqual(self.store.load(run["id"])["toolchain"], expected_identity)
        self.assertTrue((root / "recipe.json").is_file())

    def test_terminal_statuses_dispose_run_toolchains_but_preserve_global_cache(self):
        roots = {}
        for status in ("success", "failed", "cancelled"):
            _run, roots[status] = self.make_run(status, status=status)
        global_cache = self.store.root.parent / "toolchains/node/linux-x64/22.21.1"
        global_cache.mkdir(parents=True)
        (global_cache / "manifest.json").write_text("shared immutable cache")

        result = workspace_cleanup.apply_retention(self.store)

        self.assertEqual(set(result["retained"]), {"failed", "cancelled"})
        for status, root in roots.items():
            self.assertFalse((root / "toolchain").exists())
            self.assertEqual((root / "source").exists(), status in {"failed", "cancelled"})
        self.assertEqual((global_cache / "manifest.json").read_text(), "shared immutable cache")

    def test_toolchain_provenance_and_failure_diagnostics_survive_cleanup(self):
        run, root = self.make_run("provenance", status="failed")
        error = {
            "stage": "build", "code": "build_command_failed",
            "message": "pnpm build failed", "details": {
                "failed_command": {"command": "pnpm build", "status": "failed", "exit_code": 2},
            },
        }
        run["error"] = error
        next(step for step in run["steps"] if step["name"] == "build").update({
            "status": "failed", "error": error, "summary": error["message"],
        })
        self.store.save(run)
        expected_identity = json.loads(json.dumps(run["toolchain"]))

        workspace_cleanup.apply_retention(self.store)

        durable = self.store.load(run["id"])
        self.assertFalse((root / "toolchain").exists())
        self.assertEqual(durable["toolchain"], expected_identity)
        self.assertEqual(
            next(step for step in durable["steps"] if step["name"] == "dependencies")["details"]["toolchain"],
            expected_identity,
        )
        detail = execution_service.get_execution(self.store, run["id"])
        self.assertEqual(detail["toolchain"]["node"]["requested_range"], "^22.19.0")
        self.assertEqual(detail["toolchain"]["node"]["version"], "22.21.1")
        self.assertEqual(detail["toolchain"]["package_manager"]["name"], "pnpm")
        self.assertEqual(detail["toolchain"]["package_manager"]["version"], "10.24.0")
        self.assertEqual(detail["diagnostic"]["code"], "build_command_failed")
        self.assertTrue((root / "recipe.json").is_file())

    def test_active_build_pending_validation_publication_and_steps_are_never_cleaned_or_deleted(self):
        for phase in ("pending", "queued", "running", "cancelling", "validation", "publication", "step"):
            with self.subTest(phase=phase):
                run, root = self.make_run(phase)
                if phase in {"pending", "queued", "running", "cancelling"}:
                    run["status"] = phase
                elif phase == "step":
                    run["steps"][4]["status"] = "running"
                elif phase == "validation":
                    self.save_validation_attempt(run, "active-validation", "running")
                else:
                    run[f"{phase}s"] = [{"status": "running"}]
                self.store.save(run)
                with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                    execution_service.delete_log(self.store, run["id"])
                self.assertTrue((root / "source/large-data").exists())
                self.assertTrue((root / "toolchain/home/.local/share/pnpm/store/v10/files/package").exists())
                self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())
        result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
        self.assertEqual(result["cleaned"], [])
        self.assertEqual(execution_service.delete_logs(self.store, all_runs=True, dry_run=True)["count"], 0)

    def test_manifest_backed_queued_validation_blocks_cleanup_and_history_deletion(self):
        run, root = self.make_run("manifest-queued")
        attempt_id = "queued-attempt"
        attempt_root = validation_service.attempt_root(self.store, run["id"], attempt_id)
        self.save_validation_attempt(run, attempt_id, "queued")

        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "active"):
            clean_workspace(self.store, run["id"])
        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "active"):
            execution_service.delete_log(self.store, run["id"])
        result = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 0},
        )
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain/home/.local/share/pnpm/store/v10/files/package").is_file())
        self.assertTrue((root / "logs/pipeline.log").is_file())

    def test_non_latest_running_validation_or_publication_denies_cleanup(self):
        run, root = self.make_run("earlier-validation")
        self.save_validation_attempt(run, "running", "running")
        self.save_validation_attempt(run, "later-failed", "failed")
        with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
            clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())

        run, root = self.make_run("earlier-publication")
        run["publications"] = [{"status": "running"}, {"status": "failed"}]
        self.store.save(run)
        with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
            clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())

    def test_terminal_recovery_blocker_preserves_disposable_workspace(self):
        run, root = self.make_run("recovery-blocked", status="failed")
        run["recovery"] = {
            "status": "blocked",
            "code": "execution_recovery_unresolved",
            "reason": "workload absence is not proven",
        }
        self.store.save(run)
        self.assertFalse((root / ".active-command.json").exists())

        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "recovery is unresolved"):
            clean_workspace(self.store, run["id"])
        result = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 0},
        )

        self.assertIn(run["id"], result["skipped"])
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain/home/.local/share/pnpm/store/v10/files/package").is_file())

    def test_terminal_run_with_active_systemd_identity_refuses_cleanup(self):
        run, root = self.make_run("active-systemd-cleanup")
        identity = starting_metadata(run["id"], "a" * 32)
        identity.update({
            "containment_state": "active",
            "invocation_id": "b" * 32,
            "control_group": expected_control_group(identity["unit_name"]),
        })
        with self.store.locked_run(run["id"]) as fd:
            persist_identity(fd, identity)
        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "Active command"):
            clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain").is_dir())

    def test_matching_unit_or_cgroup_without_identity_refuses_cleanup_and_history_deletion(self):
        run, root = self.make_run("namespace-cleanup")
        unit = starting_metadata(run["id"], "c" * 32)["unit_name"]
        with mock.patch(
            "debbuilder.command_containment.matching_run_command_units", return_value={unit},
        ):
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "unit/cgroup"):
                clean_workspace(self.store, run["id"])
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "unit/cgroup"):
                execution_service.delete_log(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain").is_dir())
        self.assertTrue((root / "logs/pipeline.log").is_file())

    def test_unverifiable_command_namespace_refuses_destructive_cleanup(self):
        run, root = self.make_run("namespace-unverifiable")
        with mock.patch(
            "debbuilder.command_containment.matching_run_command_units",
            side_effect=ContainmentError("inventory denied"),
        ):
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "inventory is unverifiable"):
                clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "toolchain").is_dir())

    def test_process_latched_probe_cleanup_failure_refuses_cleanup(self):
        run, root = self.make_run("probe-cleanup-blocked")
        with mock.patch(
            "debbuilder.command_containment.probe_cleanup_blocker",
            return_value="probe cgroup remains",
        ):
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "probe containment"):
                clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())

    def test_process_latched_runtime_cleanup_failure_refuses_cleanup(self):
        run, root = self.make_run("runtime-cleanup-blocked")
        with mock.patch(
            "debbuilder.command_containment.runtime_cleanup_blocker",
            return_value="runtime cgroup remains",
        ):
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "Runtime command"):
                clean_workspace(self.store, run["id"])
        self.assertTrue((root / "source/large-data").is_file())

    def test_global_recovery_blocker_denies_every_destructive_entrypoint(self):
        run, root = self.make_run("globally-blocked", status="success")
        authorization = workspace_cleanup.CleanupAuthorization({
            "code": "execution_recovery_unresolved",
            "message": "global recovery remains unresolved",
        })

        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "global recovery"):
            clean_workspace(
                self.store, run["id"], authorization=authorization,
            )
        with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "global recovery"):
            execution_service.delete_log(
                self.store, run["id"], authorization=authorization,
            )
        result = workspace_cleanup.apply_retention(
            self.store,
            {"failed_workspaces_to_retain": 0},
            authorization=authorization,
        )

        self.assertEqual(result["blocked"]["scope"], "global")
        self.assertTrue((root / "source/large-data").is_file())
        self.assertTrue((root / "logs/pipeline.log").is_file())

    def test_resolved_recovered_failed_run_is_eligible(self):
        run, root = self.make_run("recovery-resolved", status="failed")
        run["recovery"] = {
            "status": "resolved",
            "code": "execution_interrupted",
            "backend": "systemd_cgroup",
            "reason": "previous boot identity is gone",
            "resolved_at": "2026-09-05T12:00:00+00:00",
        }
        self.store.save(run)

        result = workspace_cleanup.apply_retention(
            self.store, {"failed_workspaces_to_retain": 0},
        )

        self.assertEqual([row["id"] for row in result["cleaned"]], [run["id"]])
        self.assertFalse((root / "source").exists())

    def test_precreated_run_workspace_is_not_cleaned_as_terminal(self):
        run = build_pipeline.create_pipeline_run(recipe(), store=self.store, dry_run=False)
        workspace = Path(run["workspace"])
        (workspace / "source/preserved").write_text("pending execution")

        result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})

        self.assertEqual(result["cleaned"], [])
        self.assertTrue((workspace / "source/preserved").is_file())
        with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
            execution_service.delete_log(self.store, run["id"])

    def test_dry_runs_clean_prepared_and_retain_recent_failures(self):
        _run, prepared = self.make_run("prepared", status="prepared", mode="dry_run")
        _run, failed = self.make_run("failed", status="failed", mode="dry_run")
        workspace_cleanup.apply_retention(self.store)
        self.assertFalse((prepared / "source").exists())
        self.assertFalse((prepared / "toolchain").exists())
        self.assertTrue((failed / "source").exists())
        self.assertFalse((failed / "toolchain").exists())

    def test_zero_retention_reclaims_failed_validation_publication_and_cancelled_runs(self):
        for phase in ("validation", "publication", "cancelled"):
            run, root = self.make_run(phase)
            if phase == "cancelled":
                run["status"] = "cancelled"
            elif phase == "validation":
                self.save_validation_attempt(run, "failed-validation", "failed")
            else:
                run[f"{phase}s"] = [{"status": "failed", "finished_at": "2026-09-05T12:01:00+00:00"}]
            self.store.save(run)
        self.assertEqual(len(workspace_cleanup.apply_retention(self.store)["retained"]), 3)
        result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
        self.assertEqual(len(result["cleaned"]), 3)
        self.assertEqual(len(execution_service.list_executions(self.store, lambda run: "demo")), 3)

    def test_pressure_thresholds_do_not_authorize_workspace_cleanup(self):
        run, root = self.make_run("pressure-is-observational", status="failed")
        result = workspace_cleanup.apply_retention(self.store, {
            "failed_workspaces_to_retain": 1,
            "pressure_minimum_free_bytes": 1,
            "pressure_minimum_free_percent": 1,
            "pressure_target_free_bytes": workspace_cleanup.MAX_SAFE_JSON_INTEGER,
            "pressure_target_free_percent": 99,
        })

        self.assertEqual(result["retained"], [run["id"]])
        for name in workspace_cleanup.RETENTION_DISPOSABLE_DIRECTORIES:
            self.assertTrue((root / name).exists())
        self.assertTrue((root / "source.tar.gz").exists())
        self.assertFalse((root / "toolchain").exists())
        marker = json.loads((root / workspace_cleanup.CLEANUP_MARKER).read_text())
        self.assertEqual(marker["reason"], "terminal_run")

    def test_missing_runs_keep_existing_validation_and_publication_errors(self):
        from debbuilder import artifact_publication, artifact_validation
        with self.assertRaises(artifact_validation.ValidationError) as validation:
            artifact_validation.validate_artifact(
                "missing", store=self.store, prepared_dependencies={},
                attempt_id="missing-run", registry_root=self.base / "registry",
            )
        self.assertEqual(validation.exception.code, "build_run_not_found")
        with self.assertRaises(artifact_publication.PublicationError) as publication:
            artifact_publication.publish_artifact("missing", store=self.store, repo_root=self.base / "repo", distribution="stable", component="main", confirm="")
        self.assertEqual(publication.exception.code, "build_run_not_found")

    def test_manual_delete_overrides_disabled_retention_and_clears_validation_output(self):
        run, root = self.make_run(status="failed")
        self.save_validation_attempt(run, "attempt-one", "failed")
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
        self.assertNotIn("validations", self.store.load(run["id"]))
        self.assertEqual(execution_service.list_executions(self.store, lambda run: "demo"), [])
        self.assertTrue(execution_service.delete_log(BuildStore(self.store.root), run["id"])["already_deleted"])

    def test_traversal_and_forged_runtime_workspace_are_refused(self):
        run, root = self.make_run()
        for run_id in (".", "..", "../run-one", "/tmp/outside"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                clean_workspace(self.store, run_id)
        run["workspace"] = str(self.base)
        self.store.save(run)
        with self.assertRaisesRegex(ValueError, "canonical builds root"):
            execution_service.delete_log(self.store, run["id"])
        self.assertTrue((root / "source/large-data").exists())
        self.assertFalse((self.base / ".workspace.lock").exists())

    def test_symlink_run_root_metadata_and_cleanup_target_are_refused(self):
        for target in ("run", "root", "source", "toolchain", "logs", "run.json", ".workspace.lock", "marker"):
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
                elif target == "toolchain":
                    self.assertEqual(
                        (outside / "home/.local/share/pnpm/store/v10/files/package").read_text(),
                        "large pnpm store fixture",
                    )
                elif target not in {"logs", "run.json"}:
                    self.assertEqual(outside.read_text(), "protected")

    def test_nested_symlink_is_unlinked_without_following_it(self):
        run, root = self.make_run()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("protected")
        (root / "source/link").symlink_to(outside, target_is_directory=True)
        clean_workspace(self.store, run["id"])
        self.assertEqual((outside / "keep").read_text(), "protected")

    def test_bind_mount_below_disposable_directory_is_refused(self):
        for target in ("source", "toolchain"):
            with self.subTest(target=target):
                run, root = self.make_run(f"mounted-{target}")
                mountinfo = f"1 2 0:1 / {root}/{target}/mounted rw - ext4 /dev/example rw\n"
                with mock.patch("debbuilder.workspace_cleanup.Path.read_text", return_value=mountinfo):
                    with self.assertRaisesRegex(ValueError, "Mounted workspace"):
                        clean_workspace(self.store, run["id"])
                self.assertTrue((root / target).is_dir())

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
                clean_workspace(self.store, run["id"])
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
                self.save_validation_attempt(current, "late-running", "running")
            return original_read(fd, build_root, run_id)
        with mock.patch("debbuilder.workspace_cleanup.read_run", side_effect=changed):
            result = workspace_cleanup.apply_retention(self.store)
        self.assertIn(run["id"], result["skipped"])
        self.assertTrue((root / "source/large-data").exists())

    def test_retention_stop_boundary_starts_no_further_candidate(self):
        _first, first_root = self.make_run("first")
        _second, second_root = self.make_run("second")
        stop = False
        original = workspace_cleanup._clean_locked

        def clean_one(*args, **kwargs):
            nonlocal stop
            result = original(*args, **kwargs)
            stop = True
            return result

        with mock.patch("debbuilder.workspace_cleanup._clean_locked", side_effect=clean_one):
            result = workspace_cleanup.apply_retention(
                self.store, should_stop=lambda: stop,
            )

        self.assertEqual(len(result["cleaned"]), 1)
        remaining = [root for root in (first_root, second_root) if (root / "source").exists()]
        self.assertEqual(len(remaining), 1)

    def test_malformed_run_does_not_block_independent_candidate(self):
        run, root = self.make_run("valid")
        malformed = self.store.root / "malformed"
        malformed.mkdir()
        (malformed / "run.json").write_text("{")

        result = workspace_cleanup.apply_retention(self.store)

        self.assertEqual([row["id"] for row in result["cleaned"]], [run["id"]])
        self.assertFalse((root / "source").exists())
        self.assertEqual(result["errors"][0]["id"], "malformed")

    def test_artifact_in_disposable_data_is_never_deleted(self):
        run, root = self.make_run()
        run["artifact"]["path"] = str(root / "source/large-data")
        self.store.save(run)
        with self.assertRaisesRegex(ValueError, "Final artifact"):
            execution_service.delete_log(self.store, run["id"])
        self.assertTrue((root / "source/large-data").exists())
        self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())

    def test_artifact_in_run_local_toolchain_blocks_terminal_disposal(self):
        run, root = self.make_run("toolchain-artifact", status="failed")
        artifact = root / "toolchain/home/final.deb"
        artifact.write_bytes(b"misplaced final artifact")
        run["artifact"]["path"] = str(artifact)
        self.store.save(run)

        result = workspace_cleanup.apply_retention(self.store)

        self.assertTrue(any(row["id"] == run["id"] for row in result["errors"]))
        self.assertTrue(artifact.is_file())
        self.assertTrue((root / "toolchain").is_dir())

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
                clean_workspace(BuildStore(self.store.root), run["id"])
            except workspace_cleanup.WorkspaceBusyError:
                results.append("busy")
        with self.store.locked_run(run["id"]):
            thread = threading.Thread(target=cleanup)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            code = "from pathlib import Path; import sys; from debbuilder.build_store import BuildStore; from tests.lifecycle_helpers import clean_workspace; from debbuilder.workspace_cleanup import WorkspaceBusyError\ntry: clean_workspace(BuildStore(Path(sys.argv[1])), sys.argv[2])\nexcept WorkspaceBusyError: sys.exit(0)\nsys.exit(1)"
            child = subprocess.run([sys.executable, "-c", code, str(self.store.root), run["id"]], timeout=5, capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(results, ["busy"])
        self.assertTrue((root / "source").exists())
        clean_workspace(self.store, run["id"])
        self.assertFalse((root / "source").exists())

    def test_failed_run_with_live_process_is_protected_until_process_exits(self):
        run, root = self.make_run(status="failed")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            cwd=root / "toolchain/home", stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(workspace_cleanup.WorkspaceBusyError, "process still uses"):
                execution_service.delete_log(self.store, run["id"])
            result = workspace_cleanup.apply_retention(self.store, {"failed_workspaces_to_retain": 0})
            self.assertIn(run["id"], result["skipped"])
            self.assertTrue((root / "source/large-data").exists())
            self.assertTrue((root / "toolchain/home").is_dir())
            self.assertFalse((root / workspace_cleanup.HISTORY_MARKER).exists())
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.assertIn("source", clean_workspace(self.store, run["id"])["removed"])

    def test_build_pipeline_holds_workspace_lease_even_before_running_status(self):
        configured = recipe()
        configured["source"].update({"tracking": "manual", "ref": "v1"})
        acquired = []

        def acquire(_recipe, workspace, token="", expected_identity=None):
            acquired.append(workspace)
            with self.assertRaises(workspace_cleanup.WorkspaceBusyError):
                clean_workspace(self.store, Path(workspace).name)
            raise source_acquisition.SourceError("test_failure", "test failure")
        result = build_pipeline.execute_pipeline_run(
            build_pipeline.create_pipeline_run(configured, store=self.store, dry_run=True)["id"],
            store=self.store, acquire=acquire,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "test_failure")
        self.assertEqual(len(acquired), 1)
        clean_workspace(self.store, result["run_id"])

    def test_already_absent_workspace_and_disabled_policy_are_safe(self):
        self.assertEqual(workspace_cleanup.apply_retention(self.store)["cleaned"], [])
        run, root = self.make_run()
        self.assertEqual(workspace_cleanup.apply_retention(self.store, {"enabled": False})["cleaned"], [])
        self.assertTrue((root / "source").exists())
        self.assertTrue((root / "toolchain").exists())
        clean_workspace(self.store, run["id"])
        self.assertEqual(clean_workspace(self.store, run["id"])["removed"], [])
        with self.assertRaises(FileNotFoundError):
            clean_workspace(self.store, "unknown")

    def test_policy_validates_types_and_does_not_change_recipe_schema(self):
        defaults = default_settings("https://repo.example.test", "stable", "main")
        self.assertEqual(defaults["workspace_cleanup"], workspace_cleanup.DEFAULT_POLICY)
        updated = validate_settings({"workspace_cleanup": {"failed_workspaces_to_retain": 0}}, defaults)
        self.assertEqual(updated["workspace_cleanup"], {
            **workspace_cleanup.DEFAULT_POLICY,
            "failed_workspaces_to_retain": 0,
        })
        for policy in ({"enabled": "false"}, {"failed_workspaces_to_retain": True}, {"failed_workspaces_to_retain": -1}, {"failed_workspaces_to_retain": 1.5}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_settings({"workspace_cleanup": policy}, defaults)
