import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from debbuilder import build_pipeline
from debbuilder.build_migrations import compact_large_run_payloads, migrate_staging_manifest
from debbuilder.build_models import STEP_NAMES
from debbuilder.build_store import BuildStore


def recipe():
    return {
        "name": "demo", "package_name": "demo", "github_repository": "owner/demo", "active": True,
        "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo package"},
    }


class BuildStoreTests(unittest.TestCase):
    def test_large_staging_inventory_is_externalized_and_legacy_inline_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="large-run")
            files = [f"node_modules/package-{index}/index.js" for index in range(60_000)]
            details = {
                "staging_directory": str(Path(run["workspace"]) / "staging"),
                "content_source": str(Path(run["workspace"]) / "source/node_modules"),
                "content_sources": [str(Path(run["workspace"]) / "source/node_modules")],
                "content_available": True,
                "content_files": files,
            }
            stored = store.staging_details_for_storage(run, details)
            run["steps"][5].update({"status": "success", "summary": "Staging prepared with 60,000 application files", "details": stored})
            store.save(run)
            persisted = json.loads((Path(run["workspace"]) / "run.json").read_text())
            persisted_details = persisted["steps"][5]["details"]
            self.assertNotIn("content_files", persisted_details)
            self.assertEqual(persisted_details["content_file_count"], 60_000)
            self.assertEqual(persisted_details["content_manifest"], "manifests/staging-files.json")
            self.assertEqual(persisted_details["content_source"], "source/node_modules")
            self.assertLess((Path(run["workspace"]) / "run.json").stat().st_size, 20_000)
            self.assertEqual(len(store.staging_content_files(run["id"], persisted_details)), 60_000)
            self.assertEqual(store.staging_content_files(run["id"], {"content_files": ["legacy/file"]}), ["legacy/file"])
            with self.assertRaises(ValueError):
                store.staging_content_files(run["id"], {"content_manifest": "manifests/../../recipe.json"})

    def test_migration_changes_only_legacy_staging_storage_representation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="legacy-run")
            run.update({"status": "success", "artifact": {"path": "artifacts/demo.deb", "sha256": "abc"}, "validations": [{"status": "success"}], "publications": [{"status": "failed"}]})
            step = run["steps"][5]
            step.update({"status": "success", "summary": "Staging prepared with 2 application files", "details": {"content_available": True, "content_files": ["a", "b"]}})
            store.save(run)
            lifecycle = {key: json.loads(json.dumps(run.get(key))) for key in ("status", "artifact", "validations", "publications", "created_at", "started_at", "finished_at", "duration")}
            result = migrate_staging_manifest(store, run["id"])
            migrated = store.load(run["id"])
            self.assertTrue(result["changed"])
            self.assertEqual(store.staging_content_files(run["id"], migrated["steps"][5]["details"]), ["a", "b"])
            self.assertNotIn("content_files", migrated["steps"][5]["details"])
            self.assertEqual({key: migrated.get(key) for key in lifecycle}, lifecycle)

    def test_large_artifact_inventory_is_externalized_and_legacy_inline_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="artifact-run")
            files = [{"path": f"./opt/demo/{index}.js", "size": str(index)} for index in range(2_000)]
            artifact = {"path": "artifacts/demo.deb", "sha256": "abc", "inspection": {"files": files, "file_count": len(files)}}
            stored = store.artifact_details_for_storage(run, artifact)
            self.assertNotIn("files", stored["inspection"])
            self.assertEqual(stored["inspection"]["files_manifest"], "manifests/artifact-files.json")
            self.assertEqual(store.artifact_files(run["id"], stored["inspection"]), files)
            self.assertEqual(store.artifact_files(run["id"], {"files": files}), files)
            with self.assertRaises(ValueError):
                store.artifact_files(run["id"], {"files_manifest": "manifests/../../recipe.json"})

    def test_payload_migration_preserves_full_results_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="compact-run")
            files = [{"path": f"./opt/demo/{index}.js"} for index in range(2_000)]
            artifact = {"path": "artifacts/demo.deb", "sha256": "abc", "inspection": {"files": files, "file_count": len(files)}}
            run.update({"status": "success", "artifact": artifact, "publications": [{"status": "success"}]})
            artifact_step = next(row for row in run["steps"] if row["name"] == "artifact")
            artifact_step.update({"status": "success", "details": json.loads(json.dumps(artifact))})
            full = {"index": 1, "stdout": "x" * 10_000, "stderr": "", "status": "success"}
            result_path = Path(run["workspace"]) / "validation/v1/commands/001.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps(full))
            run["validations"] = [{"id": "v1", "status": "success", "commands": [json.loads(json.dumps(full))]}]
            store.save(run)
            lifecycle = {key: json.loads(json.dumps(run.get(key))) for key in ("status", "publications", "recipe_sha256", "created_at")}
            result = compact_large_run_payloads(store, run["id"])
            migrated = result["run"]
            self.assertTrue(result["changed"])
            self.assertEqual({key: migrated.get(key) for key in lifecycle}, lifecycle)
            self.assertEqual(store.artifact_files(run["id"], migrated["artifact"]["inspection"]), files)
            command = migrated["validations"][0]["commands"][0]
            self.assertTrue(command["stdout_truncated"])
            self.assertEqual(command["stdout_characters"], 10_000)
            self.assertEqual(command["result_file"], "validation/v1/commands/001.json")
            self.assertEqual(json.loads(result_path.read_text())["stdout"], full["stdout"])

    def test_create_makes_isolated_workspace_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            source = recipe()
            run = store.create(source, mode="dry_run", run_id="run-one")
            workspace = Path(run["workspace"])
            for name in ("source", "staging", "artifacts", "logs", "logs/commands"):
                self.assertTrue((workspace / name).is_dir())
            snapshot = json.loads((workspace / "recipe.json").read_text())
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertNotIn("package_name", snapshot)
            source["package_name"] = "changed"
            self.assertEqual(json.loads((workspace / "recipe.json").read_text())["package"]["name"], "demo")
            self.assertEqual((workspace / "recipe.json").stat().st_mode & 0o777, 0o400)
            self.assertEqual((workspace / "run.json").stat().st_mode & 0o777, 0o600)

    def test_each_run_has_a_distinct_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            first = store.create(recipe(), mode="dry_run")
            second = store.create(recipe(), mode="dry_run")
            self.assertNotEqual(first["id"], second["id"])
            self.assertNotEqual(first["workspace"], second["workspace"])

    def test_build_run_has_the_complete_pending_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = BuildStore(Path(temporary)).create(recipe(), mode="dry_run")
            self.assertEqual([step["name"] for step in run["steps"]], list(STEP_NAMES))
            self.assertTrue(all(step["status"] == "pending" for step in run["steps"]))

    def test_phase_two_preparation_does_not_claim_pipeline_steps_succeeded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            result = build_pipeline.prepare_run(recipe(), store=store, dry_run=True)
            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "prepared")
            self.assertTrue(all(step["status"] == "pending" for step in persisted["steps"]))
            self.assertIn("Phase 3", store.log_text(result["run_id"]))

    def test_real_build_fails_explicitly_until_source_stage_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            result = build_pipeline.prepare_run(recipe(), store=store, dry_run=False)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["returncode"], 1)
            self.assertIn("Source stage", result["stderr"])

    def test_phase_three_records_real_source_and_detection_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text('{"scripts":{"build":"vite build"}}')
                return {"repository":"owner/demo","ref":"v1.2.0","tag":"v1.2.0","upstream_version":"1.2.0","debian_version":"1.2.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected+manual,"available":detected+manual,"missing":[],"checks":[],"installation_attempted":False}
            result = build_pipeline.run_source_detection(recipe(), store=store, dry_run=True, acquire=acquire, dependency_check=available)
            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "prepared")
            self.assertEqual(persisted["version"], {"upstream":"1.2.0","debian":"1.2.0-1"})
            self.assertEqual(persisted["steps"][0]["status"], "success")
            self.assertEqual(persisted["steps"][1]["status"], "success")
            self.assertEqual([step["status"] for step in persisted["steps"][:4]], ["success"] * 4)
            self.assertEqual(persisted["steps"][4]["status"], "skipped")
            self.assertEqual([step["status"] for step in persisted["steps"][5:]], ["success", "success", "skipped", "skipped", "skipped"])
            self.assertEqual(result["detection"]["proposed_commands"], ["npm install", "npm run build"])

    def test_detection_failure_stops_before_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(Path(workspace) / "source")}
            result = build_pipeline.run_source_detection(recipe(), store=store, dry_run=True, acquire=acquire)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "detection")
            self.assertEqual(result["error"]["code"], "project_not_detected")
            self.assertEqual(result["steps"][2]["status"], "pending")

    def test_missing_dependency_fails_before_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            def missing(detected, manual, **_kwargs):
                state = {"detected":detected,"manually_added":manual,"required":detected,"available":["nodejs"],"missing":["npm"],"checks":[],"installation_attempted":False}
                from debbuilder.dependency_checker import DependencyError
                raise DependencyError("missing_build_dependencies", "Missing required build dependencies: npm. Automatic installation is disabled.", details=state)
            result = build_pipeline.run_source_detection(recipe(), store=store, dry_run=True, acquire=acquire, dependency_check=missing)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "dependencies")
            self.assertEqual(result["dependencies"]["missing"], ["npm"])
            self.assertEqual(result["steps"][3]["status"], "pending")

    def test_source_changes_modify_only_run_source_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            external = Path(temporary) / "original.txt"
            external.write_text("old")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                (source / "app.txt").write_text(external.read_text())
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            configured = recipe()
            configured["build"] = {"source_changes":[{"operation":"replace","path":"app.txt","search":"old","content":"new"}]}
            result = build_pipeline.run_source_detection(configured, store=store, dry_run=True, acquire=acquire, dependency_check=available)
            self.assertEqual(result["source_changes"]["applied_count"], 1)
            self.assertEqual((Path(result["workspace"]) / "source/app.txt").read_text(), "new")
            self.assertEqual(external.read_text(), "old")
            self.assertIn("Source change 1/1", store.log_text(result["run_id"]))

    def test_dry_run_validates_detected_commands_without_executing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=True, acquire=acquire, dependency_check=available)
            self.assertEqual(result["steps"][4]["status"], "skipped")
            self.assertFalse(result["build"]["executed"])
            self.assertEqual(result["build"]["plan"]["selection"]["source"], "detection_proposal")
            self.assertEqual(result["build"]["plan"]["commands"][0]["arguments"], ["npm", "install"])
            self.assertEqual(list((Path(result["workspace"]) / "logs/commands").iterdir()), [])

    def test_real_build_records_commands_and_creates_an_inspected_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            configured = recipe()
            configured["build"] = {
                "commands":[
                    f"{shlex.quote(sys.executable)} -c 'import pathlib; pathlib.Path(\"first\").write_text(\"ok\")'",
                    f"{shlex.quote(sys.executable)} -c 'import pathlib; pathlib.Path(\"dist\").mkdir(); pathlib.Path(\"dist/result\").write_text(pathlib.Path(\"first\").read_text())'",
                ],
                "output":{"mode":"path","path":"dist"},
            }
            result = build_pipeline.run_pipeline(configured, store=store, dry_run=False, acquire=acquire, dependency_check=available)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["steps"][4]["status"], "success")
            self.assertEqual([step["status"] for step in result["steps"][5:]], ["success", "success", "skipped", "success", "success"])
            self.assertEqual(result["build"]["output"]["kind"], "directory")
            self.assertTrue(Path(result["artifact"]["path"]).is_file())
            self.assertEqual(result["artifact"]["inspection"]["package"], "demo")
            command_logs = sorted((Path(result["workspace"]) / "logs/commands").glob("*.json"))
            self.assertEqual([path.name for path in command_logs], ["001.json", "002.json"])
            recorded = json.loads(command_logs[1].read_text())
            self.assertEqual(recorded["index"], 2)
            self.assertEqual(recorded["status"], "success")


if __name__ == "__main__":
    unittest.main()
