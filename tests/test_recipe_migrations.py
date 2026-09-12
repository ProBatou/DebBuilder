import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.recipe_migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    RecipeMigrationError,
    migrate_recipe_document,
    migrate_v0_to_v1,
    migrate_v1_to_v2,
    migrate_v2_to_v3,
)
from debbuilder.recipe_schema import RecipeDocumentError, recipe_document_for_storage, validate_recipe_metadata


FIXTURES = Path(__file__).parent / "fixtures" / "recipe_migrations"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class RecipeMigrationTests(unittest.TestCase):
    def test_v0_migrates_sequentially_to_v3_without_mutating_source(self):
        source = fixture("legacy-v0.json")
        original = copy.deepcopy(source)
        result = migrate_recipe_document(source)
        self.assertEqual(result.source_version, 0)
        self.assertEqual(result.target_version, 3)
        self.assertEqual(result.applied_migrations, ("v0_to_v1", "v1_to_v2", "v2_to_v3"))
        self.assertEqual(result.document["schema_version"], 3)
        self.assertTrue(all(value is None for value in result.document["resource_limits"].values()))
        self.assertNotIn("timeout", result.document["build"])
        self.assertEqual(result.document["build"]["inactivity_timeout"], 120)
        self.assertNotIn("steps", result.document)
        self.assertEqual(source, original)

    def test_v1_migrates_to_v3_with_known_alias_semantics(self):
        result = migrate_recipe_document(fixture("legacy-v1-aliases.json"))
        document = result.document
        self.assertEqual(result.applied_migrations, ("v1_to_v2", "v2_to_v3"))
        self.assertEqual(document["build"]["inactivity_timeout"], 90)
        self.assertNotIn("configured", document["service"])
        self.assertEqual(document["install"]["config_files"], [{
            "source": "etc/legacy-v1.conf",
            "destination": "/etc/legacy-v1.conf",
            "policy": "replace",
        }])
        self.assertEqual(document["artifact"]["archive_source"], "release_asset")
        self.assertEqual(document["artifact"]["asset_selection"], "exact")
        self.assertEqual(document["artifact"]["payload"], {
            "mode": "paths",
            "include": ["bin/legacy-v1"],
            "exclude": [],
            "legacy_file_layout": "basename",
        })

    def test_current_v3_is_unchanged_and_repeated_migration_is_stable(self):
        source = fixture("current-v3.json")
        first = migrate_recipe_document(source)
        second = migrate_recipe_document(first.document)
        self.assertFalse(first.migrated)
        self.assertFalse(second.migrated)
        self.assertEqual(first.document, source)
        self.assertEqual(second.document, first.document)

    def test_v2_to_v3_is_pure_deterministic_and_adds_only_null_policy(self):
        source = fixture("current-v2.json")
        original = copy.deepcopy(source)
        first = migrate_recipe_document(source)
        second = migrate_recipe_document(source)
        self.assertEqual(first, second)
        self.assertEqual(first.applied_migrations, ("v2_to_v3",))
        self.assertEqual(first.document["schema_version"], 3)
        self.assertTrue(all(value is None for value in first.document["resource_limits"].values()))
        self.assertEqual(source, original)

    def test_migration_is_deterministic(self):
        source = fixture("legacy-v1-aliases.json")
        self.assertEqual(migrate_recipe_document(source), migrate_recipe_document(source))

    def test_future_and_invalid_versions_are_structured_refusals(self):
        cases = [
            ({"schema_version": 4}, "future_schema_version", 4),
            ({"schema_version": True}, "invalid_schema_version", None),
            ({"schema_version": "1"}, "invalid_schema_version", None),
            ({"schema_version": 1.0}, "invalid_schema_version", None),
            ({"schema_version": None}, "invalid_schema_version", None),
            ({"schema_version": -1}, "invalid_schema_version", -1),
        ]
        for document, code, source_version in cases:
            with self.subTest(document=document), self.assertRaises(RecipeMigrationError) as raised:
                migrate_recipe_document(document)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.source_version, source_version)
            self.assertEqual(raised.exception.target_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(raised.exception.path, "$.schema_version")

    def test_missing_registry_step_fails_instead_of_skipping_a_version(self):
        migrations = {0: MIGRATIONS[0]}
        with mock.patch("debbuilder.recipe_migrations.MIGRATIONS", migrations):
            with self.assertRaises(RecipeMigrationError) as raised:
                migrate_recipe_document({"name": "gap"})
        self.assertEqual(raised.exception.code, "unsupported_schema_version")
        self.assertEqual(raised.exception.source_version, 1)

    def test_each_step_requires_its_exact_source_version(self):
        for migration, document in (
            (migrate_v0_to_v1, {"schema_version": 1}),
            (migrate_v1_to_v2, {"schema_version": 2}),
            (migrate_v2_to_v3, {"schema_version": 3}),
        ):
            with self.subTest(migration=migration.__name__), self.assertRaises(RecipeMigrationError) as raised:
                migration(document)
            self.assertEqual(raised.exception.code, "migration_source_mismatch")

    def test_engine_rejects_a_step_that_does_not_produce_the_next_version(self):
        def broken_step(document):
            return copy.deepcopy(document)

        with mock.patch("debbuilder.recipe_migrations.MIGRATIONS", {0: broken_step}):
            with self.assertRaises(RecipeMigrationError) as raised:
                migrate_recipe_document({"name": "broken"})
        self.assertEqual(raised.exception.code, "migration_target_mismatch")
        self.assertEqual(raised.exception.source_version, 0)
        self.assertEqual(raised.exception.target_version, 1)

    def test_empty_steps_are_removed_but_non_empty_steps_require_review(self):
        migrated = migrate_recipe_document({"schema_version": 1, "name": "empty", "steps": []})
        self.assertNotIn("steps", migrated.document)
        for steps in ([{"name": "build"}], {}, None):
            with self.subTest(steps=steps), self.assertRaises(RecipeMigrationError) as raised:
                migrate_recipe_document({"schema_version": 1, "name": "review", "steps": steps})
            self.assertEqual(raised.exception.code, "manual_recipe_migration_required")
            self.assertEqual(raised.exception.path, "$.steps")

    def test_ambiguous_artifact_representation_is_rejected(self):
        document = {
            "schema_version": 1,
            "name": "ambiguous",
            "artifact": {
                "mode": "upstream_archive",
                "selected_files": ["server.py"],
                "payload": {"mode": "paths", "include": ["server.py"], "exclude": []},
            },
        }
        with self.assertRaises(RecipeMigrationError) as raised:
            migrate_recipe_document(document)
        self.assertEqual(raised.exception.code, "ambiguous_recipe_fields")
        self.assertEqual(raised.exception.path, "$.artifact")

    def test_migration_preserves_unknown_fields_and_strict_storage_refuses_them(self):
        document = {"schema_version": 1, "name": "unknown", "future_meaning": {"keep": True}}
        migrated = migrate_recipe_document(document).document
        self.assertEqual(migrated["future_meaning"], {"keep": True})
        with self.assertRaises(RecipeDocumentError) as raised:
            recipe_document_for_storage(document)
        self.assertEqual(raised.exception.code, "unknown_field")
        self.assertEqual(raised.exception.path, "$.future_meaning")

    def test_frontend_style_v1_is_accepted_and_stored_as_v3(self):
        frontend = {
            "schema_version": 1,
            "name": "frontend",
            "active": True,
            "package": {"name": "frontend"},
            "source": {"provider": "github", "repository": "example/frontend"},
            "build": {"inactivity_timeout": 300, "maximum_runtime": None, "output": {"mode": "source"}},
            "install": {"content": {"source": "build_output"}},
            "service": {"enabled": False},
        }
        stored = recipe_document_for_storage(frontend)
        self.assertEqual(stored["schema_version"], 3)
        self.assertEqual(validate_recipe_metadata(frontend)["schema_version"], 3)

    def test_strict_current_schema_validation_rejects_unknown_fields(self):
        current = fixture("current-v3.json")
        current["unknown"] = "meaningful"
        with self.assertRaises(RecipeDocumentError) as raised:
            recipe_document_for_storage(current)
        self.assertEqual(raised.exception.code, "unknown_field")

    def test_current_v3_does_not_reapply_legacy_alias_migrations(self):
        current = fixture("current-v3.json")
        current["build"]["timeout"] = 90
        migrated = migrate_recipe_document(current)
        self.assertFalse(migrated.migrated)
        self.assertEqual(migrated.document["build"]["timeout"], 90)
        with self.assertRaises(RecipeDocumentError) as raised:
            recipe_document_for_storage(current)
        self.assertEqual(raised.exception.code, "unknown_field")
        self.assertEqual(raised.exception.path, "$.build.timeout")

    def test_historical_snapshot_can_migrate_in_memory_without_changing_bytes(self):
        path = FIXTURES / "legacy-v1-aliases.json"
        before = path.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        validate_recipe_metadata(json.loads(before))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
