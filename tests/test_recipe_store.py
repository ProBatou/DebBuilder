import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.recipe_schema import recipe_document_for_storage
from debbuilder.recipe_store import (
    RecipeStoreError,
    load_recipe,
    locked_recipe,
    save_recipe,
    validate_recipe_directory,
)


def current_recipe(name: str, **values) -> dict:
    return {"schema_version": 5, "name": name, **values}


def _hold_recipe_lease(path: str, ready, release) -> None:
    with locked_recipe(Path(path)):
        ready.set()
        release.wait(5)


def _save_recipe_in_process(path: str, finished) -> None:
    save_recipe(Path(path), current_recipe(Path(path).stem, active=False))
    finished.set()


def canonical_bytes(document: dict) -> bytes:
    canonical = recipe_document_for_storage(document)
    return (json.dumps(canonical, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class RecipeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_v5_save_load_edit_round_trip_is_canonical(self):
        path = self.directory / "demo.json"
        first = save_recipe(path, current_recipe("demo", active=True))
        loaded = load_recipe(path)
        edited = {**loaded, "active": False}
        second = save_recipe(path, edited)

        self.assertEqual(first["schema_version"], 5)
        self.assertTrue(first["active"])
        self.assertFalse(second["active"])
        self.assertEqual(load_recipe(path), second)
        self.assertEqual(path.read_bytes(), canonical_bytes(second))

    def test_load_is_read_only_even_when_v5_json_is_not_canonical_bytes(self):
        path = self.directory / "snapshot.json"
        original = b'{"schema_version":5,"name":"snapshot"}'
        path.write_bytes(original)

        loaded = load_recipe(path)

        self.assertEqual(loaded["schema_version"], 5)
        self.assertEqual(path.read_bytes(), original)

    def test_old_and_unversioned_schemas_are_explicitly_rejected_without_rewrite(self):
        for version in (None, 0, 1, 2, 3, 4):
            with self.subTest(version=version):
                path = self.directory / f"old-{version}.json"
                document = {"name": path.stem}
                if version is not None:
                    document["schema_version"] = version
                original = json.dumps(document).encode()
                path.write_bytes(original)
                with self.assertRaises(RecipeStoreError) as raised:
                    load_recipe(path)
                self.assertEqual(raised.exception.code, "unsupported_recipe_schema")
                self.assertEqual(raised.exception.path, "$.schema_version")
                self.assertEqual(path.read_bytes(), original)

    def test_build_version_source_is_rejected_on_save_and_load_without_rewrite(self):
        document = current_recipe("unsupported", source={"version": {"source": "build"}})
        path = self.directory / "unsupported.json"
        with self.assertRaises(RecipeStoreError) as saved:
            save_recipe(path, document)
        self.assertEqual(saved.exception.code, "unsupported_version_source")
        self.assertEqual(saved.exception.path, "$.source.version.source")
        self.assertFalse(path.exists())

        original = json.dumps(document).encode()
        path.write_bytes(original)
        with self.assertRaises(RecipeStoreError) as loaded:
            load_recipe(path)
        self.assertEqual(loaded.exception.code, "unsupported_version_source")
        self.assertEqual(loaded.exception.path, "$.source.version.source")
        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "fork multiprocessing is unavailable")
    def test_recipe_lease_blocks_writers_in_another_process(self):
        path = self.directory / "leased.json"
        save_recipe(path, current_recipe("leased"))
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        finished = context.Event()
        holder = context.Process(target=_hold_recipe_lease, args=(str(path), ready, release))
        writer = context.Process(target=_save_recipe_in_process, args=(str(path), finished))
        holder.start()
        self.assertTrue(ready.wait(2))
        writer.start()
        try:
            self.assertFalse(finished.wait(0.25))
        finally:
            release.set()
            holder.join(5)
            writer.join(5)
        self.assertEqual(holder.exitcode, 0)
        self.assertEqual(writer.exitcode, 0)
        self.assertTrue(finished.is_set())
        self.assertFalse(load_recipe(path)["active"])

    def test_repeated_identical_save_does_not_replace_file(self):
        path = self.directory / "current.json"
        document = current_recipe("current")
        stored = save_recipe(path, document)
        before = path.stat().st_mtime_ns

        with mock.patch("debbuilder.recipe_store.os.replace") as replace:
            repeated = save_recipe(path, stored)

        self.assertEqual(repeated, stored)
        replace.assert_not_called()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_save_rejects_document_identity_that_differs_from_storage_identity(self):
        path = self.directory / "alias.json"
        with self.assertRaises(RecipeStoreError) as raised:
            save_recipe(path, current_recipe("debbuilder"))
        self.assertEqual(raised.exception.code, "recipe_identity_mismatch")
        self.assertEqual(raised.exception.path, "$.name")
        self.assertFalse(path.exists())

    def test_durable_save_preserves_permissions_and_syncs_directory(self):
        path = self.directory / "permissions.json"
        save_recipe(path, current_recipe("permissions"))
        path.chmod(0o640)
        real_fsync = os.fsync
        descriptors = []

        def recording_fsync(descriptor):
            descriptors.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with mock.patch("debbuilder.recipe_store.os.fsync", side_effect=recording_fsync):
            save_recipe(path, current_recipe("permissions", active=False))

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertIn(stat.S_IFREG, descriptors)
        self.assertIn(stat.S_IFDIR, descriptors)

    def test_replace_failure_keeps_original_and_cleans_temporary_file(self):
        path = self.directory / "failure.json"
        save_recipe(path, current_recipe("failure"))
        original = path.read_bytes()

        with mock.patch("debbuilder.recipe_store.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(RecipeStoreError) as raised:
                save_recipe(path, current_recipe("failure", active=False))

        self.assertEqual(raised.exception.code, "recipe_write_failed")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".failure.json.*.tmp")), [])

    def test_corrupt_or_non_v5_files_remain_unchanged(self):
        cases = {
            "encoding.json": (b"\xff", "invalid_recipe_encoding"),
            "syntax.json": (b'{"name":', "invalid_recipe_json"),
            "duplicate.json": (b'{"schema_version":5,"name":"one","name":"two"}', "invalid_recipe_json"),
            "scalar.json": (b"[]", "invalid_root"),
            "future.json": (b'{"schema_version":99,"name":"future"}', "unsupported_recipe_schema"),
            "legacy.json": (b'{"schema_version":1,"name":"legacy","steps":[]}', "unsupported_recipe_schema"),
        }
        for filename, (original, code) in cases.items():
            with self.subTest(filename=filename):
                path = self.directory / filename
                path.write_bytes(original)
                with self.assertRaises(RecipeStoreError) as raised:
                    load_recipe(path)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(path.read_bytes(), original)

    def test_directory_validation_is_deterministic_complete_and_read_only(self):
        valid = self.directory / "a-current.json"
        valid.write_bytes(canonical_bytes(current_recipe("a-current")))
        old = self.directory / "b-old.json"
        old.write_text('{"schema_version":4,"name":"b-old"}')
        corrupt = self.directory / "c-corrupt.json"
        corrupt.write_text("{")
        (self.directory / "ignored.txt").write_text("not a Recipe")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}

        report = validate_recipe_directory(self.directory)

        self.assertFalse(report.ok)
        self.assertEqual((report.inspected, report.valid, report.failed), (3, 1, 2))
        self.assertEqual([row.file for row in report.files], ["a-current.json", "b-old.json", "c-corrupt.json"])
        self.assertEqual(report.files[1].error["code"], "unsupported_recipe_schema")
        self.assertEqual(report.files[2].error["code"], "invalid_recipe_json")
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_symbolic_link_recipe_is_refused_without_touching_target(self):
        target = self.directory / "target.data"
        target.write_bytes(canonical_bytes(current_recipe("target")))
        link = self.directory / "linked.json"
        link.symlink_to(target)
        original = target.read_bytes()
        with self.assertRaises(RecipeStoreError) as raised:
            load_recipe(link)
        self.assertEqual(raised.exception.code, "unsafe_recipe_path")
        self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
