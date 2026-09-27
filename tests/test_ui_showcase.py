import unittest

from tests.ui.showcase import showcase_recipes


class UiShowcaseRecipeTests(unittest.TestCase):
    def test_archive_recipe_uses_canonical_v5_payload(self):
        recipe = showcase_recipes()["archive-agent"]

        self.assertEqual(recipe["schema_version"], 5)
        self.assertEqual(recipe["artifact"]["payload"], {
            "mode": "paths",
            "include": ["bin/archive-agent", "share/defaults.yml"],
            "exclude": [],
        })
        self.assertNotIn("selected_files", recipe["artifact"])
        self.assertTrue(recipe["package"]["runtime_dependency_detection"]["enabled"])

    def test_showcase_covers_resource_and_inert_automation_policies(self):
        recipes = showcase_recipes()
        self.assertEqual(recipes["worker-agent"]["resource_limits"]["tasks_max"], 48)
        self.assertEqual(recipes["release-tool"]["automation"], {"enabled": False, "policy": "test"})


if __name__ == "__main__":
    unittest.main()
