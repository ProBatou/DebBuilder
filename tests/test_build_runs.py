import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from debbuilder import build_pipeline, builtin_recipe, command_runner, execution_service, workspace_cleanup
from debbuilder.automation_identity import source_archive_identity
from debbuilder.build_models import RUN_STATUSES, STEP_NAMES
from debbuilder.build_store import BuildStore
from debbuilder.execution_cancellation import CancellationControl
from debbuilder.recipe_schema import RecipeDocumentError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def recipe():
    return {
        "schema_version": 5,
        "name": "demo", "active": True,
        "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo package"},
        "source": {"repository": "owner/demo", "tracking": "manual", "ref": "v1.2.0"},
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

    def test_removed_recipe_alias_is_rejected_before_run_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            invalid = recipe()
            invalid["build"] = {"timeout": 120}

            with self.assertRaisesRegex(ValueError, r"\$\.build\.timeout"):
                build_pipeline.create_pipeline_run(invalid, store=store, dry_run=False)

            self.assertFalse(store.root.exists())

    def test_build_version_source_cannot_bypass_direct_run_admission(self):
        for admission in ("store", "pipeline"):
            with self.subTest(admission=admission), tempfile.TemporaryDirectory() as temporary:
                store = BuildStore(Path(temporary) / "builds")
                invalid = recipe()
                invalid["source"]["version"] = {"source": "build"}

                with self.assertRaises(RecipeDocumentError) as raised:
                    if admission == "store":
                        store.create(invalid, mode="build")
                    else:
                        build_pipeline.create_pipeline_run(invalid, store=store, dry_run=False)

                self.assertEqual(raised.exception.code, "unsupported_version_source")
                self.assertEqual(raised.exception.path, "$.source.version.source")
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
            self.assertEqual(len(store.load_manifest(run["id"], persisted_details["content_manifest"])), 60_000)
            with self.assertRaises(ValueError):
                store.load_manifest(run["id"], "manifests/../../recipe.json")

    def test_large_artifact_inventory_is_externalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="artifact-run")
            files = [{"path": f"./opt/demo/{index}.js", "size": str(index)} for index in range(2_000)]
            artifact = {"path": "artifacts/demo.deb", "sha256": "abc", "upstream_identity": {"internal": "seal"}, "inspection": {"files": files, "file_count": len(files)}}
            stored = store.artifact_details_for_storage(run, artifact)
            self.assertNotIn("upstream_identity", stored)
            self.assertIn("upstream_identity", artifact)
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
            self.assertEqual(snapshot["schema_version"], 5)
            self.assertEqual(snapshot["runtime_apt_repositories"], [])
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

            def append_event(index):
                with store.locked_run("concurrent-run"):
                    current = store.load("concurrent-run")
                    current.setdefault("events", []).append({"at": str(index), "level": "info", "message": f"event-{index}"})
                    store.save(current)

            threads = [threading.Thread(target=append_event, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            stored = store.load("concurrent-run")
            self.assertEqual(len(stored["events"]), 20)

    def test_current_run_schema_rejects_persisted_validation_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="validation-copy")
            for location in ("run", "artifact"):
                candidate = dict(run)
                if location == "run":
                    candidate["validations"] = []
                else:
                    candidate["artifact"] = {"validations": []}
                with self.subTest(location=location), self.assertRaisesRegex(ValueError, "must not persist Validation"):
                    store.save(candidate)

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
            run.update({"status": "success", "artifact": {"path": str(artifact), "sha256": "abc"}, "publications": [{"status": "success"}]})
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
            self.assertNotIn("validations", cleaned)
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
            store.save(run)
            projected = {**run, "_validation_attempts": [{"status": "running"}]}
            self.assertFalse(execution_service.get_log(store, run["id"], verbosity="raw", run=projected)["complete"])
            projected["_validation_attempts"][-1]["status"] = "success"
            run["publications"] = [{"status": "running"}]
            store.save(run)
            projected["publications"] = run["publications"]
            self.assertFalse(execution_service.get_log(store, run["id"], verbosity="raw", run=projected)["complete"])
            run["publications"][-1]["status"] = "success"
            store.save(run)
            projected["publications"] = run["publications"]
            self.assertTrue(execution_service.get_log(store, run["id"], verbosity="raw", run=projected)["complete"])

    def test_phase_three_records_real_source_and_detection_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "package.json").write_text('{"scripts":{"build":"vite build"}}')
                return {"repository":"owner/demo","ref":"v1.2.0","tag":"v1.2.0","upstream_version":"1.2.0","debian_version":"1.2.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected+manual,"available":detected+manual,"missing":[],"checks":[],"installation_attempted":False}
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)["id"],
                store=store, acquire=acquire, dependency_check=available,
            )
            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "prepared")
            self.assertEqual(persisted["version"], {"upstream":"1.2.0","debian":"1.2.0-1"})
            self.assertEqual(persisted["steps"][0]["status"], "success")
            self.assertEqual(persisted["steps"][1]["status"], "success")
            self.assertEqual([step["status"] for step in persisted["steps"][:4]], ["success"] * 4)
            self.assertEqual(persisted["steps"][4]["status"], "skipped")
            self.assertEqual([step["status"] for step in persisted["steps"][5:]], ["success", "success", "skipped", "skipped", "skipped"])
            self.assertEqual(result["detection"]["proposed_commands"], ["npm install", "npm run build"])

    def test_python_auxiliary_node_project_uses_run_local_toolchain_through_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            host_bin = root / "host-bin"
            host_bin.mkdir()
            host_selected = root / "host-tool-selected"

            def executable(path, source):
                path.write_text("#!/usr/bin/python3\n" + source)
                path.chmod(0o700)

            for name in ("node", "npm"):
                executable(
                    host_bin / name,
                    "from pathlib import Path\n"
                    f"Path({str(host_selected)!r}).write_text({name!r})\n"
                    "print('99.0.0')\n",
                )

            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "frontend").mkdir()
                (source / "pyproject.toml").write_text('[project]\nname = "demo"\n')
                (source / "frontend/package.json").write_text(json.dumps({
                    "packageManager": "npm@11.7.0",
                    "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
                }))
                return {
                    "repository": "owner/demo", "ref": "v1.2.0", "tag": "v1.2.0",
                    "upstream_version": "1.2.0", "debian_version": "1.2.0-1",
                    "source_directory": str(source),
                }

            configured = recipe()
            configured["build"] = {
                "node_project_directory": "frontend",
                "commands": ["npm run build"],
                "environment": {
                    "PATH": str(host_bin),
                    "HOME": str(root / "recipe-home"),
                    "npm_config_cache": str(root / "recipe-npm-cache"),
                    "COREPACK_HOME": str(root / "recipe-corepack"),
                    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "1",
                    "DEBBUILDER_TEST_MARKER": "hybrid-cp2",
                },
            }
            identity = {
                "node": {"version": "24.12.0"},
                "package_manager": {"name": "npm", "version": "11.7.0"},
            }
            prepared_environments = []

            def prepare_toolchain(_resolution, *, workspace, cache, checkpoint=None):
                run_root = Path(workspace) / "toolchain"
                bin_directory = run_root / "bin"
                home = run_root / "home"
                npm_cache = run_root / "npm-cache"
                corepack_home = run_root / "corepack"
                for directory in (bin_directory, home, npm_cache, corepack_home):
                    directory.mkdir(parents=True, exist_ok=True)
                executable(
                    bin_directory / "node",
                    "import os\n"
                    "from pathlib import Path\n"
                    "print('24.12.0')\n"
                    "print(f'node-executable={Path(__file__).resolve()}')\n"
                    "print(f'node-marker={os.environ.get(\"DEBBUILDER_TEST_MARKER\", \"\")}')\n",
                )
                executable(
                    bin_directory / "npm",
                    "import os\n"
                    "import subprocess\n"
                    "import sys\n"
                    "from pathlib import Path\n"
                    "if sys.argv[1:] == ['--version']:\n"
                    "    print('11.7.0')\n"
                    "    print(f'npm-executable={Path(__file__).resolve()}')\n"
                    "else:\n"
                    "    child = subprocess.run(['node', '--version'], check=False, capture_output=True, text=True)\n"
                    "    print(f'npm-executable={Path(__file__).resolve()}')\n"
                    "    for key in ('PATH', 'HOME', 'npm_config_cache', 'COREPACK_HOME', 'COREPACK_ENABLE_DOWNLOAD_PROMPT', 'DEBBUILDER_TEST_MARKER'):\n"
                    "        print(f'{key}={os.environ.get(key, \"\")}')\n"
                    "    print('child-output-begin')\n"
                    "    print(child.stdout, end='')\n"
                    "    print('child-output-end')\n"
                    "    sys.exit(child.returncode)\n",
                )
                environment = {
                    "PATH": f"{bin_directory}:{host_bin}:/usr/bin:/bin",
                    "HOME": str(home),
                    "npm_config_cache": str(npm_cache),
                    "COREPACK_HOME": str(corepack_home),
                    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
                }
                prepared_environments.append(environment)
                return {"identity": identity, "environment": environment}

            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"resolved": True}) as resolve,
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=prepare_toolchain) as prepare,
                patch.object(build_pipeline.node_toolchain.urllib.request, "urlopen", side_effect=AssertionError("offline Build attempted network acquisition")) as network,
                patch.object(
                    build_pipeline.debian_packaging, "prepare_staging",
                    side_effect=build_pipeline.debian_packaging.PackagingError(
                        "test_stop_after_build", "Controlled test stops after the real Build command",
                    ),
                ),
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)["id"],
                    store=store, acquire=acquire,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "staging")
            self.assertEqual(result["error"]["code"], "test_stop_after_build")
            self.assertEqual(result["detection"]["project_type"], "python")
            self.assertEqual(result["detection"]["auxiliary_node_project"]["project_directory"], "frontend")
            resolve.assert_called_once()
            self.assertEqual(resolve.call_args.args[0], {"node": ">=24 <25", "npm": "11.7.0"})
            self.assertEqual(resolve.call_args.kwargs["package_manager"], "npm")
            prepare.assert_called_once()
            network.assert_not_called()
            environment = prepared_environments[0]
            checks = {row["tool"]: row for row in result["dependencies"]["tool_checks"]}
            self.assertEqual(checks["node"]["path"], environment["PATH"].split(":", 1)[0] + "/node")
            self.assertEqual(checks["node"]["version"], "24.12.0")
            self.assertEqual(checks["npm"]["path"], environment["PATH"].split(":", 1)[0] + "/npm")
            self.assertEqual(checks["npm"]["version"], "11.7.0")
            command = result["build"]["commands"][0]
            self.assertEqual(command["status"], "success")
            output = command["stdout"]
            run_bin = Path(environment["PATH"].split(":", 1)[0])
            self.assertIn(f"npm-executable={run_bin / 'npm'}", output)
            self.assertIn(f"PATH={environment['PATH']}", output)
            self.assertIn(f"HOME={environment['HOME']}", output)
            self.assertIn(f"npm_config_cache={environment['npm_config_cache']}", output)
            self.assertIn(f"COREPACK_HOME={environment['COREPACK_HOME']}", output)
            self.assertIn("COREPACK_ENABLE_DOWNLOAD_PROMPT=0", output)
            self.assertIn("DEBBUILDER_TEST_MARKER=hybrid-cp2", output)
            self.assertIn(f"node-executable={run_bin / 'node'}", output)
            self.assertIn("node-marker=hybrid-cp2", output)
            self.assertFalse(host_selected.exists())
            self.assertEqual(store.load(result["run_id"])["toolchain"], identity)

    def test_builtin_self_build_uses_manifest_requirements_and_one_toolchain_environment(self):
        definition = builtin_recipe.load_builtin_definition()
        resolved = {
            "repository": "ProBatou/DebBuilder",
            "ref": "v1.2.3",
            "tag": "v1.2.3",
            "upstream_version": "1.2.3",
            "commit": "a" * 40,
            "ref_object_sha": "b" * 40,
            "release_id": 53,
        }
        expected_identity = source_archive_identity(definition, resolved, generated_release=True)
        manifest = json.loads((REPOSITORY_ROOT / "frontend/package.json").read_text())
        toolchain_environment = {
            "PATH": "/run/toolchain/bin:/usr/bin:/bin",
            "HOME": "/run/toolchain/home",
            "npm_config_cache": "/run/toolchain/npm-cache",
            "COREPACK_HOME": "/run/toolchain/corepack",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        }
        identity = {
            "node": {"version": "24.21.0"},
            "package_manager": {"name": "npm", "version": "11.20.0"},
        }
        dependency_environments = []
        build_environments = []

        def acquire(_recipe, _workspace, token="", expected_identity=None):
            return {
                **resolved,
                "debian_version": "1.2.3-1",
                "source_directory": str(REPOSITORY_ROOT),
                "upstream_identity": expected_identity,
            }

        def dependency_check(detected, manual, **kwargs):
            dependency_environments.append(kwargs["environment"])
            return {
                "detected": detected,
                "manually_added": manual,
                "required": detected + manual,
                "available": detected + manual,
                "missing": [],
                "available_tools": ["node", "npm"],
                "missing_tools": [],
                "tool_checks": [],
                "installation_attempted": False,
            }

        def execute_build(recipe, detection, source_directory, **kwargs):
            build_environments.append(kwargs["environment"])
            self.assertEqual(recipe["build"]["commands"], definition["build"]["commands"])
            self.assertEqual(detection["project_type"], "python")
            self.assertEqual(Path(source_directory), REPOSITORY_ROOT)
            return {
                "executed": True,
                "plan": {"commands": recipe["build"]["commands"]},
                "commands": [],
                "output": {"mode": "paths", "paths": []},
            }

        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run_id = build_pipeline.create_pipeline_run(
                definition,
                store=store,
                dry_run=False,
                manual_source_provenance=expected_identity,
            )["id"]
            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"resolved": True}) as resolve_toolchain,
                patch.object(build_pipeline.node_toolchain, "prepare", return_value={
                    "identity": identity, "environment": toolchain_environment,
                }) as prepare_toolchain,
                patch.object(build_pipeline.node_toolchain, "validate_prepared", return_value=toolchain_environment) as validate_toolchain,
                patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=dependency_check),
                patch.object(build_pipeline.build_executor, "execute_build", side_effect=execute_build),
                patch.object(
                    build_pipeline.debian_packaging,
                    "prepare_staging",
                    side_effect=build_pipeline.debian_packaging.PackagingError(
                        "test_stop_after_build", "Controlled test stops after pipeline integration proof",
                    ),
                ),
            ):
                result = build_pipeline.execute_pipeline_run(run_id, store=store, acquire=acquire)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "test_stop_after_build")
        self.assertEqual(result["detection"]["project_type"], "python")
        auxiliary = result["detection"]["auxiliary_node_project"]
        self.assertEqual(auxiliary["project_directory"], "frontend")
        self.assertEqual(auxiliary["manifest"], "frontend/package.json")
        self.assertEqual(auxiliary["node_version"], manifest["engines"]["node"])
        self.assertEqual(auxiliary["npm_version"], manifest["engines"]["npm"])
        self.assertEqual(auxiliary["package_manager_spec"], manifest["packageManager"])
        self.assertEqual(auxiliary["package_manager_version"], "11.20.0")
        resolve_toolchain.assert_called_once_with(
            {"node": manifest["engines"]["node"], "npm": "11.20.0"},
            package_manager="npm",
            checkpoint=resolve_toolchain.call_args.kwargs["checkpoint"],
        )
        prepare_toolchain.assert_called_once()
        self.assertEqual(validate_toolchain.call_count, 2)
        self.assertEqual(dependency_environments, [toolchain_environment])
        self.assertEqual(build_environments, [toolchain_environment])

    def test_missing_prepared_auxiliary_toolchain_fails_before_build_without_host_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            host_bin = root / "host-bin"
            host_bin.mkdir()
            host_selected = root / "host-tool-selected"

            def executable(path, source):
                path.write_text("#!/usr/bin/python3\n" + source)
                path.chmod(0o700)

            for name in ("node", "npm"):
                executable(
                    host_bin / name,
                    "from pathlib import Path\n"
                    f"Path({str(host_selected)!r}).write_text({name!r})\n"
                    "print('99.0.0')\n",
                )

            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "frontend").mkdir()
                (source / "pyproject.toml").write_text('[project]\nname = "demo"\n')
                (source / "frontend/package.json").write_text(json.dumps({
                    "packageManager": "npm@11.7.0",
                    "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
                }))
                return {
                    "repository": "owner/demo", "ref": "v1", "tag": "v1",
                    "upstream_version": "1.0", "debian_version": "1.0-1",
                    "source_directory": str(source),
                }

            identity = {
                "node": {"version": "24.12.0"},
                "package_manager": {"name": "npm", "version": "11.7.0"},
            }
            prepared = {}

            def prepare_toolchain(_resolution, *, workspace, cache, checkpoint=None):
                bin_directory = Path(workspace) / "toolchain/bin"
                bin_directory.mkdir(parents=True)
                executable(bin_directory / "node", "print('24.12.0')\n")
                executable(bin_directory / "npm", "print('11.7.0')\n")
                environment = {
                    "PATH": f"{bin_directory}:{host_bin}:/usr/bin:/bin",
                    "HOME": str(Path(workspace) / "toolchain/home"),
                    "npm_config_cache": str(Path(workspace) / "toolchain/npm-cache"),
                    "COREPACK_HOME": str(Path(workspace) / "toolchain/corepack"),
                    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
                }
                for name in ("home", "npm-cache", "corepack"):
                    (Path(workspace) / "toolchain" / name).mkdir()
                prepared.update({"identity": identity, "environment": environment})
                return prepared

            check_dependencies = build_pipeline.dependency_checker.check_dependencies

            def check_then_remove(*args, **kwargs):
                state = check_dependencies(*args, **kwargs)
                Path(prepared["environment"]["PATH"].split(":", 1)[0], "npm").unlink()
                return state

            configured = recipe()
            configured["build"] = {
                "node_project_directory": "frontend",
                "commands": ["npm run build"],
                "environment": {"PATH": str(host_bin)},
            }
            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"resolved": True}),
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=prepare_toolchain),
                patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=check_then_remove),
                patch.object(build_pipeline.build_executor, "execute_build", wraps=build_pipeline.build_executor.execute_build) as execute_build,
                patch.object(build_pipeline.node_toolchain.urllib.request, "urlopen", side_effect=AssertionError("offline Build attempted network acquisition")) as network,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)["id"],
                    store=store, acquire=acquire,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "build")
            self.assertEqual(result["error"]["code"], "prepared_node_toolchain_missing")
            execute_build.assert_not_called()
            network.assert_not_called()
            self.assertFalse(host_selected.exists())

    def test_auxiliary_node_resolution_failure_preserves_primary_and_auxiliary_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")

            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "frontend").mkdir()
                (source / "pyproject.toml").write_text('[project]\nname = "demo"\n')
                (source / "frontend/package.json").write_text(json.dumps({
                    "packageManager": "npm@11.7.0",
                    "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
                }))
                return {
                    "repository": "owner/demo", "ref": "v1", "tag": "v1",
                    "upstream_version": "1.0", "debian_version": "1.0-1",
                    "source_directory": str(source),
                }

            configured = recipe()
            configured["build"] = {"node_project_directory": "frontend"}
            failure = build_pipeline.node_toolchain.NodeToolchainError(
                "node_toolchain_acquisition_failed", "Node metadata unavailable",
                details={"source": "https://nodejs.invalid/dist/index.json"},
            )
            with patch.object(build_pipeline.node_toolchain, "resolve", side_effect=failure):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(configured, store=store, dry_run=True)["id"],
                    store=store, acquire=acquire,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "dependencies")
            self.assertEqual(result["error"]["code"], "node_toolchain_acquisition_failed")
            requirement = result["error"]["details"]["project_requirement"]
            self.assertEqual(requirement["project_type"], "python")
            self.assertEqual(requirement["auxiliary_node_project"]["project_directory"], "frontend")
            self.assertEqual(requirement["auxiliary_node_project"]["package_manager_spec"], "npm@11.7.0")

    def test_detection_failure_stops_before_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(Path(workspace) / "source")}
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)["id"],
                store=store, acquire=acquire,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "detection")
            self.assertEqual(result["error"]["code"], "project_not_detected")
            self.assertEqual(result["steps"][2]["status"], "pending")

    def test_missing_dependency_fails_before_source_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            def missing(detected, manual, **_kwargs):
                state = {"detected":detected,"manually_added":manual,"required":detected,"available":["nodejs"],"missing":["npm"],"checks":[],"installation_attempted":False}
                from debbuilder.dependency_checker import DependencyError
                raise DependencyError("missing_build_dependencies", "Missing required build dependencies: npm. Automatic installation is disabled.", details=state)
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)["id"],
                store=store, acquire=acquire, dependency_check=missing,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "dependencies")
            self.assertEqual(result["dependencies"]["missing"], ["npm"])
            self.assertEqual(result["steps"][3]["status"], "pending")

    def test_source_changes_modify_only_run_source_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            external = Path(temporary) / "original.txt"
            external.write_text("old")
            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                (source / "app.txt").write_text(external.read_text())
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            configured = recipe()
            configured["build"] = {"source_changes":[{"operation":"replace","path":"app.txt","search":"old","content":"new"}]}
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(configured, store=store, dry_run=True)["id"],
                store=store, acquire=acquire, dependency_check=available,
            )
            self.assertEqual(result["source_changes"]["applied_count"], 1)
            self.assertEqual((Path(result["workspace"]) / "source/app.txt").read_text(), "new")
            self.assertEqual(external.read_text(), "old")
            self.assertIn("Source change 1/1", store.log_text(result["run_id"]))

    def test_dry_run_validates_detected_commands_without_executing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                return {"repository":"owner/demo","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}
            available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(recipe(), store=store, dry_run=True)["id"],
                store=store, acquire=acquire, dependency_check=available,
            )
            self.assertEqual(result["steps"][4]["status"], "skipped")
            self.assertFalse(result["build"]["executed"])
            self.assertEqual(result["build"]["plan"]["selection"]["source"], "detection_proposal")
            self.assertEqual(result["build"]["plan"]["commands"][0]["arguments"], ["npm", "install"])
            self.assertEqual(list((Path(result["workspace"]) / "logs/commands").iterdir()), [])

    def test_existing_dry_run_executes_from_its_persisted_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
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
            def acquire(snapshot, workspace, token="", expected_identity=None):
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
            def acquire(_recipe, workspace, token="", expected_identity=None):
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

    def test_current_cancelled_run_remains_readable_without_optional_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), mode="build", run_id="cancelled-run")
            run["status"] = "cancelled"
            store.save(run)

            loaded = BuildStore(store.root).load("cancelled-run")
            self.assertEqual(loaded["status"], "cancelled")
            self.assertNotIn("queued_at", loaded)
            self.assertNotIn("cancelling_at", loaded)
            self.assertTrue({"queued", "cancelling"}.issubset(RUN_STATUSES))

    def test_real_build_records_commands_and_creates_an_inspected_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token="", expected_identity=None):
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

    def test_maintainerr_style_empty_directories_are_ensured_logged_staged_and_packaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")

            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "package.json").write_text("{}")
                (source / "packages/contracts/node_modules").mkdir(parents=True)
                return {
                    "repository": "Maintainerr/Maintainerr", "ref": "v3.29.0", "tag": "v3.29.0",
                    "upstream_version": "3.29.0", "debian_version": "3.29.0-1",
                    "source_directory": str(source),
                }

            available = lambda detected, manual, **_kwargs: {
                "detected": detected, "manually_added": manual, "required": detected + manual,
                "available": detected + manual, "missing": [], "checks": [], "installation_attempted": False,
            }
            configured = recipe()
            configured["build"] = {
                "commands": [
                    f"{shlex.quote(sys.executable)} -c 'from pathlib import Path; Path(\"node_modules\").mkdir(); Path(\"apps/server/dist\").mkdir(parents=True); Path(\"apps/server/dist/app.js\").write_text(\"built\")'",
                ],
                "ensure_directories": [
                    "packages/contracts/node_modules", "apps/server/node_modules",
                ],
                "output": {"mode": "paths", "paths": [
                    "node_modules", "apps/server/dist", "apps/server/node_modules",
                    "packages/contracts/node_modules",
                ]},
            }
            run = build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)
            result = build_pipeline.execute_pipeline_run(
                run["id"], store=store, acquire=acquire, dependency_check=available,
            )

            self.assertEqual(result["status"], "success")
            self.assertNotIn("mkdir -p", configured["build"]["commands"])
            ensured = result["build"]["ensure_directories"]
            self.assertEqual(ensured["requested"], 2)
            self.assertEqual(ensured["created"], 1)
            self.assertEqual(ensured["already_existed"], 1)
            log = store.log_text(run["id"])
            self.assertIn("packages/contracts/node_modules (already existed)", log)
            self.assertIn("apps/server/node_modules (created)", log)
            staging = Path(result["workspace"]) / "staging/opt/demo"
            self.assertTrue((staging / "packages/contracts/node_modules").is_dir())
            self.assertTrue((staging / "apps/server/node_modules").is_dir())
            inventory = {
                row["path"].rstrip("/")
                for row in store.artifact_files(run["id"], result["artifact"]["inspection"])
            }
            self.assertIn("./opt/demo/packages/contracts/node_modules", inventory)
            self.assertIn("./opt/demo/apps/server/node_modules", inventory)


