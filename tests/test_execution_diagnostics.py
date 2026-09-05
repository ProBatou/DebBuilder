import json
import tempfile
import unittest
from pathlib import Path

from debbuilder import execution_service
from debbuilder.build_store import BuildStore


def recipe():
    return {
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

    def failed_run(self, code, message, *, stage="build", details=None):
        run = self.store.create(recipe(), mode="build")
        error = {"stage": stage, "code": code, "message": message, "details": details or {}}
        run.update({"status": "failed", "error": error})
        next(step for step in run["steps"] if step["name"] == stage).update({"status": "failed", "error": error})
        self.store.save(run)
        return execution_service.get_execution(self.store, run["id"])["diagnostic"]

    def test_missing_tool_distinguishes_path_tool_from_debian_dependency(self):
        diagnostic = self.failed_run("missing_build_tools", "Required build tools are unavailable", stage="dependencies", details={
            "tool_checks": [{"tool": "cargo", "status": "version_mismatch", "available": False, "requirement": ">=1.80", "working_directory": "/runs/demo/source", "search_path": "/usr/local/bin:/usr/bin"}],
        })
        self.assertEqual(diagnostic["title"], "Required build tool unavailable")
        self.assertIn("cargo: version_mismatch", [row["value"] for row in diagnostic["facts"]])
        self.assertEqual(diagnostic["recipe_step"], "build")
        self.assertIn("build PATH", diagnostic["next_action"])

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

    def test_source_change_absent_and_ambiguous_expose_index_file_anchor(self):
        for code, matches in (("source_match_not_found", 0), ("source_match_ambiguous", 3)):
            with self.subTest(code=code):
                diagnostic = self.failed_run(code, "Source change failed", stage="source_changes", details={
                    "failed": {"index": 2, "operation": "replace", "path": "src/app.js", "matches": matches, "anchor": "oldCall()"},
                    "applied": [],
                })
                rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
                self.assertEqual(rows["Change"], "2")
                self.assertEqual(rows["File"], "src/app.js")
                self.assertEqual(rows["Anchor"], "oldCall()")

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

    def test_validation_failure_uses_profile_check_and_failed_command(self):
        run = self.store.create(recipe(), mode="build")
        artifact = Path(run["workspace"]) / "artifacts/demo.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact)}, "validations": [{
            "status": "failed", "profile": {"name": "bookworm-node22"},
            "checks": [{"name": "systemd_active", "status": "failed", "error": "unit exited"}],
            "commands": [{"command": "systemctl is-active demo", "arguments": ["systemctl", "is-active", "demo"], "accepted": False, "exit_code": 3, "stderr": "inactive"}],
            "error": {"code": "validation_failed", "message": "Service validation failed", "details": {}},
        }]})
        self.store.save(run)
        diagnostic = execution_service.get_execution(self.store, run["id"])["diagnostic"]
        rows = {row["label"]: row["value"] for row in diagnostic["where"] + diagnostic["facts"]}
        self.assertEqual(rows["Profile"], "bookworm-node22")
        self.assertEqual(rows["Failed checks"], "systemd_active")
        self.assertEqual(rows["Command"], "systemctl is-active demo")

    def test_publication_failure_uses_preflight_repository_and_command(self):
        run = self.store.create(recipe(), mode="build")
        artifact = Path(run["workspace"]) / "artifacts/demo.deb"
        artifact.write_bytes(b"deb")
        run.update({"status": "success", "artifact": {"path": str(artifact)}, "validations": [{"status": "success"}], "publications": [{
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

    def test_success_has_no_diagnostic_and_legacy_error_has_generic_fallback(self):
        run = self.store.create(recipe(), mode="build")
        run["status"] = "success"
        self.store.save(run)
        self.assertIsNone(execution_service.get_execution(self.store, run["id"])["diagnostic"])
        run["status"] = "failed"
        run["error"] = {"message": "Old run failed"}
        self.store.save(run)
        diagnostic = execution_service.get_execution(self.store, run["id"])["diagnostic"]
        self.assertEqual(diagnostic["reason"], "Old run failed")
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
