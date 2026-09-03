import json
import unittest
from pathlib import Path

from debbuilder.recipe_schema import validate_recipe_metadata


ROOT = Path(__file__).resolve().parents[1]


class StaticRecipeTests(unittest.TestCase):
    def load(self, name):
        return validate_recipe_metadata(json.loads((ROOT / "data" / "workflows" / f"{name}.json").read_text()))

    def test_bashrc_recipe_installs_release_source_as_root_conffile(self):
        recipe = self.load("bashrc")
        self.assertEqual(recipe["build"]["detected_project"], "static")
        self.assertEqual(recipe["build"]["commands"], [])
        self.assertEqual(recipe["build"]["output"]["mode"], "source")
        self.assertEqual(recipe["install"]["content"]["source"], "configured_files")
        self.assertEqual(recipe["install"]["config_files"], [{"source": ".bashrc", "destination": "/root/.bashrc"}])
        self.assertEqual(recipe["package"]["runtime_dependencies"], [])
        self.assertFalse(recipe["service"]["enabled"])

    def test_ssh_notify_recipe_targets_profile_and_declares_curl(self):
        recipe = self.load("ssh-notify")
        self.assertEqual(recipe["source"]["tracking"], "latest_release")
        self.assertEqual(recipe["package"]["version_revision"], "1")
        self.assertEqual(recipe["package"]["runtime_dependencies"], ["curl"])
        self.assertEqual(recipe["build"]["detected_project"], "static")
        self.assertEqual(recipe["build"]["commands"], [])
        self.assertEqual(
            recipe["install"]["config_files"],
            [
                {"source": "ssh-notify.sh", "destination": "/etc/profile.d/ssh-notify.sh"},
                {"source": "ssh-notify.conf.template", "destination": "/etc/ssh-notify.conf"},
            ],
        )
        self.assertFalse(recipe["service"]["enabled"])

    def test_seerr_recipe_contains_the_audited_runtime_payload_and_sqlite_directory(self):
        recipe = self.load("seerr")
        self.assertEqual(recipe["package"]["version_revision"], "2")
        self.assertEqual(
            recipe["build"]["output"],
            {
                "mode": "paths",
                "path": "",
                "paths": ["package.json", "next.config.ts", "node_modules", ".next", "dist", "public", "seerr-api.yml"],
            },
        )
        self.assertEqual(
            recipe["install"]["maintainer_scripts"]["postinst"].splitlines(),
            [
                "install -d -m 0750 -o seerr -g seerr /var/lib/seerr",
                "install -d -m 0750 -o seerr -g seerr /var/lib/seerr/db",
            ],
        )


if __name__ == "__main__":
    unittest.main()
