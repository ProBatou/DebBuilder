import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import debbuilder.app as server


class RecipePreviewModelTests(unittest.TestCase):
    def test_empty_visual_recipe_has_no_executable_steps(self):
        workflow = {
            "name": "visual-recipe",
            "package_name": "visual-recipe",
            "github_repository": "example/visual-recipe",
            "version_tracking": "latest_release",
            "version_source": "tag",
            "active": True,
            "steps": [],
        }
        self.assertEqual(server.normalize_steps(workflow), [])
        script = server.generate_script(workflow)
        self.assertIn("structured Build Runs", script)
        self.assertNotIn("not wired to packaging", script)

    def test_stored_unknown_steps_are_not_promoted_to_supported_build_steps(self):
        workflow = {
            "name": "stored-data",
            "package_name": "stored-data",
            "github_repository": "example/stored-data",
            "steps": [{"type": "anything", "payload": {"kept": True}}],
        }
        self.assertEqual(server.SUPPORTED_STEP_TYPES, set())
        self.assertEqual(server.normalize_steps(workflow)[0]["payload"], {"kept": True})
        self.assertIn("Ignored stored step payload", server.generate_script(workflow))


if __name__ == "__main__":
    unittest.main()