class HybridLifecycleTests(unittest.TestCase):
    @staticmethod
    def _configured_recipe(name="hybrid", *, command="npm run build", output=None):
        configured = recipe()
        configured["name"] = name
        configured["package"] = {**configured["package"], "name": name}
        configured["build"] = {
            "node_project_directory": "frontend",
            "commands": [command],
            **({"output": output} if output else {}),
        }
        return configured

    @staticmethod
    def _acquire_hybrid(_recipe, workspace, token="", expected_identity=None):
        source = Path(workspace) / "source"
        (source / "frontend").mkdir(exist_ok=True)
        (source / "pyproject.toml").write_text('[project]\nname = "hybrid"\n')
        (source / "frontend/package.json").write_text(json.dumps({
            "packageManager": "npm@11.7.0",
            "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
        }))
        return {
            "repository": "owner/hybrid", "ref": "v1", "tag": "v1",
            "upstream_version": "1.0", "debian_version": "1.0-1",
            "source_directory": str(source),
        }

    @staticmethod
    def _identity(node="24.12.0", manager="npm", manager_version="11.7.0"):
        return {
            "schema_version": 1,
            "requested_node_range": ">=24 <25" if node.startswith("24.") else "^22.19.0",
            "node": {
                "version": node, "platform": "linux", "architecture": "x64",
                "archive": f"node-v{node}-linux-x64.tar.xz", "sha256": "a" * 64,
                "source": f"https://nodejs.invalid/v{node}/node.tar.xz",
            },
            "package_manager": {
                "name": manager, "version": manager_version,
                "requested_range": manager_version, "integrity": "sha512-fixture",
                "source": f"https://registry.invalid/{manager}.tgz",
            },
        }

    @staticmethod
    def _executable(path, source):
        path.write_text("#!/usr/bin/python3\n" + source)
        path.chmod(0o700)

    def _prepare(self, workspace, *, node="24.12.0", manager="npm", manager_version="11.7.0", manager_source=None):
        root = Path(workspace) / "toolchain"
        bin_directory = root / "bin"
        for directory in (bin_directory, root / "home", root / "npm-cache", root / "corepack"):
            directory.mkdir(parents=True, exist_ok=True)
        self._executable(bin_directory / "node", f"print({node!r})\n")
        self._executable(
            bin_directory / manager,
            manager_source or f"print({manager_version!r})\n",
        )
        identity = self._identity(node, manager, manager_version)
        (root / "manifest.json").write_text(json.dumps(identity))
        return {"identity": identity, "environment": {
            "PATH": f"{bin_directory}:/usr/bin:/bin",
            "HOME": str(root / "home"),
            "npm_config_cache": str(root / "npm-cache"),
            "COREPACK_HOME": str(root / "corepack"),
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        }}

    @staticmethod
    def _shared_cache(root):
        shared = Path(root) / "toolchains/node/linux-x64/24.12.0/manifest.json"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("immutable shared cache")
        return shared

    def _assert_terminal_toolchain_cleanup(self, store, result, shared):
        workspace = Path(result["workspace"])
        before = store.load(result["run_id"])
        identity = json.loads(json.dumps(before["toolchain"]))
        error = json.loads(json.dumps(before.get("error")))
        cancellation = json.loads(json.dumps(before.get("cancellation")))
        step_history = [(step["name"], step["status"]) for step in before["steps"]]

        self.assertIn(before["status"], {"success", "failed", "cancelled"})
        self.assertTrue((workspace / "toolchain").is_dir())
        self.assertTrue(shared.is_file())
        self.assertTrue((workspace / "run.json").is_file())
        if identity is not None:
            self.assertTrue(identity["node"]["sha256"])
            self.assertTrue(identity["package_manager"]["integrity"])

        cleanup = workspace_cleanup.apply_retention(store)

        self.assertEqual(cleanup["errors"], [])
        self.assertFalse((workspace / "toolchain").exists())
        self.assertEqual(shared.read_text(), "immutable shared cache")
        self.assertTrue((workspace / "run.json").is_file())
        durable = store.load(result["run_id"])
        self.assertEqual(durable["status"], before["status"])
        self.assertEqual(durable["toolchain"], identity)
        self.assertEqual(durable.get("error"), error)
        self.assertEqual(durable.get("cancellation"), cancellation)
        self.assertEqual(
            [(step["name"], step["status"]) for step in durable["steps"]],
            step_history,
        )

    @staticmethod
    def _payload_path_is_forbidden(path):
        forbidden = {"frontend", "node_modules", "toolchain", "node", "npm", "pnpm"}
        return any(component in forbidden for component in PurePosixPath(path).parts)

    @staticmethod
    def _available(detected, manual, **kwargs):
        tools = list(kwargs.get("tools") or [])
        return {
            "detected": detected, "manually_added": manual,
            "required": detected + manual, "available": detected + manual,
            "missing": [], "available_tools": tools,
            "missing_tools": [], "tool_checks": [], "checks": [],
            "installation_attempted": False,
        }

    def test_cancellation_before_node_resolution_never_enters_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            control = CancellationControl()
            detector = build_pipeline.project_detection.detect_project

            def detect_then_cancel(*args, **kwargs):
                detected = detector(*args, **kwargs)
                control.request_cancel(reason="hybrid_before_resolution")
                return detected

            with patch.object(build_pipeline.node_toolchain, "resolve") as resolve:
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                    store=store, acquire=self._acquire_hybrid, detector=detect_then_cancel,
                    cancellation_control=control,
                )

            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], "hybrid_before_resolution")
            self.assertEqual(persisted["cancellation"]["stage"], "detection")
            self.assertEqual(result["steps"][1]["status"], "cancelled")
            self.assertEqual(result["steps"][2]["status"], "pending")
            resolve.assert_not_called()

    def test_cancellation_during_preparation_cleans_run_local_and_preserves_shared_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            control = CancellationControl()
            shared = self._shared_cache(root)

            def cancel_during_prepare(_resolution, *, workspace, cache, checkpoint=None):
                (Path(workspace) / "toolchain/bin").mkdir(parents=True)
                control.request_cancel(reason="hybrid_during_prepare")
                checkpoint()

            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}),
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=cancel_during_prepare),
                patch.object(build_pipeline.build_executor, "execute_build") as execute_build,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                    store=store, acquire=self._acquire_hybrid, cancellation_control=control,
                )

            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], "hybrid_during_prepare")
            self.assertEqual(persisted["cancellation"]["stage"], "dependencies")
            execute_build.assert_not_called()
            self.assertTrue((Path(result["workspace"]) / "toolchain").is_dir())
            self._assert_terminal_toolchain_cleanup(store, result, shared)

    def test_cancellation_after_preparation_preserves_identity_and_never_starts_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            control = CancellationControl()
            shared = self._shared_cache(root)

            def cancel_in_dependency_check(*args, **kwargs):
                control.request_cancel(reason="hybrid_after_prepare")
                return self._available(args[0], args[1], **kwargs)

            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}) as resolve,
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=lambda _r, **kw: self._prepare(kw["workspace"])),
                patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=cancel_in_dependency_check),
                patch.object(build_pipeline.build_executor, "execute_build") as execute_build,
                patch.object(build_pipeline.node_toolchain.urllib.request, "urlopen", side_effect=AssertionError("late network access")) as network,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                    store=store, acquire=self._acquire_hybrid, cancellation_control=control,
                )

            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], "hybrid_after_prepare")
            self.assertEqual(persisted["cancellation"]["stage"], "dependencies")
            self.assertEqual(persisted["toolchain"]["node"]["version"], "24.12.0")
            resolve.assert_called_once()
            execute_build.assert_not_called()
            network.assert_not_called()
            self._assert_terminal_toolchain_cleanup(store, result, shared)

    def test_cancellation_during_npm_command_stops_command_and_skips_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            control = CancellationControl()
            shared = self._shared_cache(root)
            # Define the handler before installing it; the command emits the synchronization marker afterwards.
            manager_source = (
                "import signal, sys, time\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('11.7.0')\n"
                "    raise SystemExit(0)\n"
                "def stop(*_args):\n"
                "    print('npm-terminated', flush=True)\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "print('npm-ready', flush=True)\n"
                "while True: time.sleep(60)\n"
            )
            real_execute = build_pipeline.build_executor.execute_build

            def execute_with_synchronized_cancel(*args, **kwargs):
                def runner(command, **runner_kwargs):
                    pipeline_output = runner_kwargs.get("on_output")

                    def output(item):
                        if pipeline_output:
                            pipeline_output(item)
                        if "npm-ready" in item["text"]:
                            control.request_cancel(reason="hybrid_during_npm")

                    return command_runner.run_command(command, **{**runner_kwargs, "on_output": output})

                return real_execute(*args, runner=runner, **kwargs)

            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}),
                patch.object(
                    build_pipeline.node_toolchain, "prepare",
                    side_effect=lambda _r, **kw: self._prepare(kw["workspace"], manager_source=manager_source),
                ),
                patch.object(build_pipeline.build_executor, "execute_build", side_effect=execute_with_synchronized_cancel),
                patch.object(build_pipeline.debian_packaging, "prepare_staging") as prepare_staging,
                patch.object(build_pipeline.node_toolchain.urllib.request, "urlopen", side_effect=AssertionError("late network access")) as network,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                    store=store, acquire=self._acquire_hybrid, cancellation_control=control,
                )

            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], "hybrid_during_npm")
            self.assertEqual(persisted["cancellation"]["stage"], "build")
            command = result["build"]["commands"][0]
            self.assertEqual(command["status"], "cancelled")
            self.assertIn("npm-ready", command["stdout"])
            self.assertIn("npm-terminated", command["stdout"])
            self.assertIsNone(command["termination_error"])
            prepare_staging.assert_not_called()
            network.assert_not_called()
            self._assert_terminal_toolchain_cleanup(store, result, shared)

    def test_cancellation_after_build_never_starts_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            control = CancellationControl()
            shared = self._shared_cache(root)

            def complete_then_cancel(_recipe, _detection, source_directory, **_kwargs):
                control.request_cancel(reason="hybrid_after_build")
                return {
                    "executed": True, "plan": {"commands": ["npm run build"]}, "commands": [],
                    "output": {"mode": "source", "path": str(source_directory), "exists": True, "kind": "directory"},
                }

            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}),
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=lambda _r, **kw: self._prepare(kw["workspace"])),
                patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=self._available),
                patch.object(build_pipeline.build_executor, "execute_build", side_effect=complete_then_cancel),
                patch.object(build_pipeline.debian_packaging, "prepare_staging") as prepare_staging,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                    store=store, acquire=self._acquire_hybrid, cancellation_control=control,
                )

            persisted = store.load(result["run_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], "hybrid_after_build")
            self.assertEqual(persisted["cancellation"]["stage"], "build")
            self.assertEqual(result["steps"][4]["status"], "cancelled")
            self.assertEqual(result["steps"][5]["status"], "pending")
            prepare_staging.assert_not_called()
            self._assert_terminal_toolchain_cleanup(store, result, shared)

    def test_hybrid_resolution_acquisition_preparation_and_dependency_failures_are_persisted(self):
        cases = (
            ("resolution", "node_range_unsatisfied"),
            ("acquisition", "node_toolchain_acquisition_failed"),
            ("preparation", "node_toolchain_invalid"),
            ("dependency", "missing_build_dependencies"),
        )
        real_resolve = build_pipeline.node_toolchain.resolve

        for point, code in cases:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                store = BuildStore(Path(temporary) / "builds")

                def resolve(requirements, *, package_manager, checkpoint=None):
                    if point == "resolution":
                        return real_resolve(
                            requirements,
                            package_manager=package_manager,
                            releases=[{"version": "v23.0.0", "npm": "10.0.0", "files": ["linux-x64"]}],
                            manager_metadata={"versions": {}},
                            checkpoint=checkpoint,
                        )
                    if point == "acquisition":
                        def unavailable(*_args, **_kwargs):
                            raise OSError("fixture Node metadata unavailable")

                        return real_resolve(
                            requirements,
                            package_manager=package_manager,
                            opener=unavailable,
                            checkpoint=checkpoint,
                        )
                    return {"fixture": True}

                def prepare(_resolution, **kwargs):
                    if point == "preparation":
                        raise build_pipeline.node_toolchain.NodeToolchainError(
                            "node_toolchain_invalid",
                            "fixture prepared Node runtime is invalid",
                            details={"phase": "prepare"},
                        )
                    return self._prepare(kwargs["workspace"])

                def dependency(detected, manual, **kwargs):
                    if point == "dependency":
                        raise build_pipeline.dependency_checker.DependencyError(
                            "missing_build_dependencies",
                            "fixture dependency failed",
                            details={"missing": ["python3"]},
                        )
                    return self._available(detected, manual, **kwargs)

                with (
                    patch.object(build_pipeline.node_toolchain, "resolve", side_effect=resolve),
                    patch.object(build_pipeline.node_toolchain, "prepare", side_effect=prepare),
                    patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=dependency),
                    patch.object(build_pipeline.build_executor, "execute_build") as execute_build,
                ):
                    result = build_pipeline.execute_pipeline_run(
                        build_pipeline.create_pipeline_run(self._configured_recipe(), store=store, dry_run=False)["id"],
                        store=store, acquire=self._acquire_hybrid,
                    )

                persisted = store.load(result["run_id"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(persisted["status"], "failed")
                self.assertEqual(persisted["error"]["stage"], "dependencies")
                self.assertEqual(persisted["error"]["code"], code)
                self.assertEqual(
                    next(row for row in persisted["steps"] if row["name"] == "dependencies")["status"],
                    "failed",
                )
                self.assertTrue(all(
                    row["status"] == "pending"
                    for row in persisted["steps"]
                    if row["name"] in {"source_changes", "build", "staging", "debian_metadata", "systemd", "package", "artifact"}
                ))
                self.assertEqual(persisted["steps"][1]["details"]["project_type"], "python")
                self.assertEqual(
                    persisted["steps"][1]["details"]["auxiliary_node_project"]["project_directory"],
                    "frontend",
                )
                execute_build.assert_not_called()

    def test_hybrid_real_nonzero_and_timeout_commands_are_persisted_and_cleaned(self):
        cases = (
            (
                "nonzero",
                (
                    "import sys\n"
                    "if sys.argv[1:] == ['--version']:\n"
                    "    print('11.7.0')\n"
                    "    raise SystemExit(0)\n"
                    "print('real-build-nonzero', flush=True)\n"
                    "raise SystemExit(7)\n"
                ),
                {},
                "build_command_failed",
                "real-build-nonzero",
            ),
            (
                "timeout",
                (
                    "import os, sys, time\n"
                    "from pathlib import Path\n"
                    "if sys.argv[1:] == ['--version']:\n"
                    "    print('11.7.0')\n"
                    "    raise SystemExit(0)\n"
                    "Path('timeout-process.pid').write_text(str(os.getpid()))\n"
                    "print('real-build-timeout-started', flush=True)\n"
                    "while True: time.sleep(60)\n"
                ),
                {"inactivity_timeout": 1, "maximum_runtime": 3},
                "build_command_timeout",
                "real-build-timeout-started",
            ),
        )
        for point, manager_source, limits, code, marker in cases:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = BuildStore(root / "builds")
                shared = self._shared_cache(root)
                configured = self._configured_recipe()
                configured["build"]["commands"] = ["npm run build", "npm must-not-run"]
                configured["build"].update(limits)

                with (
                    patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}),
                    patch.object(
                        build_pipeline.node_toolchain,
                        "prepare",
                        side_effect=lambda _r, **kw: self._prepare(
                            kw["workspace"], manager_source=manager_source,
                        ),
                    ),
                    patch.object(build_pipeline.debian_packaging, "prepare_staging") as prepare_staging,
                    patch.object(
                        build_pipeline.node_toolchain.urllib.request,
                        "urlopen",
                        side_effect=AssertionError("late network access"),
                    ) as network,
                ):
                    result = build_pipeline.execute_pipeline_run(
                        build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)["id"],
                        store=store, acquire=self._acquire_hybrid,
                    )

                persisted = store.load(result["run_id"])
                build_step = next(row for row in persisted["steps"] if row["name"] == "build")
                failed_command = persisted["error"]["details"]["failed_command"]
                self.assertEqual(result["status"], "failed")
                self.assertEqual(persisted["status"], "failed")
                self.assertEqual(persisted["error"]["stage"], "build")
                self.assertEqual(persisted["error"]["code"], code)
                self.assertEqual(build_step["status"], "failed")
                self.assertEqual(len(build_step["details"]["commands"]), 1)
                self.assertEqual(failed_command["index"], 1)
                self.assertEqual(failed_command["status"], "failed")
                self.assertIn(marker, failed_command["stdout"])
                self.assertEqual(failed_command["timed_out"], point == "timeout")
                if point == "nonzero":
                    self.assertEqual(failed_command["exit_code"], 7)
                else:
                    self.assertEqual(failed_command["timeout_reason"], "inactivity")
                    self.assertFalse(failed_command["termination_error"])
                    pid = int((Path(result["workspace"]) / "source/timeout-process.pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertTrue(all(row["status"] == "pending" for row in persisted["steps"][5:]))
                self.assertEqual(persisted["toolchain"]["node"]["version"], "24.12.0")
                self.assertEqual(persisted["toolchain"]["package_manager"]["version"], "11.7.0")
                self.assertEqual(persisted["steps"][1]["details"]["project_type"], "python")
                self.assertEqual(
                    persisted["steps"][1]["details"]["auxiliary_node_project"]["project_directory"],
                    "frontend",
                )
                prepare_staging.assert_not_called()
                network.assert_not_called()
                self._assert_terminal_toolchain_cleanup(store, result, shared)

    def test_concurrent_hybrid_primary_node_and_python_runs_keep_distinct_facts_and_environments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            recipes = {
                "run-a": self._configured_recipe("run-a"),
                "run-b": {**recipe(), "name": "run-b", "package": {**recipe()["package"], "name": "run-b"}},
                "run-c": {**recipe(), "name": "run-c", "package": {**recipe()["package"], "name": "run-c"}},
            }
            for configured in recipes.values():
                configured["build"] = configured.get("build", {}) | {"commands": ["true"]}
            common_barrier = threading.Barrier(3, timeout=10)
            node_barrier = threading.Barrier(2, timeout=10)
            common_release = threading.Event()
            all_common_active = threading.Event()
            environments = {"dependencies": {}, "build": {}}
            lock = threading.Lock()
            common_active = set()
            maximum_common_active = 0
            resolution_requests = []
            global_environment = dict(os.environ)

            def acquire(configured, workspace, token="", expected_identity=None):
                nonlocal maximum_common_active
                source = Path(workspace) / "source"
                if configured["name"] in {"run-a", "run-c"}:
                    (source / "pyproject.toml").write_text('[project]\nname = "demo"\n')
                if configured["name"] == "run-a":
                    (source / "frontend").mkdir()
                    (source / "frontend/package.json").write_text(json.dumps({
                        "packageManager": "npm@11.7.0",
                        "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
                    }))
                if configured["name"] == "run-b":
                    (source / "package.json").write_text(json.dumps({
                        "packageManager": "pnpm@10.24.0", "engines": {"node": "^22.19.0"},
                    }))
                    (source / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
                run_name = configured["name"]
                with lock:
                    common_active.add(run_name)
                    maximum_common_active = max(maximum_common_active, len(common_active))
                    if len(common_active) == 3:
                        all_common_active.set()
                try:
                    common_barrier.wait()
                    if not common_release.wait(10):
                        raise TimeoutError("three-Run overlap was not released")
                    return {
                        "repository": "owner/demo", "ref": "v1", "tag": "v1",
                        "upstream_version": "1.0", "debian_version": "1.0-1",
                        "source_directory": str(source),
                    }
                finally:
                    with lock:
                        common_active.discard(run_name)

            def resolve(requirements, *, package_manager, checkpoint=None):
                with lock:
                    resolution_requests.append((package_manager, dict(requirements)))
                return {"manager": package_manager, "node": "24.12.0" if package_manager == "npm" else "22.21.1"}

            def prepare(resolution, *, workspace, cache, checkpoint=None):
                node_barrier.wait()
                manager = resolution["manager"]
                version = "11.7.0" if manager == "npm" else "10.24.0"
                return self._prepare(workspace, node=resolution["node"], manager=manager, manager_version=version)

            def dependency_check(detected, manual, **kwargs):
                run_id = Path(kwargs["workspace"]).parent.name
                with lock:
                    environments["dependencies"][run_id] = dict(kwargs["environment"])
                return self._available(detected, manual, **kwargs)

            def execute_build(_recipe, _detection, source_directory, **kwargs):
                run_id = Path(source_directory).parent.name
                with lock:
                    environments["build"][run_id] = dict(kwargs["environment"])
                return {
                    "executed": True, "plan": {"commands": ["true"]}, "commands": [],
                    "output": {"mode": "source", "path": str(source_directory), "exists": True, "kind": "directory"},
                }

            runs = {
                run_id: build_pipeline.create_pipeline_run(configured, store=store, dry_run=False, run_id=run_id)["id"]
                for run_id, configured in recipes.items()
            }
            results = {}
            errors = []

            def run(run_id):
                try:
                    results[run_id] = build_pipeline.execute_pipeline_run(run_id, store=store, acquire=acquire)
                except Exception as exc:  # pragma: no cover - asserted below with full error visibility
                    with lock:
                        errors.append((run_id, repr(exc)))

            threads = []
            reached_common_overlap = False
            active_at_overlap = set()
            alive_at_overlap = False
            with (
                patch.object(build_pipeline.node_toolchain, "resolve", side_effect=resolve),
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=prepare),
                patch.object(build_pipeline.dependency_checker, "check_dependencies", side_effect=dependency_check),
                patch.object(build_pipeline.build_executor, "execute_build", side_effect=execute_build),
                patch.object(
                    build_pipeline.debian_packaging, "prepare_staging",
                    side_effect=build_pipeline.debian_packaging.PackagingError("fixture_complete", "stop after isolation proof"),
                ),
            ):
                threads = [threading.Thread(target=run, args=(run_id,)) for run_id in runs]
                try:
                    for thread in threads:
                        thread.start()
                    reached_common_overlap = all_common_active.wait(10)
                    with lock:
                        active_at_overlap = set(common_active)
                    alive_at_overlap = all(thread.is_alive() for thread in threads)
                    common_release.set()
                    for thread in threads:
                        thread.join(timeout=20)
                finally:
                    common_release.set()
                    common_barrier.abort()
                    node_barrier.abort()
                    for thread in threads:
                        if thread.is_alive():
                            thread.join(timeout=2)

            self.assertTrue(reached_common_overlap, errors)
            self.assertEqual(active_at_overlap, set(runs))
            self.assertEqual(maximum_common_active, 3)
            self.assertTrue(alive_at_overlap)
            for thread in threads:
                self.assertFalse(thread.is_alive(), f"worker thread {thread.name} did not terminate")
            self.assertEqual(
                errors, [],
                {run_id: (result["status"], result.get("error"), result.get("detection")) for run_id, result in results.items()},
            )
            self.assertEqual(set(results), set(runs))
            self.assertEqual(results["run-a"]["detection"]["project_type"], "python")
            self.assertEqual(results["run-a"]["detection"]["auxiliary_node_project"]["package_manager"], "npm")
            self.assertEqual(results["run-b"]["detection"]["project_type"], "nodejs")
            self.assertNotIn("auxiliary_node_project", results["run-b"]["detection"])
            self.assertEqual(results["run-c"]["detection"]["project_type"], "python")
            self.assertNotIn("auxiliary_node_project", results["run-c"]["detection"])
            self.assertEqual(results["run-c"]["dependencies"]["available_tools"], [])
            run_a_toolchain = store.load("run-a")["toolchain"]
            run_b_toolchain = store.load("run-b")["toolchain"]
            self.assertEqual(run_a_toolchain["node"]["version"], "24.12.0")
            self.assertEqual(run_a_toolchain["package_manager"]["name"], "npm")
            self.assertEqual(run_a_toolchain["package_manager"]["version"], "11.7.0")
            self.assertEqual(run_b_toolchain["node"]["version"], "22.21.1")
            self.assertEqual(run_b_toolchain["package_manager"]["name"], "pnpm")
            self.assertEqual(run_b_toolchain["package_manager"]["version"], "10.24.0")
            self.assertIsNone(store.load("run-c")["toolchain"])
            self.assertEqual(
                sorted(resolution_requests, key=lambda row: row[0]),
                [
                    ("npm", {"node": ">=24 <25", "npm": "11.7.0"}),
                    ("pnpm", {"node": "^22.19.0", "pnpm": "10.24.0"}),
                ],
            )
            for run_id in ("run-a", "run-b"):
                self.assertEqual(environments["dependencies"][run_id], environments["build"][run_id])
                self.assertTrue(environments["build"][run_id]["HOME"].startswith(results[run_id]["workspace"]))
                self.assertTrue(environments["build"][run_id]["npm_config_cache"].startswith(results[run_id]["workspace"]))
                self.assertTrue(environments["build"][run_id]["COREPACK_HOME"].startswith(results[run_id]["workspace"]))
            for key in ("PATH", "HOME", "npm_config_cache", "COREPACK_HOME"):
                self.assertNotEqual(environments["build"]["run-a"][key], environments["build"]["run-b"][key])
            self.assertEqual(environments["build"]["run-c"], {})
            self.assertEqual(dict(os.environ), global_environment)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_local_hybrid_fixture_builds_deb_without_frontend_or_toolchain_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = BuildStore(root / "builds")
            shared = self._shared_cache(root)

            def acquire(*args, **kwargs):
                source = Path(kwargs.get("workspace") or args[1]) / "source"
                result = self._acquire_hybrid(*args, **kwargs)
                (source / "payload/static").mkdir(parents=True)
                (source / "payload/app.py").write_text("print('packaged')\n")
                (source / "payload/static/index.html").write_text("built fixture\n")
                (source / "frontend/node_modules/private-package").mkdir(parents=True)
                (source / "frontend/node_modules/private-package/index.js").write_text("not packaged\n")
                return result

            configured = self._configured_recipe(
                "hybrid-package", output={"mode": "path", "path": "payload"},
            )
            configured["package"]["runtime_dependencies"] = ["ca-certificates"]
            with (
                patch.object(build_pipeline.node_toolchain, "resolve", return_value={"fixture": True}) as resolve,
                patch.object(build_pipeline.node_toolchain, "prepare", side_effect=lambda _r, **kw: self._prepare(kw["workspace"])),
                patch.object(build_pipeline.node_toolchain.urllib.request, "urlopen", side_effect=AssertionError("network access")) as network,
            ):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(configured, store=store, dry_run=False)["id"],
                    store=store, acquire=acquire,
                )

            self.assertEqual(result["status"], "success")
            self.assertTrue(Path(result["artifact"]["path"]).is_file())
            self.assertEqual(result["artifact"]["inspection"]["package"], "hybrid-package")
            self.assertEqual(result["artifact"]["inspection"]["depends"], "ca-certificates")
            inventory = {
                row["path"].rstrip("/")
                for row in store.artifact_files(result["run_id"], result["artifact"]["inspection"])
            }
            self.assertIn("./opt/hybrid-package/app.py", inventory)
            self.assertIn("./opt/hybrid-package/static/index.html", inventory)
            self.assertFalse(any(self._payload_path_is_forbidden(path) for path in inventory))
            for forbidden in ("frontend", "node_modules", "toolchain", "node", "npm", "pnpm"):
                self.assertTrue(self._payload_path_is_forbidden(f"./opt/hybrid-package/{forbidden}"))
            self.assertFalse(self._payload_path_is_forbidden("./opt/hybrid-package/node-helper.py"))
            self.assertFalse(self._payload_path_is_forbidden("./opt/hybrid-package/npmrc"))
            resolve.assert_called_once()
            network.assert_not_called()

            self._assert_terminal_toolchain_cleanup(store, result, shared)
            durable = store.load(result["run_id"])
            detection = next(step for step in durable["steps"] if step["name"] == "detection")["details"]
            self.assertEqual(durable["status"], "success")
            self.assertEqual(durable["toolchain"]["node"]["version"], "24.12.0")
            self.assertEqual(detection["project_type"], "python")
            self.assertEqual(detection["auxiliary_node_project"]["project_directory"], "frontend")


if __name__ == "__main__":
    unittest.main()
