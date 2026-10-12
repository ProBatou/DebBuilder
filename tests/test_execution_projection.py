import copy
import json
import tempfile
import unittest
from pathlib import Path

from debbuilder import execution_projection, execution_service
from debbuilder.build_store import BuildStore


def recipe():
    return {
        "schema_version": 5,
        "name": "projection-demo", "active": True,
        "package": {
            "name": "projection-package", "architecture": "all",
            "maintainer": "Demo <demo@example.test>", "description": "Demo",
        },
        "source": {"repository": "owner/projection-demo"},
    }


class ExecutionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.run = self.store.create(recipe(), mode="build", run_id="projection-run")

    def tearDown(self):
        self.temporary.cleanup()

    def populated_run(self):
        run = copy.deepcopy(self.run)
        artifact_path = Path(run["workspace"]) / "artifacts/projection-package_1.0_all.deb"
        artifact_path.write_bytes(b"deb")
        run.update({
            "status": "success",
            "version": {"upstream": "1.0", "debian": "1.0-1"},
            "future_secret": "top-secret-marker",
            "toolchain": {
                "schema_version": 1, "requested_node_range": "^22.19.0",
                "node": {"version": "22.23.3", "platform": "linux", "architecture": "x64", "sha256": "b" * 64, "source": "https://nodejs.org/dist/example", "cache_path": "/private/toolchain"},
                "package_manager": {"name": "pnpm", "requested_range": "10.24.0", "version": "10.24.0", "integrity": "sha512-safe", "source": "https://registry.npmjs.org/example"},
            },
            "artifact": {
                "path": str(artifact_path), "size": 3, "sha256": "a" * 64,
                "future_internal": "artifact-secret-marker",
                "inspection": {
                    "ok": True, "package": "projection-package", "version": "1.0-1",
                    "architecture": "all", "file_count": 4,
                    "files_manifest": "manifests/private-files.json",
                },
            },
            "error": {
                "code": "example_failure", "stage": "build",
                "message": "Safe failure at /private/error and https://internal.example/debug",
                "suggested_action": "Inspect C:\\private\\trace.log or file:///private/debug.log",
                "details": {"future_exception_state": "error-secret-marker", "path": "/private/error"},
            },
            "cancellation": {
                "code": "execution_cancelled", "reason": "user_requested", "stage": "build",
                "requested_at": "2026-09-21T10:00:00Z", "future_state": "cancellation-secret-marker",
            },
            "_validation_attempts": [{
                "id": "validation-one", "attempt_id": "validation-one", "build_run_id": "projection-run",
                "status": "success", "phase": "lifecycle", "profile": {"name": "bookworm"},
                "checks": [{"name": "install", "status": "success", "future": "check-secret-marker"}],
                "dependency_preparation": {"status": "success", "package_count": 2, "profile": "bookworm"},
                "cancellable": False, "artifact_matches_run": True,
                "status_url": "/api/executions/projection-run/validations/validation-one",
                "cancel_url": "https://internal.example/cancel?token=validation-url-secret-marker",
                "future_coordination": "validation-secret-marker",
                "backend": {"container_id": "private-container"}, "result_path": "/private/result.json",
            }],
            "publications": [{
                "id": "publication-one", "type": "publication", "build_run_id": "projection-run",
                "status": "success", "package": "projection-package", "version": "1.0-1",
                "architecture": "all", "artifact": str(artifact_path),
                "repository": {"root": "/private/repository", "distribution": "stable", "component": "main"},
                "readiness": {"ready": True, "reasons": [], "validation_id": "validation-one"},
                "proof": {
                    "schema": "debbuilder.repository-publication-proof.v1", "proof_version": 1,
                    "package": "projection-package", "version": "1.0-1", "architecture": "all",
                    "verified_at": "2026-09-21T10:00:00Z",
                    "component": "main",
                    "distribution": {"requested": "stable", "codename": "stable", "suite": "stable"},
                    "repository": {"root": "/private/repository", "device": 1, "inode": 2},
                    "source": {"path": str(artifact_path), "size": 3, "sha256": "a" * 64, "device": 3, "inode": 4},
                    "targets": [{
                        "database_architecture": "amd64",
                        "index": {
                            "path": "dists/stable/main/binary-amd64/Packages.gz",
                            "device": 5, "inode": 6, "filename": "pool/main/p/projection.deb",
                            "size": 3, "sha256": "a" * 64,
                        },
                        "pool": {
                            "path": "pool/main/p/projection.deb", "size": 3,
                            "sha256": "a" * 64, "device": 7, "inode": 8,
                        },
                    }],
                    "future_proof_internal": "proof-secret-marker",
                },
                "command": {"command": "reprepro", "environment": {"TOKEN": "publication-secret-marker"}},
                "future_internal": "publication-secret-marker",
            }],
        })
        source = run["steps"][0]
        source.update({
            "status": "success",
            "details": {
                "repository": "owner/projection-demo", "ref": "v1.0", "tag": "v1.0",
                "release_url": "https://api.example/private?token=source-secret-marker",
                "download_destination": "/private/download", "future_private": "source-secret-marker",
                "asset": {
                    "source": "release_asset", "name": "source.tar.gz", "sha256": "b" * 64,
                    "download_size": 42, "url": "https://signed.example/a?X-Amz-Signature=source-secret-marker",
                },
            },
        })
        build = run["steps"][4]
        build.update({
            "status": "success",
            "summary": "Build completed in /private/workspace via https://internal.example/build",
            "details": {"plan": {
                "commands": [{"command": "deploy --token step-secret-marker"}],
                "working_directory": "/private/workspace/source",
                "configured_working_directory": "src",
                "environment": {"TOKEN": "step-secret-marker", "MODE": "release"},
                "output": {"mode": "path", "configured_path": "dist", "path": "/private/dist"},
                "future_backend": "step-secret-marker",
            }, "ensure_directories": {
                "requested": 2, "created": 1, "already_existed": 1,
                "entries": [{"path": "private/step-secret-marker", "status": "created"}],
            }},
            "future_backend": "step-secret-marker",
        })
        source_changes = run["steps"][3]
        source_changes.update({
            "status": "success",
            "details": {"applied": [{
                "index": 1, "status": "applied", "operation": "replace",
                "path": "src/app.py", "matches": 1,
                "anchor": "arbitrary-source-secret-marker", "anchor_truncated": False,
            }]},
        })
        detection = run["steps"][1]
        detection.update({
            "status": "success",
            "details": {
                "project_type": "python",
                "display_name": "Python · pyproject.toml",
                "detected_files": ["pyproject.toml"],
                "auxiliary_node_project": {
                    "project_directory": "frontend",
                    "manifest": "frontend/package.json",
                    "package_manager": "npm",
                    "package_manager_spec": "npm@11.20.0",
                    "package_manager_version": "11.20.0",
                    "node_version": ">=24 <25",
                    "npm_version": ">=11 <12",
                    "tool_version_requirements": {"node": ">=24 <25", "npm": "11.20.0"},
                    "detected_files": ["frontend/package.json", "/private/manifest"],
                    "diagnostics": [],
                    "source_root": "/private/source",
                },
            },
        })
        run["automation"] = {
            "policy": "full", "attempt_key": "automation-secret-marker",
            "expected_upstream_identity": {"future_seal": "automation-secret-marker"},
        }
        return run

    def test_detail_is_allowlisted_at_every_run_depth(self):
        detail = execution_projection.public_detail(self.populated_run())
        encoded = json.dumps(detail, sort_keys=True)
        for marker in (
            "top-secret-marker", "artifact-secret-marker", "source-secret-marker",
            "step-secret-marker", "validation-secret-marker", "publication-secret-marker",
            "proof-secret-marker", "error-secret-marker", "automation-secret-marker",
            "check-secret-marker",
            "validation-url-secret-marker",
            "cancellation-secret-marker",
            "arbitrary-source-secret-marker",
        ):
            self.assertNotIn(marker, encoded)
        for forbidden in (
            "workspace", "recipe_sha256", "schema_version", "admission_sha256",
            "download_destination", "release_url",
            "backend", "result_path", "repository.root", "files_manifest",
        ):
            self.assertNotIn(forbidden, encoded)

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()

        self.assertTrue({"commands", "command", "arguments", "environment"}.isdisjoint(keys(detail["steps"])))
        self.assertNotIn("/private", json.dumps(detail["steps"], sort_keys=True))

        self.assertEqual(detail["artifact"]["name"], "projection-package_1.0_all.deb")
        self.assertNotIn("path", detail["artifact"])
        self.assertEqual(detail["source"]["repository"], "owner/projection-demo")
        self.assertEqual(detail["source"]["asset"]["name"], "source.tar.gz")
        build = next(step for step in detail["steps"] if step["name"] == "build")
        self.assertEqual(build["details"]["plan"]["configured_working_directory"], "src")
        self.assertEqual(build["details"]["plan"]["environment_keys"], ["TOKEN", "MODE"])
        self.assertEqual(build["details"]["plan"]["command_count"], 1)
        self.assertEqual(build["details"]["ensure_directories"], {
            "requested": 2, "created": 1, "already_existed": 1,
        })
        self.assertEqual(
            build["summary"],
            "Build completed in [redacted-path] via [redacted-url]",
        )
        self.assertEqual(detail["validations"][0]["checks"], [{"name": "install", "status": "success"}])
        self.assertEqual(detail["validations"][0]["status_url"], "/api/executions/projection-run/validations/validation-one")
        self.assertEqual(detail["validations"][0]["cancel_url"], "")
        self.assertTrue(detail["publications"][0]["proof"]["available"])
        self.assertEqual(detail["publications"][0]["proof"]["verified_at"], "2026-09-21T10:00:00Z")
        self.assertEqual(detail["publications"][0]["proof"]["artifact"]["sha256"], "a" * 64)
        self.assertEqual(detail["automation"], {"policy": "full"})
        self.assertEqual(detail["toolchain"], {
            "node": {"requested_range": "^22.19.0", "version": "22.23.3", "platform": "linux", "architecture": "x64", "sha256": "b" * 64},
            "package_manager": {"name": "pnpm", "requested_range": "10.24.0", "version": "10.24.0", "integrity": "sha512-safe"},
        })
        detection = next(step for step in detail["steps"] if step["name"] == "detection")
        self.assertEqual(detection["details"]["project_type"], "python")
        self.assertEqual(detection["details"]["auxiliary_node_project"], {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "package_manager_spec": "npm@11.20.0",
            "package_manager_version": "11.20.0",
            "node_version": ">=24 <25",
            "npm_version": ">=11 <12",
            "detected_files": ["frontend/package.json"],
        })
        self.assertNotIn("tool_version_requirements", encoded)
        self.assertNotIn("source_root", encoded)
        self.assertNotIn("cache_path", encoded)
        self.assertEqual(detail["error"], {
            "code": "example_failure", "stage": "build",
            "message": "Safe failure at [redacted-path] and [redacted-url]",
            "suggested_action": "Inspect [redacted-path] or [redacted-url]",
        })

    def test_auxiliary_diagnostics_are_publicly_sanitized_bounded_and_read_only(self):
        run = self.populated_run()
        auxiliary = next(step for step in run["steps"] if step["name"] == "detection")["details"]["auxiliary_node_project"]
        auxiliary["diagnostics"] = [
            "failed at /private/source/project",
            "cache miss in /var/cache/debbuilder/npm/private-entry",
            "fetch https://operator:password@internal.example/private",
            "request token=recognizable-token-marker",
            "Manifest constraints are compatible.",
            "x" * 500,
            {"message": "non-text-secret-marker"},
            42,
        ] + [f"bounded diagnostic {index}" for index in range(60)]
        original = copy.deepcopy(run)

        detail = execution_projection.public_detail(run)
        projected = next(step for step in detail["steps"] if step["name"] == "detection")["details"]["auxiliary_node_project"]
        diagnostics = projected["diagnostics"]
        encoded = json.dumps(diagnostics)

        self.assertLessEqual(len(diagnostics), 50)
        self.assertEqual(diagnostics[0], "failed at [redacted-path]")
        self.assertEqual(diagnostics[1], "cache miss in [redacted-path]")
        self.assertEqual(diagnostics[2], "fetch [redacted-url]")
        self.assertEqual(diagnostics[3], "request token=[redacted]")
        self.assertEqual(diagnostics[4], "Manifest constraints are compatible.")
        self.assertLessEqual(len(diagnostics[5]), 240)
        for forbidden in (
            "/private/source", "/var/cache/debbuilder", "operator:password",
            "recognizable-token-marker", "non-text-secret-marker", "bounded diagnostic 59",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(run, original)

    def test_incomplete_auxiliary_projects_are_omitted_without_losing_primary_or_toolchain_facts(self):
        complete = {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "node_version": ">=24 <25",
            "diagnostics": ["Manifest constraints are compatible."],
        }
        cases = {
            "diagnostics_only": {"diagnostics": ["partial"]},
            "constraint_only": {"node_version": ">=24 <25"},
            "project_directory_absent": {key: value for key, value in complete.items() if key != "project_directory"},
            "manifest_absent": {key: value for key, value in complete.items() if key != "manifest"},
            "package_manager_absent": {key: value for key, value in complete.items() if key != "package_manager"},
            "historical_absent": None,
        }
        for status, auxiliary in (("failed", {"diagnostics": ["partial"]}), ("cancelled", {"node_version": ">=24 <25"})):
            cases[f"{status}_partial"] = auxiliary

        for name, auxiliary in cases.items():
            with self.subTest(name=name):
                run = self.populated_run()
                run["status"] = name.split("_", 1)[0] if name.startswith(("failed_", "cancelled_")) else "success"
                detection = next(step for step in run["steps"] if step["name"] == "detection")["details"]
                if auxiliary is None:
                    detection.pop("auxiliary_node_project")
                else:
                    detection["auxiliary_node_project"] = auxiliary
                detail = execution_projection.public_detail(run)
                projected = next(step for step in detail["steps"] if step["name"] == "detection")["details"]
                self.assertEqual(projected["project_type"], "python")
                self.assertNotIn("auxiliary_node_project", projected)
                self.assertEqual(detail["toolchain"]["node"]["version"], "22.23.3")
                self.assertEqual(detail["toolchain"]["package_manager"]["version"], "10.24.0")

        run = self.populated_run()
        detail = execution_projection.public_detail(run)
        projected = next(step for step in detail["steps"] if step["name"] == "detection")["details"]["auxiliary_node_project"]
        self.assertEqual(projected["project_directory"], "frontend")
        self.assertEqual(projected["manifest"], "frontend/package.json")
        self.assertEqual(projected["package_manager"], "npm")

    def test_malformed_auxiliary_core_is_omitted_without_coercion_or_normalization(self):
        complete = {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "diagnostics": ["Manifest constraints are compatible."],
        }
        cases = {
            "project_directory_dict": {"project_directory": {"bad": 1}},
            "project_directory_number": {"project_directory": 42},
            "project_directory_boolean": {"project_directory": True},
            "manifest_list": {"manifest": ["frontend/package.json"]},
            "manifest_number": {"manifest": 42},
            "manifest_boolean": {"manifest": False},
            "empty": {"project_directory": ""},
            "one_space": {"project_directory": " ", "manifest": " /package.json"},
            "multiple_spaces": {"project_directory": "   ", "manifest": "   /package.json"},
            "tabs": {"project_directory": "\t\t", "manifest": "\t\t/package.json"},
            "dot": {"project_directory": ".", "manifest": "./package.json"},
            "parent": {"project_directory": "apps/../frontend"},
            "posix_absolute": {"project_directory": "/private/frontend", "manifest": "/private/frontend/package.json"},
            "windows_backslash": {"project_directory": "C:\\private", "manifest": "C:\\private/package.json"},
            "windows_drive": {"project_directory": "C:/private", "manifest": "C:/private/package.json"},
            "backslash": {"project_directory": "front\\end", "manifest": "front\\end/package.json"},
            "repeated_separator": {"project_directory": "front//end", "manifest": "front//end/package.json"},
            "trailing_separator": {"project_directory": "frontend/", "manifest": "frontend//package.json"},
            "control_character": {"project_directory": "front\x00end", "manifest": "front\x00end/package.json"},
            "mismatched_manifest": {"manifest": "other/package.json"},
            "wrong_package_manager": {"package_manager": "pnpm"},
            "non_text_package_manager": {"package_manager": ["npm"]},
        }

        for name, updates in cases.items():
            with self.subTest(name=name):
                run = self.populated_run()
                detection = next(step for step in run["steps"] if step["name"] == "detection")["details"]
                detection["auxiliary_node_project"] = {**complete, **updates}
                detail = execution_projection.public_detail(run)
                projected = next(step for step in detail["steps"] if step["name"] == "detection")["details"]
                self.assertEqual(projected["project_type"], "python")
                self.assertNotIn("auxiliary_node_project", projected)
                self.assertEqual(detail["toolchain"]["node"]["version"], "22.23.3")
                self.assertEqual(detail["toolchain"]["package_manager"]["version"], "10.24.0")

    def test_auxiliary_optional_values_require_text_and_detected_files_are_canonical(self):
        auxiliary = {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "package_manager_spec": {"bad": "npm@11.20.0"},
            "package_manager_version": ["11.20.0"],
            "node_version": 24,
            "npm_version": "11\x00.x",
            "detected_files": [
                "frontend/package.json", {"private": "/private/manifest"}, 42, True,
                "/private/manifest", "C:\\private\\package.json", "../private/package.json",
                "frontend//other.json", "frontend/other.json",
            ],
            "diagnostics": ["Manifest constraints are compatible.", {"private": "/private/manifest"}],
        }

        projected = execution_projection._public_auxiliary_node_project(auxiliary)

        self.assertEqual(projected, {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "detected_files": ["frontend/package.json", "frontend/other.json"],
            "diagnostics": ["Manifest constraints are compatible."],
        })

    def test_auxiliary_optional_values_omit_private_locations_and_preserve_constraints(self):
        auxiliary = {
            "project_directory": "my frontend",
            "manifest": "my frontend/package.json",
            "package_manager": "npm",
            "package_manager_spec": "https://internal.example/private",
            "package_manager_version": "https://operator:password@internal.example/npm",
            "node_version": "/private/source/node-range",
            "npm_version": "C:\\private\\npm-range",
        }

        projected = execution_projection._public_auxiliary_node_project(auxiliary)

        self.assertEqual(projected, {
            "project_directory": "my frontend",
            "manifest": "my frontend/package.json",
            "package_manager": "npm",
        })
        legitimate = execution_projection._public_auxiliary_node_project({
            **auxiliary,
            "package_manager_spec": "npm@11.20.0",
            "package_manager_version": "11.20.0",
            "node_version": ">=24 <25",
            "npm_version": ">=11 <12",
        })
        self.assertEqual(legitimate["package_manager_spec"], "npm@11.20.0")
        self.assertEqual(legitimate["package_manager_version"], "11.20.0")
        self.assertEqual(legitimate["node_version"], ">=24 <25")
        self.assertEqual(legitimate["npm_version"], ">=11 <12")

    def test_summary_has_consistent_version_semantics_and_no_internal_fields(self):
        run = self.populated_run()
        summary = execution_projection.public_summary(run)
        detail = execution_projection.public_detail(run)
        self.assertEqual(summary["version"], {"upstream": "1.0", "debian": "1.0-1"})
        self.assertEqual(summary["version"], detail["version"])
        self.assertNotIn("workspace", summary)
        self.assertNotIn("schema_version", summary)
        self.assertNotIn("recipe_sha256", summary)

    def test_proofless_success_fails_closed_in_run_projection(self):
        run = self.populated_run()
        run["publications"][0]["proof"] = None
        run["already_published"] = True
        run["publication_reconciliation_available"] = True

        summary = execution_projection.public_summary(run)
        publication = execution_projection.public_publication(run["publications"][0])

        self.assertEqual(summary["publication_status"], "failed")
        self.assertEqual(summary["lifecycle_status"], "publication_failed")
        self.assertFalse(summary["already_published"])
        self.assertFalse(summary["publication_reconciliation_available"])
        self.assertFalse(summary["allowed_actions"]["publish"])
        self.assertEqual(publication["status"], "failed")
        self.assertFalse(publication["proof"]["available"])

    def test_diagnostic_boundary_masks_nested_credentials_and_signed_urls(self):
        run = self.populated_run()
        run["status"] = "failed"
        run["publications"] = []
        run["_validation_attempts"] = []
        run["error"] = {
            "code": "build_command_failed", "stage": "build",
            "message": "failed https://user:password@example.test/file?X-Amz-Signature=url-secret-marker ssh://user:ssh-secret-marker@internal.example/repo",
            "details": {
                "failed_command": {
                    "command": "deploy --token command-secret-marker",
                    "stderr": "Authorization: Bearer output-secret-marker plain-env-marker",
                    "environment": {"TOKEN": "environment-secret-marker"},
                },
                "future_nested": {"secret": "nested-secret-marker"},
            },
        }
        run["steps"][4]["details"]["plan"]["environment"]["MODE"] = "plain-env-marker"
        diagnostic = execution_service.execution_diagnostic(run)
        encoded = json.dumps(diagnostic)
        for marker in (
            "url-secret-marker", "command-secret-marker", "output-secret-marker",
            "environment-secret-marker", "nested-secret-marker", "user:password",
            "ssh-secret-marker", "plain-env-marker",
        ):
            self.assertNotIn(marker, encoded)
        self.assertIn("[redacted", encoded.lower())

    def test_validation_check_reason_is_bounded_and_sanitized(self):
        validation = execution_projection.public_validation({
            "id": "validation-one", "status": "failed", "phase": "lifecycle",
            "profile": {"name": "bookworm"},
            "checks": [{
                "name": "package_install", "status": "failed",
                "error": "failed at /private/work with token=private-token " + "x" * 500,
            }],
        })
        reason = validation["checks"][0]["error"]
        self.assertLessEqual(len(reason), 240)
        self.assertNotIn("/private/work", reason)
        self.assertNotIn("private-token", reason)
        self.assertIn("[redacted", reason.lower())

    def test_log_diagnostics_filter_secrets_at_every_verbosity(self):
        run = self.populated_run()
        run["events"] = [{"message": "Build command token=event-secret-marker"}]
        run["steps"][4]["details"] = {
            "plan": {"environment": {"MODE": "plain-env-output-marker"}},
            "commands": [{
                "index": 1, "status": "failed", "working_directory": "/private/source",
                "stdout": "Authorization: Bearer stdout-secret-marker plain-env-output-marker",
                "stderr": "failed https://signed.example/a?X-Amz-Signature=stderr-secret-marker",
            }],
        }
        run["_validation_attempts"] = [{
            "id": "failed-validation", "status": "failed",
            "error": {"code": "failed", "message": "Failed", "details": {
                "environment": {"MODE": "environment-secret-marker"},
                "TOKEN": "json-secret-marker",
            }},
        }]
        self.store.append_log_line(run["id"], 'password=raw-secret-marker {"TOKEN": "raw-json-secret-marker"}')
        for verbosity in ("normal", "verbose", "raw"):
            log = execution_service.get_log(self.store, run["id"], verbosity=verbosity, run=run)
            for marker in (
                "event-secret-marker", "stdout-secret-marker", "stderr-secret-marker", "raw-secret-marker",
                "environment-secret-marker", "json-secret-marker", "raw-json-secret-marker",
                "plain-env-output-marker",
            ):
                self.assertNotIn(marker, log["text"])
        raw_text = self.store.log_text(run["id"])
        adversarial_offset = raw_text.index("raw-secret-marker")
        tail = execution_service.get_log(
            self.store, run["id"], verbosity="raw", after=adversarial_offset, run=run,
        )
        self.assertNotIn("raw-secret-marker", tail["text"])

    def test_projection_is_read_only(self):
        run = self.populated_run()
        run["automation"] = None
        stored = dict(run)
        stored.pop("_validation_attempts", None)
        self.store.save(stored)
        path = self.store.run_dir(run["id"]) / "run.json"
        before = path.read_bytes()
        detail = execution_service.get_execution(self.store, run["id"], run=run)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("workspace", detail)


if __name__ == "__main__":
    unittest.main()
