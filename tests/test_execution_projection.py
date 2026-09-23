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
        self.assertEqual(detail["error"], {
            "code": "example_failure", "stage": "build",
            "message": "Safe failure at [redacted-path] and [redacted-url]",
            "suggested_action": "Inspect [redacted-path] or [redacted-url]",
        })

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
