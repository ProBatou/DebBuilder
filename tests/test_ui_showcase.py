import unittest

from tests.ui.showcase import showcase_recipes


class UiShowcaseRecipeTests(unittest.TestCase):
    def test_archive_recipe_uses_canonical_v3_payload(self):
        recipe = showcase_recipes()["archive-agent"]

        self.assertEqual(recipe["schema_version"], 3)
        self.assertEqual(recipe["artifact"]["payload"], {
            "mode": "paths",
            "include": ["bin/archive-agent", "share/defaults.yml"],
            "exclude": [],
        })
        self.assertNotIn("selected_files", recipe["artifact"])


if __name__ == "__main__":
    unittest.main()
