import unittest

from debbuilder.recipe_schema import normalize_recipe, recipe_for_storage, validate_recipe_metadata


class RecipeSchemaTests(unittest.TestCase):
    def test_flat_recipe_is_migrated_to_recipe_v1(self):
        recipe = normalize_recipe({
            "name": "demo-recipe", "package_name": "demo", "github_repository": "owner/demo",
            "version_tracking": "latest_release", "version_source": "tag", "steps": [],
        })
        self.assertEqual(recipe["schema_version"], 1)
        self.assertEqual(recipe["package"]["name"], "demo")
        self.assertEqual(recipe["source"]["repository"], "owner/demo")
        self.assertEqual(recipe["install"]["destination"], "/opt/demo")
        self.assertEqual(recipe["build"]["output"], {"mode": "source", "path": ""})
        self.assertNotEqual(recipe["install"]["owner"], recipe["service"])

    def test_storage_shape_is_canonical_but_read_shape_has_compatibility_aliases(self):
        stored = recipe_for_storage({"name": "demo", "package_name": "demo", "github_repository": "owner/demo"})
        self.assertIn("package", stored)
        self.assertIn("source", stored)
        self.assertNotIn("package_name", stored)
        self.assertNotIn("github_repository", stored)
        loaded = normalize_recipe(stored)
        self.assertEqual(loaded["package_name"], "demo")

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

    def test_legacy_step_package_is_preserved_during_migration(self):
        recipe = normalize_recipe({"name": "old", "steps": [{"type": "init_deb_package", "package": "real-name"}]})
        self.assertEqual(recipe["package"]["name"], "real-name")
        self.assertEqual(recipe["steps"][0]["type"], "init_deb_package")

    def test_validation_rejects_unsafe_paths_and_unknown_source_changes(self):
        base = {"name": "demo", "package_name": "demo", "github_repository": "owner/demo"}
        with self.assertRaisesRegex(ValueError, "working_directory"):
            validate_recipe_metadata({**base, "build": {"working_directory": "../outside"}})
        with self.assertRaisesRegex(ValueError, "unsupported operation"):
            validate_recipe_metadata({**base, "build": {"source_changes": [{"operation": "patch", "path": "a.txt"}]}})

    def test_validation_rejects_invalid_nested_types(self):
        with self.assertRaisesRegex(ValueError, "build.environment"):
            validate_recipe_metadata({"name": "demo", "package_name": "demo", "build": {"environment": ["A=B"]}})

    def test_static_and_configuration_source_mappings_are_preserved(self):
        recipe = validate_recipe_metadata({
            "name": "static-demo", "package_name": "static-demo", "github_repository": "owner/demo",
            "build": {"detected_project": "static", "output": {"mode": "source"}},
            "install": {"content": {"source": "configured_files"}, "config_files": [{"source": ".bashrc", "destination": "/root/.bashrc"}]},
        })
        self.assertEqual(recipe["build"]["detected_project"], "static")
        self.assertEqual(recipe["install"]["content"]["source"], "configured_files")
        self.assertEqual(recipe["install"]["config_files"][0]["source"], ".bashrc")
        self.assertEqual(recipe["install"]["destination"], "")
        self.assertFalse(recipe["install"]["owner"]["create_user"])
        self.assertFalse(recipe["install"]["owner"]["create_group"])

    def test_unconfigured_service_has_no_fictitious_defaults(self):
        recipe = validate_recipe_metadata({"name": "demo", "package_name": "demo", "github_repository": "owner/demo", "service": {"enabled": False}})
        self.assertFalse(recipe["service"]["configured"])
        self.assertFalse(recipe["service"]["enabled"])
        for key in ("name", "user", "group", "type", "restart", "command"):
            self.assertEqual(recipe["service"][key], "")


if __name__ == "__main__":
    unittest.main()
