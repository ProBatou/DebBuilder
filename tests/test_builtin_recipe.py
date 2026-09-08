import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import builtin_recipe, debian_packaging, recipe_store
from debbuilder.recipe_schema import recipe_document_for_storage, validate_recipe_metadata

HISTORICAL_DEBBUILDER_FIXTURE = Path(__file__).parent / "fixtures" / "recipes" / "debbuilder.json"


class BuiltinRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workflows = Path(self.temporary.name) / "workflows"
        self.workflows.mkdir()
        self.path = self.workflows / "debbuilder.json"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, document: dict) -> bytes:
        original = (json.dumps(document, indent=2) + "\n").encode()
        self.path.write_bytes(original)
        return original

    def definition(self) -> dict:
        return builtin_recipe.load_builtin_definition()

    def test_canonical_definition_is_current_strict_and_packaged(self):
        path = builtin_recipe.BUILTIN_RECIPE_PATH
        raw = json.loads(path.read_text())
        canonical = self.definition()

        self.assertTrue(path.is_file())
        self.assertEqual(recipe_document_for_storage(raw), raw)
        self.assertEqual(canonical["schema_version"], 2)
        self.assertEqual(canonical["name"], "debbuilder")
        self.assertEqual(canonical["management"], {
            "owner": "application", "builtin_id": "debbuilder",
            "definition_version": 2, "operator_overrides": {},
        })
        self.assertEqual(builtin_recipe.OPERATOR_OVERRIDE_PATHS, (
            "active",
            "package.maintainer",
            "build.environment",
            "build.inactivity_timeout",
            "build.maximum_runtime",
        ))
        self.assertEqual(canonical["package"]["runtime_dependencies"], ["python3", "python3-dbus"])
        self.assertEqual(builtin_recipe.ui_projection(canonical), {
            "source": "builtin",
            "managed": True,
            "editable_paths": list(builtin_recipe.OPERATOR_OVERRIDE_PATHS),
        })
        self.assertEqual(builtin_recipe.ui_projection({"name": "operator-recipe"}), {})

    def test_initial_seed_and_repeated_reconciliation_are_idempotent(self):
        seeded = builtin_recipe.reconcile_builtin_recipe(self.workflows)
        before = self.path.stat().st_mtime_ns

        with mock.patch("debbuilder.recipe_store.os.replace", wraps=os.replace) as replace:
            repeated = builtin_recipe.reconcile_builtin_recipe(self.workflows)

        self.assertEqual(seeded.action, "seeded")
        self.assertEqual(repeated.action, "current")
        self.assertEqual(replace.call_count, 0)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_historical_recipe_adoption_preserves_only_allowlisted_values(self):
        historical = json.loads(HISTORICAL_DEBBUILDER_FIXTURE.read_text())
        historical["active"] = False
        historical["package"].update({"name": "local-debbuilder", "maintainer": "Ops <ops@example.test>"})
        historical["source"]["repository"] = "local/fork"
        historical["build"].update({
            "timeout": 45, "maximum_runtime": 900, "environment": {"LOCAL_POLICY": "1"},
        })
        historical["install"]["destination"] = "/opt/local-debbuilder"
        historical["steps"] = []
        self.write(historical)

        result = builtin_recipe.reconcile_builtin_recipe(self.workflows)
        stored = recipe_store.load_recipe(self.path, write_back=False)

        self.assertEqual(result.action, "adopted")
        self.assertFalse(stored["active"])
        self.assertEqual(stored["package"]["maintainer"], "Ops <ops@example.test>")
        self.assertEqual(stored["build"]["environment"], {"LOCAL_POLICY": "1"})
        self.assertEqual(stored["build"]["inactivity_timeout"], 45)
        self.assertEqual(stored["build"]["maximum_runtime"], 900)
        self.assertEqual(stored["package"]["name"], "debbuilder")
        self.assertEqual(stored["source"]["repository"], "ProBatou/DebBuilder")
        self.assertEqual(stored["install"]["destination"], "/opt/debbuilder")
        self.assertEqual(stored["management"]["operator_overrides"], {
            "active": False,
            "package": {"maintainer": "Ops <ops@example.test>"},
            "build": {
                "environment": {"LOCAL_POLICY": "1"},
                "inactivity_timeout": 45,
                "maximum_runtime": 900,
            },
        })

    def test_unsafe_adoption_failures_leave_original_bytes_untouched(self):
        cases = {
            "unknown": {"schema_version": 2, "name": "debbuilder", "meaningful": True},
            "future": {"schema_version": 99, "name": "debbuilder"},
            "review": {"schema_version": 1, "name": "debbuilder", "steps": [{"name": "build"}]},
            "mismatch": {"schema_version": 2, "name": "another"},
        }
        for label, document in cases.items():
            with self.subTest(label=label):
                original = self.write(document)
                with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
                    builtin_recipe.reconcile_builtin_recipe(self.workflows)
                self.assertEqual(raised.exception.code, "builtin_recipe_adoption_failed")
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()

        original = b'{"schema_version":'
        self.path.write_bytes(original)
        with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
            builtin_recipe.reconcile_builtin_recipe(self.workflows)
        self.assertEqual(raised.exception.code, "builtin_recipe_adoption_failed")
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_and_obsolete_stored_overrides_are_explicit(self):
        for override, code in (
            ({"build": {"inactivity_timeout": "slow"}}, "builtin_recipe_override_invalid"),
            ({"package": {"maintainer": ""}}, "builtin_recipe_override_invalid"),
            ({"source": {"repository": "local/fork"}}, "builtin_recipe_upgrade_required"),
        ):
            with self.subTest(override=override):
                managed = self.definition()
                managed["management"]["operator_overrides"] = override
                original = self.write(managed)
                with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
                    builtin_recipe.reconcile_builtin_recipe(self.workflows)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()

        with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
            builtin_recipe.effective_builtin_recipe({"build": "invalid"})
        self.assertEqual(raised.exception.code, "builtin_recipe_override_invalid")

    def test_definition_upgrade_replaces_managed_fields_and_keeps_override(self):
        first = builtin_recipe.reconcile_builtin_recipe(self.workflows)
        edited = copy.deepcopy(first.recipe)
        edited["active"] = False
        builtin_recipe.update_builtin_recipe(self.path, edited)

        upgraded_definition = self.definition()
        upgraded_definition["management"]["definition_version"] = 3
        upgraded_definition["package"]["description"] = "DebBuilder managed definition v3"
        upgraded_definition["package"]["runtime_dependencies"].append("curl")
        definition_path = Path(self.temporary.name) / "definition-v3.json"
        definition_path.write_text(json.dumps(upgraded_definition))

        result = builtin_recipe.reconcile_builtin_recipe(self.workflows, definition_path=definition_path)

        self.assertEqual(result.action, "upgraded")
        self.assertEqual(result.previous_definition_version, 2)
        self.assertEqual(result.definition_version, 3)
        self.assertFalse(result.recipe["active"])
        self.assertEqual(result.recipe["package"]["description"], "DebBuilder managed definition v3")
        self.assertIn("curl", result.recipe["package"]["runtime_dependencies"])
        self.assertEqual(result.recipe["management"]["operator_overrides"], {"active": False})

    def test_cp3_definition_upgrade_installs_packaged_shutdown_policy(self):
        previous = self.definition()
        previous["management"]["definition_version"] = 1
        previous["service"].update({
            "restart_sec": "",
            "timeout_stop_sec": "",
            "kill_signal": "",
            "kill_mode": "",
        })
        self.write(previous)

        result = builtin_recipe.reconcile_builtin_recipe(self.workflows)

        self.assertEqual(result.action, "upgraded")
        self.assertEqual(result.previous_definition_version, 1)
        self.assertEqual(result.definition_version, 2)
        self.assertEqual(result.recipe["service"]["restart_sec"], "3s")
        self.assertEqual(result.recipe["service"]["timeout_stop_sec"], "20s")
        self.assertEqual(result.recipe["service"]["kill_signal"], "SIGTERM")
        self.assertEqual(result.recipe["service"]["kill_mode"], "control-group")

    def test_newer_persisted_definition_requires_review_without_rewrite(self):
        managed = self.definition()
        managed["management"]["definition_version"] = 7
        original = self.write(managed)

        with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
            builtin_recipe.reconcile_builtin_recipe(self.workflows)

        self.assertEqual(raised.exception.code, "builtin_recipe_upgrade_required")
        self.assertEqual(self.path.read_bytes(), original)

    def test_allowlisted_edit_persists_override_and_managed_edit_is_atomic_refusal(self):
        current = builtin_recipe.reconcile_builtin_recipe(self.workflows).recipe
        allowed = copy.deepcopy(current)
        allowed["package"]["maintainer"] = "Operator <operator@example.test>"
        allowed["build"]["maximum_runtime"] = 1200
        stored = builtin_recipe.update_builtin_recipe(self.path, allowed)
        self.assertEqual(stored["management"]["operator_overrides"], {
            "package": {"maintainer": "Operator <operator@example.test>"},
            "build": {"maximum_runtime": 1200},
        })

        original = self.path.read_bytes()
        forbidden = copy.deepcopy(stored)
        forbidden["service"]["command"] = "/bin/false"
        with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
            builtin_recipe.update_builtin_recipe(self.path, forbidden)
        self.assertEqual(raised.exception.code, "builtin_recipe_managed_field")
        self.assertEqual(raised.exception.path, "$.service.command")
        self.assertEqual(self.path.read_bytes(), original)

    def test_write_failure_before_replace_keeps_historical_recipe(self):
        original = self.write({"schema_version": 1, "name": "debbuilder", "active": False, "steps": []})

        with mock.patch("debbuilder.recipe_store.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(recipe_store.RecipeStoreError) as raised:
                builtin_recipe.reconcile_builtin_recipe(self.workflows)

        self.assertEqual(raised.exception.code, "recipe_write_failed")
        self.assertEqual(self.path.read_bytes(), original)

    def test_reserved_identity_and_normal_user_recipes_are_independent(self):
        with self.assertRaises(builtin_recipe.BuiltinRecipeError) as raised:
            builtin_recipe.require_user_recipe_id("debbuilder")
        self.assertEqual(raised.exception.code, "builtin_recipe_reserved")
        builtin_recipe.require_user_recipe_id("operator-recipe")

        normal = recipe_store.save_recipe(self.workflows / "operator-recipe.json", {"name": "operator-recipe"})
        self.assertNotIn("management", normal)
        self.assertEqual(validate_recipe_metadata(normal)["name"], "operator-recipe")

    def test_self_build_definition_keeps_required_source_install_and_service_contract(self):
        recipe = validate_recipe_metadata(self.definition())
        self.assertEqual(recipe["source"]["repository"], "ProBatou/DebBuilder")
        self.assertEqual(recipe["package"]["runtime_dependencies"], ["python3", "python3-dbus"])
        self.assertEqual(recipe["build"]["detected_project"], "python")
        self.assertEqual(recipe["build"]["commands"], [])
        self.assertEqual(recipe["build"]["output"], {
            "mode": "paths", "path": "", "paths": ["debbuilder", "server.py", "static"],
        })
        self.assertEqual(recipe["artifact"]["mode"], "source_build")
        self.assertEqual(recipe["install"]["destination"], "/opt/debbuilder")
        self.assertEqual(recipe["install"]["config_files"][0]["policy"], "create_if_missing")
        self.assertEqual(recipe["service"]["command"], "/usr/bin/python3 /opt/debbuilder/server.py")
        self.assertTrue(recipe["service"]["enabled"])
        self.assertEqual(recipe["service"]["timeout_stop_sec"], "20s")
        self.assertEqual(recipe["service"]["kill_mode"], "control-group")
        self.assertEqual(recipe["service"]["kill_signal"], "SIGTERM")
        self.assertEqual(recipe["service"]["restart"], "on-failure")
        self.assertEqual(recipe["service"]["restart_sec"], "3s")

    def test_self_build_definition_prepares_complete_package_layout(self):
        recipe = validate_recipe_metadata(self.definition())
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            for name in ("source/debbuilder", "source/static", "source/packaging", "staging", "artifacts", "logs"):
                (workspace / name).mkdir(parents=True)
            (workspace / "source/debbuilder/__init__.py").write_text('"""package"""\n')
            (workspace / "source/server.py").write_text("#!/usr/bin/python3\n")
            (workspace / "source/static/index.html").write_text("DebBuilder\n")
            (workspace / "source/packaging/debbuilder.env").write_text("DEBBUILDER_PORT=8099\n")
            output = {
                "mode": "paths",
                "paths": [
                    {"path": str(workspace / "source/debbuilder")},
                    {"path": str(workspace / "source/server.py")},
                    {"path": str(workspace / "source/static")},
                ],
            }

            result = debian_packaging.prepare_staging(
                recipe, {"output": output, "version": "1.0-2"}, workspace,
            )

            staging = workspace / "staging"
            self.assertTrue((staging / "opt/debbuilder/debbuilder/__init__.py").is_file())
            self.assertTrue((staging / "opt/debbuilder/server.py").is_file())
            self.assertTrue((staging / "opt/debbuilder/static/index.html").is_file())
            self.assertTrue((staging / "usr/share/debbuilder/config-templates/etc/debbuilder/debbuilder.env").is_file())
            self.assertIn("Depends: python3, python3-dbus", result["control"])
            self.assertIn("ExecStart=/usr/bin/python3 /opt/debbuilder/server.py", result["systemd"]["content"])
            self.assertIn("TimeoutStopSec=20s", result["systemd"]["content"])
            self.assertIn("KillMode=control-group", result["systemd"]["content"])
            self.assertIn("KillSignal=SIGTERM", result["systemd"]["content"])
            self.assertIn("Restart=on-failure", result["systemd"]["content"])
            self.assertIn("RestartSec=3s", result["systemd"]["content"])


if __name__ == "__main__":
    unittest.main()
