import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.resource_limits import ResourceLimitError, empty_policy
from debbuilder.settings_service import update_settings, validate_app_settings_storage
from debbuilder.settings_store import (
    SETTINGS_SCHEMA_VERSION,
    SettingsDocumentError,
    default_settings,
    load_secrets,
    load_settings,
    save_secrets,
    save_settings,
    validate_secrets_document,
    validate_settings,
)


class ResourceSettingsTests(unittest.TestCase):
    def defaults(self):
        return default_settings("http://localhost/debian", "stable", "main", "amd64")

    def test_fresh_install_uses_canonical_defaults_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            loaded = load_settings(root, self.defaults())
            self.assertFalse((root / "settings.json").exists())
        self.assertEqual(loaded["schema_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(loaded["resource_limits"], empty_policy())

    def test_startup_storage_validation_is_read_only_on_fresh_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            loaded = validate_app_settings_storage(root, self.defaults())
            self.assertEqual(loaded["schema_version"], SETTINGS_SCHEMA_VERSION)
            self.assertEqual(list(root.iterdir()), [])

    def test_startup_requires_oidc_client_secret_without_repairing_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = self.defaults()
            settings["security"] = {
                "auth_mode": "oidc",
                "oidc_issuer": "https://id.example.test",
                "oidc_client_id": "debbuilder",
                "oidc_redirect_uri": "https://apt.example.test/auth/callback",
            }
            save_settings(root, settings)
            save_secrets(root, {"schema_version": 1, "session": {"cookie_secret": "s" * 48}})
            settings_before = (root / "settings.json").read_bytes()
            secrets_before = (root / "secrets.json").read_bytes()
            with mock.patch.dict("os.environ", {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}), \
                    mock.patch("debbuilder.settings_service.save_secrets") as write_secrets, \
                    self.assertRaises(SettingsDocumentError) as raised:
                validate_app_settings_storage(root, self.defaults())
            self.assertEqual(raised.exception.code, "missing_required_secret")
            self.assertEqual(raised.exception.path, "$.security.oidc_client_secret")
            write_secrets.assert_not_called()
            self.assertEqual((root / "settings.json").read_bytes(), settings_before)
            self.assertEqual((root / "secrets.json").read_bytes(), secrets_before)

            save_secrets(root, {"schema_version": 1, "oidc": {"client_secret": "client-only-secret"}})
            secrets_before = (root / "secrets.json").read_bytes()
            with mock.patch.dict("os.environ", {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}):
                self.assertEqual(validate_app_settings_storage(root, self.defaults()), settings)
            self.assertEqual((root / "secrets.json").read_bytes(), secrets_before)
            self.assertEqual(stat.S_IMODE((root / "secrets.json").stat().st_mode), 0o600)

    def test_non_oidc_startup_does_not_require_oidc_client_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_settings(root, self.defaults())
            save_secrets(root, {"schema_version": 1, "session": {"cookie_secret": "s" * 48}})
            with mock.patch.dict("os.environ", {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}):
                self.assertEqual(validate_app_settings_storage(root, self.defaults()), self.defaults())

    def test_startup_rejects_malformed_secrets_without_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_settings(root, self.defaults())
            path = root / "secrets.json"
            path.write_bytes(b"{")
            path.chmod(0o600)
            with self.assertRaises(SettingsDocumentError):
                validate_app_settings_storage(root, self.defaults())
            self.assertEqual(path.read_bytes(), b"{")

    def test_canonical_settings_round_trip_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = {**empty_policy(), "tasks_max": 96, "cpu_quota_percent": 250}
            updated = validate_settings({"resource_limits": policy}, self.defaults())
            save_settings(root, updated)
            loaded = load_settings(root, self.defaults())
            stored = json.loads((root / "settings.json").read_text())
        self.assertEqual(loaded, updated)
        self.assertEqual(stored["schema_version"], SETTINGS_SCHEMA_VERSION)
        self.assertNotIn("github", stored)
        self.assertNotIn("configured", stored["notifications"])

    def test_partial_update_preserves_omitted_existing_limits(self):
        current = self.defaults()
        current["resource_limits"] = {
            **empty_policy(), "memory_max_bytes": 1024, "tasks_max": 32,
        }
        updated = validate_settings({"resource_limits": {"tasks_max": 16}}, current)
        self.assertEqual(updated["resource_limits"]["memory_max_bytes"], 1024)
        self.assertEqual(updated["resource_limits"]["tasks_max"], 16)

    def test_unversioned_partial_and_future_documents_are_rejected_without_rewrite(self):
        documents = [
            {"general": {"app_name": "Legacy"}},
            {**self.defaults(), "schema_version": SETTINGS_SCHEMA_VERSION + 1},
            {**self.defaults(), "automation": {"upstream_checks_enabled": True}},
        ]
        for document in documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                raw = json.dumps(document)
                (root / "settings.json").write_text(raw)
                with self.assertRaises(SettingsDocumentError):
                    load_settings(root, self.defaults())
                self.assertEqual((root / "settings.json").read_text(), raw)

    def test_malformed_document_is_rejected_without_default_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text("{")
            with self.assertRaises(SettingsDocumentError) as raised:
                load_settings(root, self.defaults())
        self.assertEqual(raised.exception.code, "invalid_settings_json")
        self.assertEqual(raised.exception.path, "$")

    def test_unknown_persisted_and_update_fields_are_rejected_at_exact_path(self):
        stored = {**self.defaults(), "legacy": True}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(json.dumps(stored))
            with self.assertRaises(SettingsDocumentError) as raised:
                load_settings(root, self.defaults())
        self.assertEqual(raised.exception.path, "$.legacy")

        for payload, path in (
            ({"legacy": True}, "$.legacy"),
            ({"general": {"legacy": True}}, "$.general.legacy"),
            ({"github": {"token_configured": True}}, "$.github.token_configured"),
            ({"security": {"pocket_id_active": False}}, "$.security.pocket_id_active"),
        ):
            with self.subTest(path=path), self.assertRaises(SettingsDocumentError) as raised:
                validate_settings(payload, self.defaults())
            self.assertEqual(raised.exception.path, path)

    def test_update_types_are_strict(self):
        for payload, path in (
            ({"general": {"app_name": 7}}, "$.general.app_name"),
            ({"automation": {"upstream_checks_enabled": 1}}, "$.automation.upstream_checks_enabled"),
            ({"automation": {"auto_validate_after_successful_build": "false"}}, "$.automation.auto_validate_after_successful_build"),
            ({"security": {"auth_mode": False}}, "$.security.auth_mode"),
        ):
            with self.subTest(path=path), self.assertRaises(SettingsDocumentError) as raised:
                validate_settings(payload, self.defaults())
            self.assertEqual(raised.exception.path, path)

    def test_post_validation_uses_canonical_resource_field_errors(self):
        with self.assertRaises(ResourceLimitError) as raised:
            validate_settings({"resource_limits": {"memory_max_bytes": False}}, self.defaults())
        self.assertEqual(raised.exception.path, "$.resource_limits.memory_max_bytes")

    def test_scheduler_defaults_and_bounds_are_canonical(self):
        automation = self.defaults()["automation"]
        self.assertEqual(automation["upstream_check_interval_seconds"], 3600)
        self.assertEqual(automation["upstream_check_concurrency"], 4)
        self.assertTrue(automation["upstream_checks_enabled"])
        for field, values in (
            ("upstream_check_interval_seconds", (59, 86401, True)),
            ("upstream_check_concurrency", (0, 9, True)),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(SettingsDocumentError):
                    validate_settings({"automation": {field: value}}, self.defaults())
        updated = validate_settings({"automation": {
            "upstream_checks_enabled": False,
            "upstream_check_interval_seconds": 60,
            "upstream_check_concurrency": 8,
        }}, self.defaults())
        self.assertFalse(updated["automation"]["upstream_checks_enabled"])

    def test_invalid_persisted_scheduler_tuning_is_rejected_not_defaulted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stored = self.defaults()
            stored["automation"]["upstream_check_interval_seconds"] = 1
            (root / "settings.json").write_text(json.dumps(stored))
            with self.assertRaises(SettingsDocumentError) as raised:
                load_settings(root, self.defaults())
        self.assertEqual(raised.exception.path, "$.automation.upstream_check_interval_seconds")

    def test_secret_store_is_versioned_and_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_secrets(root, {"schema_version": 1, "github": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}})
            stored = json.loads((root / "secrets.json").read_text())
            self.assertEqual(stored["schema_version"], 1)
            self.assertEqual(load_secrets(root)["github"]["token"], "ghp_abcdefghijklmnopqrstuvwxyz123456")
            self.assertEqual(stat.S_IMODE((root / "secrets.json").stat().st_mode), 0o600)
            stored["legacy"] = {"token": "not-supported"}
            raw = json.dumps(stored)
            (root / "secrets.json").write_text(raw)
            with self.assertRaises(SettingsDocumentError) as raised:
                load_secrets(root)
            self.assertEqual(raised.exception.path, "$.legacy")
            self.assertEqual((root / "secrets.json").read_text(), raw)

    def test_unversioned_and_future_secrets_are_rejected_without_rewrite(self):
        for document in (
            {"github": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}},
            {"schema_version": 2},
        ):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / "secrets.json"
                raw = json.dumps(document).encode()
                path.write_bytes(raw)
                path.chmod(0o600)
                with self.assertRaises(SettingsDocumentError):
                    load_secrets(root)
                self.assertEqual(path.read_bytes(), raw)

    def test_literal_masked_is_never_a_valid_persisted_secret(self):
        for section, field in (
            ("github", "token"),
            ("notifications", "token"),
            ("oidc", "client_secret"),
            ("session", "cookie_secret"),
        ):
            document = {"schema_version": 1, section: {field: "masked"}}
            with self.subTest(section=section), self.assertRaises(SettingsDocumentError) as raised:
                validate_secrets_document(document)
            self.assertEqual(raised.exception.path, f"$.{section}.{field}")

    def test_resource_repair_rejects_missing_oidc_secret_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = self.defaults()
            settings["security"] = {
                "auth_mode": "oidc",
                "oidc_issuer": "https://id.example.test",
                "oidc_client_id": "debbuilder",
                "oidc_redirect_uri": "https://apt.example.test/auth/callback",
            }
            settings["resource_limits"]["tasks_max"] = "bad"
            path = root / "settings.json"
            original = json.dumps(settings).encode()
            path.write_bytes(original)
            with mock.patch.dict("os.environ", {"DEBBUILDER_OIDC_CLIENT_SECRET": ""}), \
                    self.assertRaises(SettingsDocumentError) as raised:
                update_settings(root, {"resource_limits": {"tasks_max": 32}}, self.defaults())
            self.assertEqual(raised.exception.code, "missing_required_secret")
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "secrets.json").exists())

    def test_resource_repair_rejects_unrelated_invalid_settings_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = self.defaults()
            settings["general"]["app_name"] = ""
            settings["resource_limits"]["tasks_max"] = "bad"
            path = root / "settings.json"
            original = json.dumps(settings).encode()
            path.write_bytes(original)

            with self.assertRaises(SettingsDocumentError) as raised:
                update_settings(root, {"resource_limits": {"tasks_max": 32}}, self.defaults())

            self.assertEqual(raised.exception.path, "$.general.app_name")
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "secrets.json").exists())


if __name__ == "__main__":
    unittest.main()
