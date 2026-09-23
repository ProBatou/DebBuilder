import json
import unittest
from pathlib import Path

from debbuilder.recipe_schema import (
    RecipeDocumentError,
    automation_eligible,
    normalize_recipe,
    recipe_document_for_storage,
    recipe_for_storage,
    runtime_recipe_for_storage,
    validate_recipe_metadata,
)


def current(name="demo", **values):
    return {"schema_version": 5, "name": name, **values}


class RecipeSchemaTests(unittest.TestCase):
    def test_v5_fixtures_and_builtin_shape_round_trip_canonically(self):
        fixtures = Path(__file__).parent / "fixtures" / "recipes"
        for path in sorted(fixtures.glob("*.json")):
            with self.subTest(recipe=path.name):
                source = json.loads(path.read_text())
                self.assertEqual(source["schema_version"], 5)
                stored = recipe_document_for_storage(source)
                self.assertEqual(stored["schema_version"], 5)
                self.assertEqual(recipe_document_for_storage(stored), stored)

    def test_only_schema_v5_is_supported(self):
        for version in (None, 0, 1, 2, 3, 4, 6, True, "5"):
            with self.subTest(version=version):
                document = {"name": "unsupported"}
                if version is not None:
                    document["schema_version"] = version
                with self.assertRaises(RecipeDocumentError) as raised:
                    recipe_document_for_storage(document)
                self.assertEqual(raised.exception.code, "unsupported_recipe_schema")
                self.assertEqual(raised.exception.path, "$.schema_version")

    def test_json_document_rejects_bad_roots_missing_ids_and_unknown_fields(self):
        cases = [
            ([], "invalid_root"),
            (None, "invalid_root"),
            ({"schema_version": 5, "package": {"name": "demo"}}, "missing_id"),
            (current(unexpected=True), "unknown_field"),
            (current(package=[]), "invalid_recipe"),
        ]
        for value, code in cases:
            with self.subTest(value=value), self.assertRaises(RecipeDocumentError) as raised:
                recipe_document_for_storage(value)
            self.assertEqual(raised.exception.code, code)

    def test_only_executable_version_sources_are_accepted(self):
        for source in ("tag", "release_name", "regex"):
            version = {"source": source}
            if source == "regex":
                version["expression"] = r"([0-9]+(?:\.[0-9]+)*)"
            with self.subTest(source=source):
                stored = recipe_document_for_storage(current(source={"version": version}))
                self.assertEqual(stored["source"]["version"]["source"], source)

        unsupported = current(source={"version": {"source": "build"}})
        for validator in (validate_recipe_metadata, recipe_for_storage, runtime_recipe_for_storage, recipe_document_for_storage):
            with self.subTest(validator=validator.__name__), self.assertRaises(RecipeDocumentError) as raised:
                validator(unsupported)
            self.assertEqual(raised.exception.code, "unsupported_version_source")
            self.assertEqual(raised.exception.path, "$.source.version.source")
            self.assertNotIn("fallback", str(raised.exception).lower())

    def test_removed_authoring_aliases_are_rejected(self):
        cases = [
            (current(build={"timeout": 120}), "$.build.timeout"),
            (current(install={"config_policy": "replace"}), "$.install.config_policy"),
            (current(service={"configured": False}), "$.service.configured"),
            (current(steps=[]), "$.steps"),
            (current(artifact={"mode": "upstream_archive", "selected_files": ["bin/demo"]}), "$.artifact.selected_files"),
            (current(artifact={
                "mode": "upstream_archive", "archive_source": "github_source",
                "payload": {"mode": "paths", "include": ["bin/demo"], "exclude": [], "legacy_file_layout": "basename"},
            }), "$.artifact.payload.legacy_file_layout"),
        ]
        for document, path in cases:
            for validator in (validate_recipe_metadata, recipe_for_storage, recipe_document_for_storage):
                with self.subTest(path=path, validator=validator.__name__), self.assertRaises(RecipeDocumentError) as raised:
                    validator(document)
                self.assertEqual(raised.exception.code, "unknown_field")
                self.assertEqual(raised.exception.path, path)

    def test_string_configuration_entries_are_rejected(self):
        with self.assertRaises(RecipeDocumentError) as raised:
            recipe_document_for_storage(current(install={"config_files": ["/etc/demo.conf"]}))
        self.assertEqual(raised.exception.code, "invalid_recipe")

    def test_canonical_defaults_have_one_v5_owner(self):
        recipe = normalize_recipe(current(
            "demo-recipe",
            package={"name": "demo"},
            source={"repository": "owner/demo", "tracking": "latest_release", "version": {"source": "tag"}},
        ))
        self.assertEqual(recipe["schema_version"], 5)
        self.assertEqual(recipe["automation"], {"enabled": False, "policy": "manual"})
        self.assertEqual(recipe["runtime_apt_repositories"], [])
        self.assertTrue(all(value is None for value in recipe["resource_limits"].values()))
        self.assertEqual(recipe["package"]["version_revision"], "1")
        self.assertEqual(recipe["build"]["inactivity_timeout"], 300)
        self.assertIsNone(recipe["build"]["maximum_runtime"])
        self.assertEqual(recipe["install"]["destination"], "/opt/demo")

    def test_archive_payload_and_source_modes_are_canonical(self):
        source_archive = recipe_document_for_storage(current(
            package={"name": "demo"},
            source={"repository": "owner/demo"},
            artifact={
                "mode": "upstream_archive", "archive_source": "github_source", "archive_format": "tar.gz",
                "payload": {
                    "mode": "paths",
                    "include": ["static/js/app.js", "server.py", "static/"],
                    "exclude": ["static/dev/"],
                },
            },
        ))
        self.assertEqual(source_archive["artifact"]["payload"], {
            "mode": "paths", "include": ["server.py", "static/"], "exclude": ["static/dev/"],
        })
        self.assertEqual(source_archive["artifact"]["archive_source"], "github_source")
        self.assertNotIn("selected_files", source_archive["artifact"])

        release_asset = validate_recipe_metadata(current(
            "release-asset",
            package={"name": "release-asset"}, source={"repository": "owner/release-asset"},
            artifact={
                "mode": "upstream_archive", "archive_source": "release_asset", "asset_selection": "exact",
                "asset_name": "release.tar.gz", "payload": {"mode": "paths", "include": ["bin/release"], "exclude": []},
            },
        ))
        self.assertEqual(release_asset["artifact"]["asset_name"], "release.tar.gz")

    def test_current_install_mapping_accounts_directories_and_systemd_are_preserved(self):
        recipe = validate_recipe_metadata(current(
            package={"name": "demo", "architecture": "amd64"},
            source={"repository": "owner/demo"},
            install={
                "content": {"source": "configured_files"},
                "owner": {"user": "root", "group": "root"},
                "account": {"user": "demo", "group": "demo", "create_user": True, "create_group": True},
                "directories": [{"path": "/var/lib/demo", "owner": "demo", "group": "demo", "mode": "0750"}],
                "config_files": [{
                    "source": "bin/demo", "destination": "/usr/bin/demo", "policy": "replace",
                    "owner": "root", "group": "root", "mode": "0755",
                }],
            },
            service={
                "enabled": True, "name": "demo.service", "command": "/usr/bin/demo",
                "user": "demo", "group": "demo", "conflicts": ["other.service"],
                "limit_nofile": "65536", "kill_mode": "process", "syslog_identifier": "demo",
                "ambient_capabilities": ["CAP_NET_BIND_SERVICE"],
            },
        ))
        self.assertEqual(recipe["install"]["config_files"][0]["mode"], "0755")
        self.assertEqual(recipe["install"]["account"]["user"], "demo")
        self.assertTrue(recipe["service"]["configured"])
        self.assertEqual(recipe["service"]["ambient_capabilities"], ["CAP_NET_BIND_SERVICE"])
        stored = runtime_recipe_for_storage(recipe)
        self.assertNotIn("configured", stored["service"])
        self.assertTrue(validate_recipe_metadata(stored)["service"]["configured"])
        recipe["service"]["configured"] = False
        with self.assertRaisesRegex(RecipeDocumentError, "does not match"):
            runtime_recipe_for_storage(recipe)

    def test_current_path_and_systemd_safety_checks_remain_fail_closed(self):
        cases = [
            {"install": {"destination": "/usr/bin/../../tmp"}},
            {"install": {"directories": [{"path": "/var/lib/other", "owner": "root", "group": "root"}]}},
            {"service": {"name": "demo.service", "command": "/bin/true", "conflicts": ["bad"]}},
            {"service": {"name": "demo.service", "command": "/bin/true", "ambient_capabilities": ["NET_ADMIN"]}},
            {"build": {"working_directory": "../outside"}},
            {"build": {"source_changes": [{"operation": "patch", "path": "a.txt"}]}},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_recipe_metadata(current(package={"name": "demo"}, **changes))

    def test_build_timeout_and_output_contract(self):
        recipe = validate_recipe_metadata(current(
            build={"inactivity_timeout": None, "maximum_runtime": 900, "output": {"mode": "paths", "paths": ["dist", "public"]}},
        ))
        self.assertIsNone(recipe["build"]["inactivity_timeout"])
        self.assertEqual(recipe["build"]["maximum_runtime"], 900)
        self.assertEqual(runtime_recipe_for_storage(recipe)["build"]["output"], {"mode": "paths", "paths": ["dist", "public"]})
        for value in (0, -1, "0", "-1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                validate_recipe_metadata(current(build={"inactivity_timeout": value}))

    def test_config_mapping_policy_is_per_mapping(self):
        mappings = [
            {"source": "dist/foo", "destination": "/usr/bin/foo", "policy": "replace"},
            {"source": "config/foo.conf", "destination": "/etc/foo/foo.conf", "policy": "dpkg_conffile"},
        ]
        stored = recipe_document_for_storage(current(
            "mapped", package={"name": "mapped"},
            install={"content": {"source": "configured_files"}, "config_files": mappings},
        ))
        self.assertEqual(stored["install"]["config_files"], mappings)
        with self.assertRaisesRegex(ValueError, "unsupported configuration policy"):
            validate_recipe_metadata(current(
                "mapped", install={"config_files": [{"source": "bad", "destination": "/etc/bad", "policy": "unknown"}]},
            ))

    def test_automation_resource_and_runtime_apt_fields_round_trip(self):
        public_key = """-----BEGIN PGP PUBLIC KEY BLOCK-----

dGhpcy1pcy1zdHJ1Y3R1cmFsbHktcHVibGljLWtleS1kYXRh
-----END PGP PUBLIC KEY BLOCK-----
"""
        configured = recipe_document_for_storage(current(
            "automated", active=True,
            automation={"enabled": True, "policy": "full"},
            resource_limits={
                "memory_max_bytes": 536870912, "tasks_max": 128, "cpu_quota_percent": 150,
                "io_read_bandwidth_max_bytes_per_sec": None, "io_write_bandwidth_max_bytes_per_sec": None,
            },
            runtime_apt_repositories=[{
                "id": "vendor", "uri": "https://packages.example.test", "suite": "stable",
                "components": ["main"], "signing_key": {"armored": public_key},
            }],
        ))
        self.assertEqual(configured["automation"], {"enabled": True, "policy": "full"})
        self.assertEqual(configured["resource_limits"]["tasks_max"], 128)
        self.assertEqual(configured["runtime_apt_repositories"][0]["id"], "vendor")
        self.assertEqual(recipe_document_for_storage(configured), configured)

    def test_automation_policy_is_strict_and_active_is_master_switch(self):
        for automation in (
            {"enabled": "yes", "policy": "build"},
            {"enabled": True, "policy": "everything"},
            {"enabled": True, "policy": "build", "interval": 60},
            [],
        ):
            with self.subTest(automation=automation), self.assertRaises(RecipeDocumentError):
                recipe_document_for_storage(current("invalid", automation=automation))
        self.assertTrue(automation_eligible(current("eligible", active=True, automation={"enabled": True, "policy": "detect"})))
        self.assertFalse(automation_eligible(current("inactive", active=False, automation={"enabled": True, "policy": "full"})))


if __name__ == "__main__":
    unittest.main()
