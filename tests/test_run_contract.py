import copy
import hashlib
import json
import unittest

from debbuilder.build_models import (
    CURRENT_RUN_SCHEMA_VERSION,
    RunDocumentError,
    new_run,
    validate_run,
)


class RunContractTests(unittest.TestCase):
    def test_current_run_validates_with_isolated_copy_and_admission_seal(self):
        run = new_run("current", "demo", "build", "/tmp/current", "a" * 64)
        payload = {
            "schema_version": 1,
            "recipe_id": run["recipe_id"],
            "recipe_sha256": run["recipe_sha256"],
            "mode": run["mode"],
            "origin": run["origin"],
            "automation": run["automation"],
            "manual_source_provenance": run["manual_source_provenance"],
        }
        expected = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(run["admission_sha256"], expected)
        validated = validate_run(run)
        self.assertEqual(validated, run)
        self.assertIsNot(validated, run)
        validated["steps"][0]["details"]["changed"] = True
        self.assertEqual(run["steps"][0]["details"], {})

        tampered = copy.deepcopy(run)
        tampered["mode"] = "dry_run"
        with self.assertRaisesRegex(ValueError, "seal is invalid"):
            validate_run(tampered)

    def test_only_current_schema_is_accepted(self):
        run = new_run("current", "demo", "build", "/tmp/current", "a" * 64)
        cases = (
            (1, "unsupported_run_schema_version"),
            (2, "unsupported_run_schema_version"),
            (3, "unsupported_run_schema_version"),
            (CURRENT_RUN_SCHEMA_VERSION + 1, "future_run_schema_version"),
            (True, "invalid_run_schema_version"),
            ("1", "invalid_run_schema_version"),
        )
        for version, code in cases:
            with self.subTest(version=version), self.assertRaises(RunDocumentError) as raised:
                validate_run({**run, "schema_version": version})
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.source_version, version)
            self.assertEqual(raised.exception.target_version, CURRENT_RUN_SCHEMA_VERSION)
        with self.assertRaises(RunDocumentError) as raised:
            validate_run(None)
        self.assertEqual(raised.exception.code, "invalid_run")


if __name__ == "__main__":
    unittest.main()
