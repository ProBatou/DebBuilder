import unittest

from debbuilder.recipe_schema import normalize_recipe, recipe_for_storage, validate_recipe_metadata


class RecipeSchemaTests(unittest.TestCase):
    def test_upstream_archive_fhs_account_directories_and_mapping_overrides(self):
        recipe = validate_recipe_metadata({
            "name": "demo", "package": {"name": "demo", "architecture": "amd64"},
            "source": {"repository": "owner/demo", "tracking": "latest_release"},
            "artifact": {"mode": "upstream_archive", "type": "archive", "asset_name": "demo.tar.gz", "selected_files": ["demo"]},
            "install": {"content": {"source": "configured_files"}, "owner": {"user": "root", "group": "root"}, "account": {"user": "demo", "group": "demo", "create_user": True, "create_group": True}, "directories": [{"path": "/var/lib/demo", "owner": "demo", "group": "demo", "mode": "0750"}], "config_files": [{"source": "demo", "destination": "/usr/bin/demo", "policy": "replace", "owner": "root", "group": "root", "mode": "0755"}]},
            "service": {"name": "demo.service", "command": "/usr/bin/demo", "conflicts": ["other.service"], "limit_nofile": "65536", "kill_mode": "process", "syslog_identifier": "demo", "ambient_capabilities": ["CAP_NET_BIND_SERVICE"]},
        })
        self.assertEqual(recipe["artifact"]["selected_files"], ["demo"])
        self.assertEqual(recipe["install"]["config_files"][0]["mode"], "0755")
        self.assertEqual(recipe["install"]["account"]["user"], "demo")
        self.assertEqual(recipe["service"]["ambient_capabilities"], ["CAP_NET_BIND_SERVICE"])

    def test_upstream_archive_source_modes_do_not_require_release_asset_fields(self):
        source_archive = validate_recipe_metadata({
            "name": "demo", "package": {"name": "demo"},
            "source": {"repository": "owner/demo"},
            "artifact": {"mode": "upstream_archive", "archive_source": "github_source", "archive_format": "tar.gz", "selected_files": ["demo"]},
        })
        self.assertEqual(source_archive["artifact"]["archive_source"], "github_source")
        self.assertEqual(source_archive["artifact"]["asset_name"], "")
        self.assertEqual(source_archive["artifact"]["name_pattern"], "")
        legacy_asset = validate_recipe_metadata({
            "name": "legacy", "package": {"name": "legacy"},
            "source": {"repository": "owner/legacy"},
            "artifact": {"mode": "upstream_archive", "asset_name": "legacy.tar.gz", "selected_files": ["legacy"]},
        })
        self.assertEqual(legacy_asset["artifact"]["archive_source"], "release_asset")
        self.assertEqual(legacy_asset["artifact"]["asset_selection"], "exact")

    def test_fhs_and_advanced_systemd_validation_rejects_unsafe_values(self):
        cases = [
            {"install": {"destination": "/usr/bin/../../tmp"}},
            {"install": {"directories": [{"path": "/var/lib/other", "owner": "root", "group": "root"}]}},
            {"service": {"name": "demo.service", "command": "/bin/true", "conflicts": ["bad"]}},
            {"service": {"name": "demo.service", "command": "/bin/true", "ambient_capabilities": ["NET_ADMIN"]}},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_recipe_metadata({"name": "demo", "package": {"name": "demo"}, **changes})

        with self.assertRaisesRegex(ValueError, "artifact type"):
            validate_recipe_metadata({"name": "demo", "package": {"name": "demo"}, "artifact": {"mode": "source_build", "type": "zip"}})

    def test_canonical_recipe_defaults_are_applied(self):
        recipe = normalize_recipe({
            "name": "demo-recipe", "package": {"name": "demo"},
            "source": {"repository": "owner/demo", "tracking": "latest_release", "version": {"source": "tag"}},
        })
        self.assertEqual(recipe["schema_version"], 1)
        self.assertEqual(recipe["package"]["name"], "demo")
        self.assertEqual(recipe["source"]["repository"], "owner/demo")
        self.assertEqual(recipe["install"]["destination"], "/opt/demo")
        self.assertEqual(recipe["build"]["output"], {"mode": "source", "path": ""})
        self.assertNotEqual(recipe["install"]["owner"], recipe["service"])

    def test_legacy_build_timeout_migrates_to_inactivity_timeout(self):
        recipe = validate_recipe_metadata({
            "name": "legacy-timeout", "package": {"name": "legacy-timeout"}, "source": {"repository": "owner/demo"},
            "build": {"timeout": 120, "commands": ["make"], "output": {"mode": "source"}},
        })
        self.assertEqual(recipe["build"]["inactivity_timeout"], 120)
        self.assertIsNone(recipe["build"]["maximum_runtime"])
        stored = recipe_for_storage(recipe)
        self.assertEqual(stored["build"]["inactivity_timeout"], 120)
        self.assertNotIn("timeout", stored["build"])

    def test_new_build_timeouts_have_generic_defaults(self):
        recipe = validate_recipe_metadata({"name": "demo", "package": {"name": "demo"}, "source": {"repository": "owner/demo"}})
        self.assertEqual(recipe["build"]["inactivity_timeout"], 300)
        self.assertIsNone(recipe["build"]["maximum_runtime"])

    def test_storage_and_read_shapes_are_canonical(self):
        stored = recipe_for_storage({"name": "demo", "package": {"name": "demo"}, "source": {"repository": "owner/demo"}})
        self.assertIn("package", stored)
        self.assertIn("source", stored)
        self.assertNotIn("package_name", stored)
        self.assertNotIn("github_repository", stored)
        loaded = normalize_recipe(stored)
        self.assertEqual(loaded["package"]["name"], "demo")
        self.assertEqual(loaded["source"]["repository"], "owner/demo")

    def test_storage_preserves_each_build_output_mode_without_hidden_fields(self):
        paths = ["package.json", "node_modules", ".next", "dist"]
        stored_paths = recipe_for_storage({"name": "demo", "build": {"output": {"mode": "paths", "paths": paths}}})
        self.assertEqual(stored_paths["build"]["output"], {"mode": "paths", "paths": paths})
        self.assertEqual(recipe_for_storage(stored_paths)["build"]["output"], {"mode": "paths", "paths": paths})
        self.assertEqual(recipe_for_storage({"name": "demo", "build": {"output": {"mode": "source"}}})["build"]["output"], {"mode": "source"})
        self.assertEqual(recipe_for_storage({"name": "demo", "build": {"output": {"mode": "path", "path": "dist"}}})["build"]["output"], {"mode": "path", "path": "dist"})

    def test_complete_recipe_preserves_independent_install_and_service_owners(self):
        recipe = validate_recipe_metadata({
            "schema_version": 1, "name": "demo", "active": True,
            "package": {"name": "demo", "architecture": "all"},
            "source": {"repository": "owner/demo"},
            "build": {"working_directory": "frontend", "output": {"mode": "source"}, "commands": ["npm ci"]},
            "install": {"owner": {"user": "root", "group": "root"}},
            "service": {"configured": True, "enabled": False, "user": "demo", "group": "demo"},
        })
        self.assertEqual(recipe["install"]["owner"]["user"], "root")
        self.assertEqual(recipe["service"]["user"], "demo")
        self.assertEqual(recipe["build"]["output"], {"mode": "source", "path": ""})

    def test_validation_rejects_unsafe_paths_and_unknown_source_changes(self):
        base = {"name": "demo", "package": {"name": "demo"}, "source": {"repository": "owner/demo"}}
        with self.assertRaisesRegex(ValueError, "working_directory"):
            validate_recipe_metadata({**base, "build": {"working_directory": "../outside"}})
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            validate_recipe_metadata({**base, "build": {"source_changes": [{"operation": "patch", "path": "a.txt"}]}})

    def test_validation_rejects_invalid_nested_types(self):
        with self.assertRaisesRegex(ValueError, "build.environment"):
            validate_recipe_metadata({"name": "demo", "package": {"name": "demo"}, "build": {"environment": ["A=B"]}})

    def test_static_and_configuration_source_mappings_are_preserved(self):
        recipe = validate_recipe_metadata({
            "name": "static-demo", "package": {"name": "static-demo"}, "source": {"repository": "owner/demo"},
            "build": {"detected_project": "static", "output": {"mode": "source"}},
            "install": {"content": {"source": "configured_files"}, "config_files": [{"source": ".bashrc", "destination": "/root/.bashrc"}]},
        })
        self.assertEqual(recipe["build"]["detected_project"], "static")
        self.assertEqual(recipe["install"]["content"]["source"], "configured_files")
        self.assertEqual(recipe["install"]["config_files"][0]["source"], ".bashrc")
        self.assertEqual(recipe["install"]["config_files"][0]["policy"], "dpkg_conffile")
        self.assertEqual(recipe["install"]["destination"], "")
        self.assertFalse(recipe["install"]["owner"]["create_user"])
        self.assertFalse(recipe["install"]["owner"]["create_group"])

    def test_custom_mapping_round_trip_reuses_install_config_files(self):
        mappings = [
            {"source": "dist/foo", "destination": "/usr/bin/foo"},
            {"source": "config/foo.conf", "destination": "/etc/foo/foo.conf"},
        ]
        stored = recipe_for_storage({
            "name": "mapped", "package": {"name": "mapped"},
            "install": {"content": {"source": "configured_files"}, "config_files": mappings, "config_policy": "replace"},
        })
        self.assertEqual(stored["install"]["content"]["source"], "configured_files")
        expected = [{**mapping, "policy": "replace"} for mapping in mappings]
        self.assertEqual(stored["install"]["config_files"], expected)
        self.assertNotIn("config_policy", stored["install"])
        self.assertEqual(recipe_for_storage(stored)["install"]["config_files"], expected)

    def test_mapping_policy_is_local_and_overrides_legacy_global_policy(self):
        stored = recipe_for_storage({
            "name": "mapped", "package": {"name": "mapped"},
            "install": {"config_policy": "replace", "config_files": [
                "/etc/mapped/legacy.conf",
                {"source": "owned.sh", "destination": "/etc/profile.d/owned.sh", "policy": "dpkg_conffile"},
            ]},
        })
        self.assertEqual([row["policy"] for row in stored["install"]["config_files"]], ["replace", "dpkg_conffile"])
        with self.assertRaisesRegex(ValueError, "unsupported configuration policy"):
            validate_recipe_metadata({"name": "mapped", "install": {"config_files": [
                {"source": "bad", "destination": "/etc/bad", "policy": "unknown"},
            ]}})

    def test_root_accounts_are_never_created(self):
        recipe = validate_recipe_metadata({"name": "demo", "install": {"owner": {
            "user": "root", "group": "root", "create_user": True, "create_group": True,
        }}})
        self.assertFalse(recipe["install"]["owner"]["create_user"])
        self.assertFalse(recipe["install"]["owner"]["create_group"])

    def test_unconfigured_service_has_no_fictitious_defaults(self):
        recipe = validate_recipe_metadata({"name": "demo", "package": {"name": "demo"}, "source": {"repository": "owner/demo"}, "service": {"enabled": False}})
        self.assertFalse(recipe["service"]["configured"])
        self.assertFalse(recipe["service"]["enabled"])
        for key in ("name", "user", "group", "type", "restart", "command"):
            self.assertEqual(recipe["service"][key], "")

    def test_service_configuration_is_derived_and_not_stored_as_a_second_state(self):
        complete = validate_recipe_metadata({"name": "demo", "service": {"configured": False, "enabled": False, "name": "demo.service", "command": "/usr/bin/demo"}})
        self.assertTrue(complete["service"]["configured"])
        stored = recipe_for_storage(complete)
        self.assertNotIn("configured", stored["service"])
        self.assertTrue(validate_recipe_metadata(stored)["service"]["configured"])
        partial = validate_recipe_metadata({"name": "demo", "service": {"configured": True, "user": "demo"}})
        self.assertFalse(partial["service"]["configured"])
        self.assertEqual(partial["service"]["user"], "demo")


if __name__ == "__main__":
    unittest.main()
