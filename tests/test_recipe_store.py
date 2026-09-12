import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from debbuilder.recipe_schema import recipe_document_for_storage
from debbuilder.recipe_store import (
    RecipeStoreError,
    load_recipe,
    load_recipe_result,
    migrate_recipe_directory,
    save_recipe,
)


def canonical_bytes(document: dict) -> bytes:
    canonical = recipe_document_for_storage(document)
    return (json.dumps(canonical, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class RecipeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_load_migrates_legacy_recipe_and_writes_canonical_v3(self):
        path = self.directory / "legacy.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "name": "legacy",
            "build": {"timeout": 75, "output": {"mode": "source"}},
            "steps": [],
        }))

        result = load_recipe_result(path)

        self.assertEqual(result.recipe["schema_version"], 3)
        self.assertEqual(result.recipe["build"]["inactivity_timeout"], 75)
        self.assertEqual(result.applied_migrations, ("v1_to_v2", "v2_to_v3"))
        self.assertTrue(result.rewritten)
        self.assertEqual(path.read_bytes(), canonical_bytes(result.recipe))

    def test_load_without_write_back_migrates_only_in_memory(self):
        path = self.directory / "snapshot.json"
        original = json.dumps({"schema_version": 1, "name": "snapshot", "steps": []}).encode()
        path.write_bytes(original)

        loaded = load_recipe(path, write_back=False)

        self.assertEqual(loaded["schema_version"], 3)
        self.assertEqual(path.read_bytes(), original)

    def test_canonical_current_recipe_is_not_rewritten(self):
        path = self.directory / "current.json"
        path.write_bytes(canonical_bytes({"name": "current"}))
        before = path.stat().st_mtime_ns

        with mock.patch("debbuilder.recipe_store.os.replace") as replace:
            result = load_recipe_result(path)

        self.assertFalse(result.rewritten)
        replace.assert_not_called()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_concurrent_loads_serialize_one_migrating_replacement(self):
        path = self.directory / "concurrent.json"
        path.write_text(json.dumps({"schema_version": 1, "name": "concurrent", "steps": []}))

        with mock.patch("debbuilder.recipe_store.os.replace", wraps=os.replace) as replace:
            with ThreadPoolExecutor(max_workers=8) as executor:
                loaded = list(executor.map(lambda _index: load_recipe(path), range(16)))

        self.assertTrue(all(recipe["schema_version"] == 3 for recipe in loaded))
        self.assertEqual(replace.call_count, 1)

    def test_save_accepts_frontend_v1_and_is_idempotent(self):
        path = self.directory / "frontend.json"
        frontend = {
            "schema_version": 1,
            "name": "frontend",
            "package": {"name": "frontend"},
            "build": {"inactivity_timeout": 300, "output": {"mode": "source"}},
            "service": {"enabled": False},
        }
        stored = save_recipe(path, frontend)
        before = path.stat().st_mtime_ns

        with mock.patch("debbuilder.recipe_store.os.replace") as replace:
            repeated = save_recipe(path, frontend)

        self.assertEqual(stored["schema_version"], 3)
        self.assertEqual(repeated, stored)
        replace.assert_not_called()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_save_rejects_document_identity_that_differs_from_storage_identity(self):
        path = self.directory / "alias.json"

        with self.assertRaises(RecipeStoreError) as raised:
            save_recipe(path, {"name": "debbuilder"})

        self.assertEqual(raised.exception.code, "recipe_identity_mismatch")
        self.assertEqual(raised.exception.path, "$.name")
        self.assertFalse(path.exists())

    def test_durable_write_preserves_existing_permissions_and_syncs_directory(self):
        path = self.directory / "permissions.json"
        path.write_text(json.dumps({"schema_version": 1, "name": "permissions", "steps": []}))
        path.chmod(0o640)
        real_fsync = os.fsync
        descriptors = []

        def recording_fsync(descriptor):
            descriptors.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with mock.patch("debbuilder.recipe_store.os.fsync", side_effect=recording_fsync):
            load_recipe(path)

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertIn(stat.S_IFREG, descriptors)
        self.assertIn(stat.S_IFDIR, descriptors)

    def test_replace_failure_keeps_original_and_cleans_temporary_file(self):
        path = self.directory / "failure.json"
        original = json.dumps({"schema_version": 1, "name": "failure", "steps": []}).encode()
        path.write_bytes(original)

        with mock.patch("debbuilder.recipe_store.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(RecipeStoreError) as raised:
                load_recipe(path)

        self.assertEqual(raised.exception.code, "recipe_write_failed")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".failure.json.*.tmp")), [])

    def test_corrupt_incompatible_and_manual_review_files_remain_unchanged(self):
        cases = {
            "encoding.json": (b"\xff", "invalid_recipe_encoding"),
            "syntax.json": (b'{"name":', "invalid_recipe_json"),
            "duplicate.json": (b'{"name":"one","name":"two"}', "invalid_recipe_json"),
            "scalar.json": (b"[]", "invalid_root"),
            "future.json": (b'{"schema_version":99,"name":"future"}', "future_schema_version"),
            "review.json": (b'{"schema_version":1,"name":"review","steps":[{"name":"build"}]}', "manual_recipe_migration_required"),
            "unknown.json": (b'{"schema_version":2,"name":"unknown","new_meaning":true}', "unknown_field"),
        }
        for filename, (original, code) in cases.items():
            with self.subTest(filename=filename):
                path = self.directory / filename
                path.write_bytes(original)
                with self.assertRaises(RecipeStoreError) as raised:
                    load_recipe(path)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(path.read_bytes(), original)

    def test_directory_scan_is_deterministic_complete_and_idempotent(self):
        (self.directory / "b-legacy-v0.json").write_text(json.dumps({
            "name": "b-legacy-v0", "steps": [],
        }))
        (self.directory / "c-legacy-v1.json").write_text(json.dumps({
            "schema_version": 1, "name": "c-legacy-v1", "steps": [],
        }))
        (self.directory / "a-current.json").write_bytes(canonical_bytes({"name": "a-current"}))
        (self.directory / "f-future.json").write_text(json.dumps({
            "schema_version": 8, "name": "f-future",
        }))
        corrupt = self.directory / "d-corrupt.json"
        corrupt.write_text("{")
        (self.directory / "e-review.json").write_text(json.dumps({
            "schema_version": 1, "name": "e-review", "steps": [{"name": "build"}],
        }))
        ignored = self.directory / "ignored.txt"
        ignored.write_text("not a Recipe")

        first = migrate_recipe_directory(self.directory)
        second = migrate_recipe_directory(self.directory)

        self.assertFalse(first.ok)
        self.assertEqual((first.inspected, first.current, first.migrated, first.failed), (6, 1, 2, 3))
        self.assertEqual([row.file for row in first.files], [
            "a-current.json", "b-legacy-v0.json", "c-legacy-v1.json", "d-corrupt.json",
            "e-review.json", "f-future.json",
        ])
        self.assertEqual(first.files[3].error["code"], "invalid_recipe_json")
        self.assertEqual(first.files[4].error["code"], "manual_recipe_migration_required")
        self.assertEqual(first.files[5].error["code"], "future_schema_version")
        self.assertEqual((second.inspected, second.current, second.migrated, second.failed), (6, 3, 0, 3))
        self.assertEqual(corrupt.read_text(), "{")

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_symbolic_link_recipe_is_refused_without_touching_target(self):
        target = self.directory / "target.data"
        target.write_bytes(canonical_bytes({"name": "target"}))
        link = self.directory / "linked.json"
        link.symlink_to(target)
        original = target.read_bytes()

        with self.assertRaises(RecipeStoreError) as raised:
            load_recipe(link)

        self.assertEqual(raised.exception.code, "unsafe_recipe_path")
        self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
