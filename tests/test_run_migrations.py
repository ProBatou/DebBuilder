import copy
import json
import tempfile
import unittest
from pathlib import Path

from debbuilder.build_store import BuildStore
from debbuilder.run_migrations import RunMigrationError, migrate_run_document


def recipe():
    return {"name": "demo", "package": {"name": "demo"}, "source": {"repository": "owner/demo"}}


class RunMigrationTests(unittest.TestCase):
    def test_v1_migrates_in_memory_to_historical_v2_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            current = store.create(recipe(), run_id="historical-source")
            source = copy.deepcopy(current)
            source["schema_version"] = 1
            source.pop("resource_limits")
            original = copy.deepcopy(source)
            migrated = migrate_run_document(source)
        self.assertEqual(source, original)
        self.assertEqual(migrated.source_version, 1)
        self.assertEqual(migrated.target_version, 2)
        self.assertEqual(migrated.applied, (1,))
        self.assertFalse(migrated.document["resource_limits"]["enforcement_required"])
        self.assertEqual(migrated.document["resource_limits"]["admission_capability"]["backend"], "historical")

    def test_future_and_invalid_versions_are_rejected(self):
        for value, code in ((3, "future_run_schema_version"), (True, "invalid_run_schema_version"), ("1", "invalid_run_schema_version")):
            with self.subTest(value=value), self.assertRaises(RunMigrationError) as raised:
                migrate_run_document({"schema_version": value})
            self.assertEqual(raised.exception.code, code)

    def test_store_read_does_not_rewrite_but_later_mutation_persists_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(recipe(), run_id="historical")
            path = Path(run["workspace"]) / "run.json"
            raw = json.loads(path.read_text())
            raw["schema_version"] = 1
            raw.pop("resource_limits")
            path.write_text(json.dumps(raw))
            before = path.read_bytes()

            loaded = store.load("historical")
            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(path.read_bytes(), before)

            loaded["status"] = "queued"
            store.save(loaded)
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(persisted["resource_limits"]["admission_capability"]["backend"], "historical")


if __name__ == "__main__":
    unittest.main()
