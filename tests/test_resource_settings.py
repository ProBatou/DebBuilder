import json
import tempfile
import unittest
from pathlib import Path

from debbuilder.settings_store import default_settings, load_settings_result, save_settings, validate_settings
from debbuilder.resource_limits import ResourceLimitError, empty_policy


class ResourceSettingsTests(unittest.TestCase):
    def defaults(self):
        return default_settings("http://localhost/debian", "stable", "main", "amd64")

    def test_legacy_settings_without_section_are_unlimited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(json.dumps({"general": {"app_name": "Legacy"}}))
            loaded = load_settings_result(root, self.defaults())
        self.assertIsNone(loaded.resource_limits_error)
        self.assertEqual(loaded.settings["resource_limits"], empty_policy())

    def test_valid_null_and_finite_values_persist_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = {**empty_policy(), "tasks_max": 96, "cpu_quota_percent": 250}
            updated = validate_settings({"resource_limits": policy}, self.defaults())
            save_settings(root, updated)
            loaded = load_settings_result(root, self.defaults())
        self.assertIsNone(loaded.resource_limits_error)
        self.assertEqual(loaded.settings["resource_limits"], policy)

    def test_partial_update_preserves_omitted_existing_limits(self):
        current = self.defaults()
        current["resource_limits"] = {
            **empty_policy(), "memory_max_bytes": 1024, "tasks_max": 32,
        }
        updated = validate_settings({"resource_limits": {"tasks_max": 16}}, current)
        self.assertEqual(updated["resource_limits"]["memory_max_bytes"], 1024)
        self.assertEqual(updated["resource_limits"]["tasks_max"], 16)

    def test_malformed_section_is_diagnostic_not_silently_unlimited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(json.dumps({"resource_limits": {"tasks_max": "64"}}))
            loaded = load_settings_result(root, self.defaults())
        self.assertEqual(loaded.settings["resource_limits"], empty_policy())
        self.assertEqual(loaded.resource_limits_error["code"], "invalid_resource_limit")
        self.assertEqual(loaded.resource_limits_error["path"], "$.resource_limits.tasks_max")

    def test_present_null_section_is_malformed_and_post_rejects_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(json.dumps({"resource_limits": None}))
            loaded = load_settings_result(root, self.defaults())
        self.assertEqual(loaded.resource_limits_error["code"], "invalid_resource_limits")
        self.assertEqual(loaded.resource_limits_error["path"], "$.resource_limits")
        with self.assertRaises(ResourceLimitError) as raised:
            validate_settings({"resource_limits": None}, self.defaults())
        self.assertEqual(raised.exception.path, "$.resource_limits")

    def test_unreadable_document_blocks_authoritative_use_but_returns_defaults_for_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text("{")
            loaded = load_settings_result(root, self.defaults())
        self.assertEqual(loaded.resource_limits_error["code"], "resource_settings_unreadable")
        self.assertEqual(loaded.settings["resource_limits"], empty_policy())

    def test_post_validation_uses_canonical_field_errors(self):
        with self.assertRaises(ResourceLimitError) as raised:
            validate_settings({"resource_limits": {"memory_max_bytes": False}}, self.defaults())
        self.assertEqual(raised.exception.path, "$.resource_limits.memory_max_bytes")


if __name__ == "__main__":
    unittest.main()
