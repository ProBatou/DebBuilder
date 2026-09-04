import json
import tempfile
import unittest
from pathlib import Path

from debbuilder import debian_packaging
from debbuilder.recipe_schema import recipe_for_storage, validate_recipe_metadata


ROOT = Path(__file__).resolve().parents[1]


class StaticRecipeTests(unittest.TestCase):
    def load(self, name):
        path = ROOT / "tests" / "fixtures" / "recipes" / f"{name}.json"
        return validate_recipe_metadata(json.loads(path.read_text()))

    def uses_compact_static_build(self, name):
        recipe = self.load(name)
        return recipe["build"]["detected_project"] == "static" and not recipe["build"]["commands"] and recipe["install"]["content"]["source"] == "configured_files"

    def test_compact_static_build_applies_only_to_static_custom_mapping_recipes(self):
        self.assertTrue(self.uses_compact_static_build("ssh-notify"))
        self.assertTrue(self.uses_compact_static_build("bashrc"))
        self.assertFalse(self.uses_compact_static_build("seerr"))
        self.assertFalse(self.uses_compact_static_build("debbuilder"))

    def test_bashrc_recipe_installs_release_source_as_root_conffile(self):
        recipe = self.load("bashrc")
        self.assertEqual(recipe["build"]["detected_project"], "static")
        self.assertEqual(recipe["build"]["commands"], [])
        self.assertEqual(recipe["build"]["output"]["mode"], "source")
        self.assertEqual(recipe["install"]["content"]["source"], "configured_files")
        self.assertEqual(recipe["install"]["config_files"], [{"source": ".bashrc", "destination": "/root/.bashrc", "policy": "replace"}])
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
                {"source": "ssh-notify.sh", "destination": "/etc/profile.d/ssh-notify.sh", "policy": "replace"},
                {"source": "ssh-notify.conf.template", "destination": "/etc/ssh-notify.conf", "policy": "dpkg_conffile"},
            ],
        )
        self.assertFalse(recipe["service"]["enabled"])

    def test_ssh_notify_mixed_mapping_policies_stage_without_a_build(self):
        recipe = self.load("ssh-notify")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for name in ("source", "staging", "artifacts", "logs"):
                (workspace / name).mkdir()
            (workspace / "source/ssh-notify.sh").write_text("#!/bin/sh\n")
            (workspace / "source/ssh-notify.conf.template").write_text("URL=\n")
            result = debian_packaging.prepare_staging(
                recipe, {"output": {"path": str(workspace / "source")}, "version": "1.0-1"}, workspace, preview=True,
            )
            self.assertEqual(result["conffiles"], ["/etc/ssh-notify.conf"])
            self.assertEqual([row["policy"] for row in result["configurations"]], ["replace", "dpkg_conffile"])
            self.assertFalse(result["include_output"])
            self.assertTrue(result["preview"])

    def test_shipped_service_recipes_survive_canonical_storage_round_trip(self):
        for name in ("seerr", "debbuilder"):
            original = self.load(name)
            reloaded = validate_recipe_metadata(recipe_for_storage(original))
            self.assertEqual(reloaded["service"], original["service"])
            self.assertTrue(reloaded["service"]["configured"])
        self.assertEqual(self.load("debbuilder")["install"]["config_files"][0]["policy"], "create_if_missing")

    def test_debbuilder_recipe_preserves_apache_traversal_to_public_repository(self):
        recipe = self.load("debbuilder")
        preinst = recipe["install"]["maintainer_scripts"]["preinst"].splitlines()
        self.assertIn("install -d -m 0751 -o root -g root /var/lib/debbuilder", preinst)
        self.assertIn("install -d -m 0751 -o root -g root /var/lib/debbuilder/repository", preinst)
        self.assertFalse(any("0750" in line and line.endswith("/var/lib/debbuilder/repository") for line in preinst))

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
