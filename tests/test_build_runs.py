import json
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from debbuilder import build_pipeline, execution_service
from debbuilder.build_models import RUN_STATUSES, STEP_NAMES
from debbuilder.build_store import BuildStore


def recipe():
    return {
        "name": "demo", "active": True,
        "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo package"},
        "source": {"repository": "owner/demo"},
    }


class BuildStoreTests(unittest.TestCase):
    def test_create_pipeline_run_persists_pending_run_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=False)
            workspace = Path(run["workspace"])

            self.assertEqual(run["status"], "pending")
            self.assertEqual(run["mode"], "build")
            self.assertTrue((workspace / "run.json").is_file())
            self.assertTrue((workspace / "recipe.json").is_file())
            self.assertTrue((workspace / "logs/pipeline.log").is_file())
            self.assertEqual((workspace / "logs/pipeline.log").read_text(), "")
            self.assertTrue(all(step["status"] == "pending" for step in run["steps"]))

    def test_invalid_recipe_is_rejected_before_run_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            invalid = recipe()
            invalid["package"]["name"] = "INVALID PACKAGE"

            with self.assertRaises(ValueError):
                build_pipeline.create_pipeline_run(invalid, store=store, dry_run=False)

            self.assertFalse(store.root.exists())

    def test_large_staging_inventory_is_externalized(self):
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
            with self.assertRaises(ValueError):
                store.staging_content_files(run["id"], {"content_manifest": "manifests/../../recipe.json"})

    def test_large_artifact_inventory_is_externalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="artifact-run")
            files = [{"path": f"./opt/demo/{index}.js", "size": str(index)} for index in range(2_000)]
            artifact = {"path": "artifacts/demo.deb", "sha256": "abc", "inspection": {"files": files, "file_count": len(files)}}
            stored = store.artifact_details_for_storage(run, artifact)
            self.assertNotIn("files", stored["inspection"])
            self.assertEqual(stored["inspection"]["files_manifest"], "manifests/artifact-files.json")
            self.assertEqual(store.artifact_files(run["id"], stored["inspection"]), files)
            with self.assertRaises(ValueError):
                store.artifact_files(run["id"], {"files_manifest": "manifests/../../recipe.json"})

    def test_create_makes_isolated_workspace_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            source = recipe()
            run = store.create(source, mode="dry_run", run_id="run-one")
            workspace = Path(run["workspace"])
            for name in ("source", "staging", "artifacts", "logs", "logs/commands"):
                self.assertTrue((workspace / name).is_dir())
            snapshot = json.loads((workspace / "recipe.json").read_text())
            self.assertEqual(snapshot["schema_version"], 3)
            self.assertNotIn("package_name", snapshot)
            source["package"]["name"] = "changed"
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

    def test_complete_run_mutations_are_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            store.create(recipe(), mode="build", run_id="concurrent-run")

            def append_validation(index):
                with store.locked_run("concurrent-run"):
                    current = store.load("concurrent-run")
                    current.setdefault("validations", []).append({"id": f"validation-{index}", "status": "success"})
                    store.save(current)

            threads = [threading.Thread(target=append_validation, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            stored = store.load("concurrent-run")
            self.assertEqual(len(stored["validations"]), 20)

    def test_build_run_has_the_complete_pending_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = BuildStore(Path(temporary)).create(recipe(), mode="dry_run")
            self.assertEqual([step["name"] for step in run["steps"]], list(STEP_NAMES))
            self.assertTrue(all(step["status"] == "pending" for step in run["steps"]))

    def test_log_slice_and_clear_history_preserve_lifecycle_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="log-run")
            artifact = Path(run["workspace"]) / "artifacts/demo.deb"
            artifact.write_bytes(b"deb")
            run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc"}, "validations": [{"status": "success"}], "publications": [{"status": "success"}]})
            run["steps"][4]["details"] = {"commands": [{"index": 1, "stdout": "hello", "stderr": "warn"}]}
            store.save(run)
            store.append_log_line("log-run", "first")
            store.append_log_line("log-run", "second")
            first = store.log_slice("log-run", 0)
            second = store.log_slice("log-run", first["text"].index("second"))
            self.assertIn("first", first["text"])
            self.assertIn("second", second["text"])
            stale_run = store.load("log-run")
            deleted = store.clear_log_history("log-run")
            cleaned = store.load("log-run")
            self.assertEqual(deleted["deleted"], "log_history")
            self.assertFalse(deleted["already_deleted"])
            self.assertTrue(store.execution_history_deletion_path("log-run").is_file())
            self.assertEqual(cleaned["status"], "success")
            self.assertEqual(cleaned["artifact"]["path"], str(artifact))
            self.assertEqual(cleaned["validations"][0]["status"], "success")
            self.assertEqual(cleaned["publications"][0]["status"], "success")
            self.assertEqual(cleaned["steps"][4]["details"]["commands"][0]["stdout"], "")
            self.assertTrue(cleaned["log_deleted"])
            store.save(stale_run)
            restarted_store = BuildStore(Path(temporary) / "builds")
            self.assertTrue(restarted_store.execution_history_deleted("log-run", restarted_store.load("log-run")))
            repeated = restarted_store.clear_log_history("log-run")
            self.assertTrue(repeated["already_deleted"])

    def test_log_completion_tracks_the_whole_execution_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="lifecycle-log")
            run["status"] = "success"
            run["artifact"] = {"path": str(Path(run["workspace"]) / "artifacts/demo.deb")}
            run["validations"] = [{"status": "running"}]
            store.save(run)
            self.assertFalse(execution_service.get_log(store, run["id"], verbosity="raw")["complete"])
            run["validations"][-1]["status"] = "success"
            run["publications"] = [{"status": "running"}]
            store.save(run)
            self.assertFalse(execution_service.get_log(store, run["id"], verbosity="raw")["complete"])
            run["publications"][-1]["status"] = "success"
            store.save(run)
            self.assertTrue(execution_service.get_log(store, run["id"], verbosity="raw")["complete"])

    def test_phase_three_records_real_source_and_detection_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text('{"scripts":{"build":"vite build"}}')
                return {"repository":"owner/demo","ref":"v1.2.0","tag":"v1.2.0","upstream_version":"1.2.0","debian_version":"1.2.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected+manual,"available":detected+manual,"missing":[],"checks":[],"installation_attempted":False}
            result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=True, acquire=acquire, dependency_check=available)
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
            result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=True, acquire=acquire)
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
            result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=True, acquire=acquire, dependency_check=missing)
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
            result = build_pipeline.run_pipeline(configured, store=store, dry_run=True, acquire=acquire, dependency_check=available)
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

    def test_existing_dry_run_executes_from_its_persisted_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}

            run = build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)
            result = build_pipeline.execute_pipeline_run(run["id"], store=store, acquire=acquire, dependency_check=available)

            self.assertEqual(result["status"], "prepared")
            self.assertEqual(store.load(run["id"])["mode"], "dry_run")
            self.assertEqual(result["steps"][4]["status"], "skipped")
            self.assertFalse(result["build"]["executed"])

    def test_existing_run_executes_the_immutable_recipe_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            repositories = []
            def acquire(snapshot, workspace, token=""):
                repositories.append(snapshot["source"]["repository"])
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":snapshot["source"]["repository"],"ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            submitted = recipe()
            run = build_pipeline.create_pipeline_run(submitted, store=store, dry_run=True)
            submitted["source"]["repository"] = "owner/changed-after-creation"

            result = build_pipeline.execute_pipeline_run(run["id"], store=store, acquire=acquire, dependency_check=available)

            self.assertEqual(result["status"], "prepared")
            self.assertEqual(repositories, ["owner/demo"])
            self.assertEqual(json.loads((Path(run["workspace"]) / "recipe.json").read_text())["source"]["repository"], "owner/demo")

    def test_existing_run_can_only_execute_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(Path(workspace) / "source")}
            run = build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)
            first = build_pipeline.execute_pipeline_run(run["id"], store=store, acquire=acquire)

            self.assertEqual(first["status"], "failed")
            with self.assertRaises(build_pipeline.PipelineRunError) as repeated:
                build_pipeline.execute_pipeline_run(run["id"], store=store)
            self.assertEqual(repeated.exception.as_dict(), {
                "code": "build_run_not_pending",
                "message": "Build Run cannot execute from status failed",
                "details": {"run_id": run["id"], "status": "failed"},
            })

    def test_only_pending_runs_are_eligible_for_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            refused_statuses = ("queued", "running", "cancelling", "prepared", "success", "failed", "cancelled")
            for status in refused_statuses:
                with self.subTest(status=status):
                    run = store.create(recipe(), mode="build", run_id=f"run-{status}")
                    run["status"] = status
                    store.save(run)
                    with self.assertRaises(build_pipeline.PipelineRunError) as refused:
                        build_pipeline.execute_pipeline_run(run["id"], store=store)
                    self.assertEqual(refused.exception.code, "build_run_not_pending")
                    self.assertEqual(refused.exception.details, {"run_id": run["id"], "status": status})

    def test_execute_unknown_run_has_a_structured_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            with self.assertRaises(build_pipeline.PipelineRunError) as missing:
                build_pipeline.execute_pipeline_run("missing-run", store=store)
            self.assertEqual(missing.exception.as_dict(), {
                "code": "build_run_not_found",
                "message": "Build Run was not found",
                "details": {"run_id": "missing-run"},
            })

    def test_historical_run_without_future_state_fields_remains_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            historical = store.create(recipe(), mode="build", run_id="historical-run")
            historical["status"] = "cancelled"
            store.save(historical)

            loaded = BuildStore(store.root).load("historical-run")
            self.assertEqual(loaded["status"], "cancelled")
            self.assertNotIn("queued_at", loaded)
            self.assertNotIn("cancelling_at", loaded)
            self.assertTrue({"queued", "cancelling"}.issubset(RUN_STATUSES))

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
            run = build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)
            self.assertEqual(run["status"], "pending")
            result = build_pipeline.execute_pipeline_run(run["id"], store=store, acquire=acquire, dependency_check=available)
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
