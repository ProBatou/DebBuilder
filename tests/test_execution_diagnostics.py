import json
import tempfile
import unittest
from pathlib import Path

from debbuilder import execution_service
from debbuilder.build_store import BuildStore


def recipe():
    return {
        "schema_version": 5,
        "name": "demo", "active": True,
        "package": {"name": "demo", "architecture": "all", "maintainer": "Demo <demo@example.test>", "description": "Demo"},
        "source": {"repository": "owner/demo"},
    }


class ExecutionDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = BuildStore(Path(self.temporary.name) / "builds")

    def tearDown(self):
        self.temporary.cleanup()

    def failed_run(self, code, message, *, stage="build", details=None, toolchain=None):
        run = self.store.create(recipe(), mode="build")
        error = {"stage": stage, "code": code, "message": message, "details": details or {}}
        run.update({"status": "failed", "error": error})
        if toolchain:
            run["toolchain"] = toolchain
        next(step for step in run["steps"] if step["name"] == stage).update({"status": "failed", "error": error})
        self.store.save(run)
        return execution_service.get_execution(self.store, run["id"])["diagnostic"]

    def test_missing_tool_distinguishes_path_tool_from_debian_dependency(self):
        diagnostic = self.failed_run("missing_build_tools", "Required build tools are unavailable", stage="dependencies", details={
            "tool_checks": [{"tool": "cargo", "status": "version_mismatch", "available": False, "requirement": ">=1.80", "version": "1.79.0", "working_directory": "/runs/demo/source", "search_path": "/usr/local/bin:/usr/bin"}],
            "project_requirement": {"package_manager": "pnpm", "package_manager_spec": "pnpm@10.0.0", "node_version": "^22.19.0"},
        })
        self.assertEqual(diagnostic["title"], "Required build tool unavailable")
        self.assertIn("cargo: version_mismatch", [row["value"] for row in diagnostic["facts"]])
        self.assertEqual(diagnostic["recipe_step"], "build")
        self.assertIn("build PATH", diagnostic["next_action"])
        facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
        self.assertEqual(facts["Detected version"], "cargo 1.79.0")
        self.assertEqual(facts["Package manager"], "pnpm")
        self.assertEqual(facts["Project requirement"], "pnpm@10.0.0")

    def test_structured_command_requirement_has_required_and_detected_versions(self):
        command = {"command": "node frontend/scripts/check-toolchain.mjs", "exit_code": 1, "status": "failed"}
        diagnostic = self.failed_run("toolchain_requirement_mismatch", "Environment mismatch", details={
            "failed_command": command,
            "command_diagnostic": {"code": "toolchain_requirement_mismatch", "requirements": [
                {"tool": "Node", "required": "24.x", "detected": "26.9.0"},
                {"tool": "npm", "required": "11.x", "detected": "9.7.2"},
            ]},
        })
        facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
        self.assertEqual(diagnostic["title"], "Prepared toolchain does not satisfy command requirements")
        self.assertEqual(facts["Required Node"], "24.x")
        self.assertEqual(facts["Detected Node"], "26.9.0")
        self.assertEqual(facts["Required npm"], "11.x")
        self.assertEqual(facts["Detected npm"], "9.7.2")

    def test_run_local_toolchain_error_families_are_actionable(self):
        project = {"project_requirement": {
            "project_type": "nodejs", "node_version": "^22.19.0",
            "package_manager": "pnpm", "package_manager_spec": "pnpm@10.24.0",
        }}
        cases = (
            ("node_range_unsupported", "Unsupported Node requirement", {"requested_range": "latest"}),
            ("node_range_unsatisfied", "No compatible Node release available", {"requested_range": "^22.19.0"}),
            ("node_toolchain_acquisition_failed", "Run-local Node toolchain preparation failed", {"source": "https://nodejs.org/dist/index.json"}),
            ("node_integrity_mismatch", "Node toolchain integrity verification failed", {"version": "22.21.1", "archive": "node.tar.xz", "expected_sha256": "a" * 64}),
            ("package_manager_acquisition_failed", "Package-manager toolchain preparation failed", {"package_manager": "pnpm", "requested_range": "10.24.0", "source": "https://registry.npmjs.org/pnpm"}),
            ("package_manager_integrity_mismatch", "Package-manager toolchain preparation failed", {"package_manager": "pnpm", "version": "10.24.0", "integrity": "sha512-safe"}),
            ("package_manager_range_unsatisfied", "Package-manager requirement cannot be resolved", {"package_manager": "pnpm", "requested_range": "99.x"}),
            ("prepared_node_toolchain_missing", "Prepared Run-local toolchain unavailable", {}),
        )
        for code, title, detail in cases:
            with self.subTest(code=code):
                diagnostic = self.failed_run(code, "Toolchain failure", stage="dependencies", details={**project, **detail})
                facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
                self.assertEqual(diagnostic["title"], title)
                self.assertTrue(diagnostic["next_action"])
                self.assertIn("Node requirement", facts)
                self.assertEqual(facts["Package manager"], "pnpm")

    def test_prepared_toolchain_execution_failure_includes_provenance(self):
        toolchain = {
            "requested_node_range": "^22.19.0",
            "node": {"version": "22.21.1"},
            "package_manager": {"name": "pnpm", "version": "10.24.0", "requested_range": "10.24.0"},
        }
        diagnostic = self.failed_run(
            "build_command_failed", "Build failed",
            details={"failed_command": {"command": "pnpm build", "exit_code": 2, "status": "failed"}},
            toolchain=toolchain,
        )
        facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
        self.assertEqual(diagnostic["title"], "Prepared toolchain command failed")
        self.assertEqual(facts["Prepared Node"], "22.21.1")
        self.assertEqual(facts["Prepared package manager"], "pnpm 10.24.0")

    def test_missing_debian_dependencies_keep_detected_and_manual_origins(self):
        diagnostic = self.failed_run("missing_build_dependencies", "Missing system packages", stage="dependencies", details={
            "missing": ["libssl-dev"], "detected": ["libssl-dev"], "manually_added": ["pkg-config"],
        })
        values = {row["label"]: row["value"] for row in diagnostic["facts"]}
        self.assertEqual(values["Missing packages"], "libssl-dev")
        self.assertEqual(values["Manually configured"], "pkg-config")
        self.assertIn("Build dependencies", diagnostic["next_action"])

    def test_command_failure_exposes_command_cwd_exit_and_bounded_output(self):
        command = {"index": 2, "command": "npm run build", "working_directory": "/runs/demo/source/web", "exit_code": 7, "stderr": "one\ntwo\nthree\nfour", "status": "failed"}
        diagnostic = self.failed_run("build_command_failed", "Build command 2 failed", details={"failed_command": command, "plan": {}})
        rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
        self.assertEqual(rows["Command"], "npm run build")
        self.assertEqual(rows["Exit code"], "7")
        self.assertNotIn("one", rows["Last command output"])
        self.assertIn("four", rows["Last command output"])

    def test_diagnostic_keeps_sensitive_command_and_output_values_masked(self):
        command = {
            "command": "deploy --token private-command-value", "arguments": ["deploy", "--token", "private-command-value"],
            "stderr": "request failed: token=private-output-value", "status": "failed", "exit_code": 1,
        }
        diagnostic = self.failed_run("build_command_failed", "Bearer private-reason-value", details={"failed_command": command})
        rendered = str(diagnostic)
        for secret in ("private-command-value", "private-output-value", "private-reason-value"):
            self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("environment", diagnostic)

    def test_timeout_reasons_have_distinct_limits_and_actions(self):
        for reason, limit_name, expected_action in (
            ("inactivity", "Configured inactivity", "inactivity timeout"),
            ("maximum_runtime", "Configured maximum runtime", "maximum runtime"),
        ):
            with self.subTest(reason=reason):
                command = {"index": 1, "command": "slow-build", "working_directory": "/source", "status": "failed", "timed_out": True, "timeout_reason": reason}
                diagnostic = self.failed_run("build_command_timeout", "Build command timed out", details={"failed_command": command, "plan": {"inactivity_timeout": 60, "maximum_runtime": 900}})
                facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
                self.assertIn(limit_name, facts)
                self.assertIn(expected_action, diagnostic["next_action"])

    def test_source_change_absent_and_ambiguous_expose_summary_without_source_excerpt(self):
        for code, matches in (("source_match_not_found", 0), ("source_match_ambiguous", 3)):
            with self.subTest(code=code):
                diagnostic = self.failed_run(code, "Source change failed", stage="source_changes", details={
                    "failed": {"index": 2, "operation": "replace", "path": "src/app.js", "matches": matches, "anchor": "oldCall()"},
                    "applied": [],
                })
                rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
                self.assertEqual(rows["Change"], "2")
                self.assertEqual(rows["File"], "src/app.js")
                self.assertNotIn("Anchor", rows)
                self.assertNotIn("oldCall()", str(diagnostic))

    def test_expected_output_and_mapping_have_precise_locations(self):
        output = self.failed_run("expected_output_missing", "Output missing", details={
            "output": {"configured_path": "dist/app", "path": "/runs/demo/source/dist/app", "kind": "missing"},
        })
        self.assertEqual({row["label"]: row["value"] for row in output["where"]}["Configured path"], "dist/app")
        mapping = self.failed_run("configuration_source_missing", "Mapping failed", stage="staging", details={
            "mapping_index": 2, "source": "packaging/demo.env", "resolved_source": "/runs/demo/source/packaging/demo.env", "destination": "/etc/demo.env", "cause": "does not exist",
        })
        rows = {row["label"]: row["value"] for row in mapping["where"] + mapping["facts"]}
        self.assertEqual(rows["Mapping"], "2")
        self.assertEqual(rows["Destination"], "/etc/demo.env")
        self.assertEqual(mapping["recipe_step"], "install")

    def test_post_build_directory_failure_has_relative_location_and_kind(self):
        diagnostic = self.failed_run("post_build_directory_symlink", "Directory failed", details={
            "directory": "apps/server/node_modules", "actual_kind": "symlink",
        })
        where = {row["label"]: row["value"] for row in diagnostic["where"]}
        facts = {row["label"]: row["value"] for row in diagnostic["facts"]}
        self.assertEqual(diagnostic["title"], "Post-build directory preparation failed")
        self.assertEqual(where["Configured path"], "apps/server/node_modules")
        self.assertEqual(facts["Observed"], "symlink")
        self.assertNotIn("/runs/", str(diagnostic))

    def test_validation_failure_uses_profile_check_and_failed_command(self):
        run = self.store.create(recipe(), mode="build")
        artifact = Path(run["workspace"]) / "artifacts/demo.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact)}})
        projected = {**run, "_validation_attempts": [{
            "status": "failed", "profile": {"name": "bookworm-node22"},
            "checks": [{"name": "systemd_active", "status": "failed", "error": "unit exited"}],
            "commands": [{"command": "systemctl is-active demo", "arguments": ["systemctl", "is-active", "demo"], "accepted": False, "exit_code": 3, "stderr": "inactive"}],
            "error": {"code": "validation_failed", "message": "Service validation failed", "details": {}},
        }]}
        self.store.save(run)
        diagnostic = execution_service.get_execution(self.store, run["id"], run=projected)["diagnostic"]
        rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
        self.assertEqual(rows["Profile"], "bookworm-node22")
        self.assertEqual(rows["Failed checks"], "systemd_active")
        self.assertEqual(rows["Command"], "systemctl is-active demo")
        self.assertEqual(rows["Reason"], "unit exited")
        self.assertEqual(rows["Lifecycle phase"], "lifecycle")
        self.assertEqual(diagnostic["title"], "Offline lifecycle validation failed")

    def test_publication_failure_uses_preflight_repository_and_command(self):
        run = self.store.create(recipe(), mode="build")
        artifact = Path(run["workspace"]) / "artifacts/demo.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact)}, "publications": [{
            "status": "failed", "repository": {"distribution": "bookworm", "component": "main"},
            "readiness": {"ready": True, "reasons": []},
            "command": {"command": "reprepro includedeb bookworm demo.deb", "status": "failed", "exit_code": 1, "stderr": "signature failed"},
            "error": {"code": "reprepro_include_failed", "message": "Publication command failed", "details": {}},
        }]})
        self.store.save(run)
        diagnostic = execution_service.get_execution(self.store, run["id"])["diagnostic"]
        rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
        self.assertEqual(rows["Distribution"], "bookworm")
        self.assertEqual(rows["Preflight"], "reprepro_include_failed")
        self.assertIn("signature failed", rows["Last command output"])

    def test_proofless_publication_success_is_diagnosed_as_failure(self):
        run = self.store.create(recipe(), mode="build")
        run.update({
            "status": "success",
            "publications": [{"id": "proofless", "status": "success", "version": "1.0-1", "proof": None}],
        })
        self.store.save(run)

        diagnostic = execution_service.get_execution(self.store, run["id"])["diagnostic"]

        self.assertEqual(diagnostic["code"], "publication_proof_invalid")
        self.assertEqual(diagnostic["stage"], "publication")

    def test_success_has_no_diagnostic_and_missing_error_code_has_default(self):
        run = self.store.create(recipe(), mode="build")
        run["status"] = "success"
        self.store.save(run)
        self.assertIsNone(execution_service.get_execution(self.store, run["id"])["diagnostic"])
        run["status"] = "failed"
        run["error"] = {"message": "Build failed"}
        self.store.save(run)
        diagnostic = execution_service.get_execution(self.store, run["id"])["diagnostic"]
        self.assertEqual(diagnostic["reason"], "Build failed")
        self.assertEqual(diagnostic["code"], "build_failed")
        self.assertNotIn("undefined", str(diagnostic))

    def test_diagnostic_is_a_read_time_detail_only_projection(self):
        run = self.store.create(recipe(), mode="build")
        run.update({"status": "failed", "error": {"message": "Projected failure"}})
        self.store.save(run)
        stored_path = self.store.run_dir(run["id"]) / "run.json"
        self.assertNotIn("diagnostic", json.loads(stored_path.read_text()))

        detail = execution_service.get_execution(self.store, run["id"])
        self.assertEqual(detail["diagnostic"]["reason"], "Projected failure")
        self.assertNotIn("diagnostic", json.loads(stored_path.read_text()))
        listed = execution_service.list_executions(self.store, lambda item: item["recipe_id"])
        self.assertNotIn("diagnostic", listed[0])


if __name__ == "__main__":
    unittest.main()
