import json
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import recipe_store
from debbuilder.automation_identity import UpstreamIdentityError
from debbuilder.automation_ledger import AutomationLedger, AutomationLedgerError
from debbuilder.build_store import BuildStore, canonical_recipe_sha256


def identity(asset_id="5678"):
    return {
        "schema_version": 1,
        "provider": "github",
        "repository": "example/project",
        "tracking": "latest_release",
        "source_type": "release_asset",
        "payload_kind": "deb",
        "release_id": "1234",
        "asset_id": asset_id,
        "resolved_ref": "v1.2.3",
        "resolved_version": "1.2.3",
        "content_sha256": "12" * 32,
        "asset_name": "project_1.2.3_amd64.deb",
        "expected_size": 42,
        "expected_package": "project",
        "expected_architecture": "amd64",
    }


def _process_claim(data_dir, recipe_path, recipe_sha, start, results):
    start.wait()
    result = AutomationLedger(data_dir).claim_current_recipe(recipe_path, "recipe", identity(), recipe_sha, "build")
    results.put((result.created, result.attempt_key, result.generation))


def automated_recipe(policy="build"):
    return {
        "schema_version": 5,
        "name": "recipe",
        "active": True,
        "automation": {"enabled": True, "policy": policy},
        "package": {"name": "recipe"},
        "source": {"repository": "example/project"},
    }


class AutomationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.recipe_path = self.root / "recipes" / "recipe.json"
        self.recipe_sha = canonical_recipe_sha256(recipe_store.save_recipe(self.recipe_path, automated_recipe()))
        self.ledger = AutomationLedger(self.data)

    def tearDown(self):
        self.temporary.cleanup()

    def claim(self, **kwargs):
        return self.ledger.claim_current_recipe(
            self.recipe_path, "recipe", kwargs.pop("identity", identity()),
            kwargs.pop("recipe_sha", self.recipe_sha), kwargs.pop("policy", "build"), **kwargs,
        )

    def test_missing_ledger_is_empty_and_read_only(self):
        self.assertEqual(self.ledger.read(), {"schema_version": 1, "attempts": {}})
        self.assertFalse(self.data.exists())

    def test_atomic_create_duplicate_reuse_and_restart(self):
        first = self.claim()
        before = self.ledger.path.read_bytes()
        second = AutomationLedger(self.data).claim_current_recipe(self.recipe_path, "recipe", identity(), self.recipe_sha, "build")
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.attempt_key, second.attempt_key)
        self.assertEqual(first.record, second.record)
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_failure_before_claim_replace_leaves_no_partial_claim(self):
        with mock.patch(
            "debbuilder.automation_ledger.storage.atomic_write_text",
            side_effect=OSError("simulated pre-replace crash"),
        ):
            with self.assertRaises(OSError):
                self.claim()
        self.assertFalse(self.ledger.path.exists())
        recovered = AutomationLedger(self.data).claim_current_recipe(self.recipe_path, "recipe", identity(), self.recipe_sha, "build")
        self.assertTrue(recovered.created)

    def test_same_attempt_cannot_change_captured_policy(self):
        self.claim()
        with self.assertRaises(AutomationLedgerError) as raised:
            self.claim(policy="full")
        self.assertEqual(raised.exception.code, "recipe_changed_during_detection")

    def test_concurrent_threads_converge(self):
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(self.claim())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(len({result.attempt_key for result in results}), 1)
        self.assertEqual(len(self.ledger.read()["attempts"]), 1)

    def test_concurrent_processes_converge_under_file_lock(self):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [context.Process(target=_process_claim, args=(self.data, self.recipe_path, self.recipe_sha, start, results)) for _ in range(4)]
        for process in processes:
            process.start()
        start.set()
        rows = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(created for created, _key, _generation in rows), 1)
        self.assertEqual(len({key for _created, key, _generation in rows}), 1)

    def test_distinct_recipe_hash_and_identity_make_distinct_attempts(self):
        first = self.claim()
        changed = automated_recipe()
        changed["package"]["description"] = "Changed Recipe"
        revised_sha = canonical_recipe_sha256(recipe_store.save_recipe(self.recipe_path, changed))
        second = self.claim(recipe_sha=revised_sha)
        third = self.claim(identity=identity("9999"), recipe_sha=revised_sha)
        self.assertEqual(len({first.attempt_key, second.attempt_key, third.attempt_key}), 3)

    def test_stale_recipe_hash_cannot_claim(self):
        with self.assertRaises(AutomationLedgerError) as raised:
            self.claim(recipe_sha="cd" * 32)
        self.assertEqual(raised.exception.code, "recipe_changed_during_detection")
        self.assertEqual(self.ledger.read()["attempts"], {})

    def test_incomplete_identity_cannot_be_claimed(self):
        with self.assertRaises(UpstreamIdentityError) as raised:
            self.claim(identity=identity(asset_id=""))
        self.assertEqual(raised.exception.code, "incomplete_upstream_identity")
        self.assertFalse(self.data.exists())

    def test_run_preallocation_linking_and_restart_reconciliation(self):
        configured = automated_recipe()
        recipe_sha = canonical_recipe_sha256(configured)
        claim = self.claim(recipe_sha=recipe_sha)
        preallocated = self.ledger.preallocate_run(claim.attempt_key, 0)
        duplicate = self.ledger.preallocate_run(claim.attempt_key, 0)
        self.assertEqual(preallocated["preallocated_run_id"], duplicate["preallocated_run_id"])
        self.assertTrue(AutomationLedger(self.data).may_create_run(claim.attempt_key, claim.generation))
        with self.assertRaises(AutomationLedgerError) as missing:
            self.ledger.link_run(claim.attempt_key, 0, preallocated["preallocated_run_id"])
        self.assertEqual(missing.exception.code, "automation_run_not_found")
        store = BuildStore(self.data / "builds")
        store.create(
            configured, recipe_id="recipe", run_id=preallocated["preallocated_run_id"], mode="build",
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation={
                "attempt_key": claim.attempt_key, "generation": 0, "policy": "build",
                "expected_upstream_identity": identity(),
            },
        )
        self.assertFalse(AutomationLedger(self.data).may_create_run(claim.attempt_key, claim.generation))
        reconciled = AutomationLedger(self.data).reconcile_preallocated_run(claim.attempt_key, 0)
        self.assertTrue(reconciled["linked"])
        self.assertFalse(reconciled["recovered_incomplete"])
        linked = reconciled["record"]
        self.assertEqual(linked["state"], "admitted")
        self.assertFalse(self.ledger.may_create_run(claim.attempt_key, claim.generation))
        self.assertEqual(self.ledger.link_run(claim.attempt_key, 0, preallocated["preallocated_run_id"]), linked)
        with self.assertRaises(AutomationLedgerError):
            self.ledger.link_run(claim.attempt_key, 0, "different")

    def test_restart_reconciliation_discards_only_an_uncommitted_preallocation(self):
        claim = self.claim()
        row = self.ledger.preallocate_run(claim.attempt_key, 0)
        folder = BuildStore(self.data / "builds").run_dir(row["preallocated_run_id"])
        folder.mkdir(parents=True)
        (folder / "partial").write_text("incomplete")
        reconciled = AutomationLedger(self.data).reconcile_preallocated_run(claim.attempt_key, 0)
        self.assertFalse(reconciled["linked"])
        self.assertTrue(reconciled["recovered_incomplete"])
        self.assertFalse(folder.exists())
        self.assertTrue(self.ledger.may_create_run(claim.attempt_key, claim.generation))

    def test_detect_only_policy_never_admits_a_run(self):
        configured = recipe_store.save_recipe(self.recipe_path, automated_recipe("detect"))
        detected = self.claim(recipe_sha=canonical_recipe_sha256(configured), policy="detect", detect_only=True)
        self.assertFalse(self.ledger.may_create_run(detected.attempt_key, detected.generation))
        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.preallocate_run(detected.attempt_key, 0)
        self.assertIn(raised.exception.code, {"automation_policy_ineligible", "automation_state_conflict"})

    def test_detect_only_handled_record_is_atomic_and_idempotent(self):
        configured = recipe_store.save_recipe(self.recipe_path, automated_recipe("detect"))
        recipe_sha = canonical_recipe_sha256(configured)
        first = self.claim(recipe_sha=recipe_sha, policy="detect", detect_only=True)
        second = AutomationLedger(self.data).claim_current_recipe(
            self.recipe_path, "recipe", identity(), recipe_sha, "detect", detect_only=True,
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        row = second.record["generations"][0]
        self.assertEqual(row["state"], "terminal")
        self.assertEqual(row["terminal_classification"], "success")
        self.assertEqual(row["diagnostic"], "detect_only_handled")
        self.assertFalse(self.ledger.may_create_run(first.attempt_key, first.generation))

    def test_link_rejects_unrelated_run_with_same_preallocated_id(self):
        claim = self.claim()
        row = self.ledger.preallocate_run(claim.attempt_key, 0)
        store = BuildStore(self.data / "builds")
        store.create({"schema_version": 5, "name": "other"}, recipe_id="other", run_id=row["preallocated_run_id"])
        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.link_run(claim.attempt_key, 0, row["preallocated_run_id"])
        self.assertEqual(raised.exception.code, "automation_run_binding_invalid")

    def test_terminal_retry_metadata_and_explicit_generation_roundtrip(self):
        claim = self.claim()
        finished = self.ledger.finish(
            claim.attempt_key, 0, "transient_retryable", retry_class="transient",
            retry_count=2, not_before="2030-01-02T03:04:05+00:00",
            last_error_code="github_unavailable", diagnostic="provider_recovery_required",
        )
        self.assertEqual(finished["state"], "retry_delayed")
        self.assertFalse(self.ledger.may_create_run(claim.attempt_key, claim.generation))
        retry = self.ledger.explicit_retry_current_recipe(self.recipe_path, "recipe", claim.attempt_key, 0, expected_updated_at=finished["updated_at"])
        self.assertEqual(retry.generation, 1)
        self.assertTrue(self.ledger.may_create_run(claim.attempt_key, 1))
        generations = AutomationLedger(self.data).read()["attempts"][claim.attempt_key]["generations"]
        self.assertEqual(generations[0]["retry_count"], 2)
        self.assertEqual(generations[0]["last_error_code"], "github_unavailable")
        self.assertEqual(generations[1]["generation"], 1)

    def test_terminal_classifications_persist_without_downstream_status(self):
        claim = self.claim()
        row = self.ledger.finish(claim.attempt_key, 0, "terminal_failure", last_error_code="build_failed")
        self.assertEqual(row["state"], "terminal")
        entry = self.ledger.read()["attempts"][claim.attempt_key]
        self.assertNotIn("build_status", entry)
        self.assertNotIn("validation_status", entry)
        self.assertNotIn("publication_status", entry)

    def test_retry_classification_combinations_fail_closed(self):
        cases = (
            ("success", {"retry_class": "transient"}),
            ("blocked_manual", {"retry_class": "none"}),
            ("terminal_failure", {"not_before": "2030-01-02T03:04:05+00:00"}),
        )
        for index, (classification, metadata) in enumerate(cases):
            claim = self.claim(identity=identity(str(8000 + index)))
            with self.subTest(classification=classification), self.assertRaises(AutomationLedgerError):
                self.ledger.finish(claim.attempt_key, 0, classification, **metadata)

    def test_unsupported_future_and_malformed_ledgers_fail_closed_without_repair(self):
        self.data.mkdir()
        cases = [
            ({"schema_version": 2, "attempts": {}}, "automation_ledger_future_version"),
            ({"schema_version": 1, "attempts": [], "token": "secret"}, "automation_ledger_malformed"),
        ]
        for document, code in cases:
            with self.subTest(document=document):
                self.ledger.path.write_text(json.dumps(document))
                before = self.ledger.path.read_bytes()
                with self.assertRaises(AutomationLedgerError) as raised:
                    self.ledger.read()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.ledger.path.read_bytes(), before)
        self.ledger.path.write_text("{")
        before = self.ledger.path.read_bytes()
        with self.assertRaises(AutomationLedgerError) as raised:
            self.claim()
        self.assertEqual(raised.exception.code, "automation_ledger_malformed")
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_pre_v1_generation_shape_is_rejected_without_upgrade(self):
        claim = self.claim()
        document = json.loads(self.ledger.path.read_text())
        generation = document["attempts"][claim.attempt_key]["generations"][0]
        for field in (
            "stage", "validation_attempt_id", "publication_attempt_id", "completion_notified",
            "run_retry_count", "validation_retry_count", "publication_retry_count",
        ):
            generation.pop(field)
        self.ledger.path.write_text(json.dumps(document))
        before = self.ledger.path.read_bytes()

        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.read()

        self.assertEqual(raised.exception.code, "automation_ledger_invalid")
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_noncanonical_persisted_upstream_identity_is_rejected(self):
        claim = self.claim()
        document = json.loads(self.ledger.path.read_text())
        document["attempts"][claim.attempt_key]["upstream_identity"].pop("schema_version")
        self.ledger.path.write_text(json.dumps(document))
        before = self.ledger.path.read_bytes()

        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.read()

        self.assertEqual(raised.exception.code, "automation_ledger_invalid")
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_current_lifecycle_stages_require_canonical_links(self):
        claim = self.claim()
        original = json.loads(self.ledger.path.read_text())
        cases = (
            {"state": "detected"},
            {"state": "claimed", "stage": "validation"},
            {
                "state": "admitted", "stage": "publication",
                "preallocated_run_id": "run-one", "run_id": "run-one",
            },
        )
        for updates in cases:
            with self.subTest(updates=updates):
                document = json.loads(json.dumps(original))
                row = document["attempts"][claim.attempt_key]["generations"][0]
                row.update(updates)
                self.ledger.path.write_text(json.dumps(document))
                before = self.ledger.path.read_bytes()
                with self.assertRaises(AutomationLedgerError) as raised:
                    self.ledger.read()
                self.assertEqual(raised.exception.code, "automation_ledger_invalid")
                self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_entry_bounds_refuse_growth_without_pruning_history(self):
        ledger = AutomationLedger(self.data, max_attempts=2, max_attempts_per_recipe=2)
        ledger.claim_current_recipe(self.recipe_path, "recipe", identity("1"), self.recipe_sha, "build")
        ledger.claim_current_recipe(self.recipe_path, "recipe", identity("2"), self.recipe_sha, "build")
        before = ledger.path.read_bytes()
        with self.assertRaises(AutomationLedgerError) as raised:
            ledger.claim_current_recipe(self.recipe_path, "recipe", identity("3"), self.recipe_sha, "build")
        self.assertEqual(raised.exception.code, "automation_ledger_bounds_exceeded")
        self.assertEqual(ledger.path.read_bytes(), before)

    def test_serialized_byte_bound_is_checked_before_first_write(self):
        with mock.patch("debbuilder.automation_ledger.MAX_LEDGER_BYTES", 64):
            with self.assertRaises(AutomationLedgerError) as raised:
                self.claim()
        self.assertEqual(raised.exception.code, "automation_ledger_bounds_exceeded")
        self.assertFalse(self.ledger.path.exists())

    def test_read_is_byte_identical_and_does_not_create_lock(self):
        self.claim()
        before = self.ledger.path.read_bytes()
        lock_before = self.ledger.lock_path.read_bytes()
        self.ledger.read()
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.ledger.lock_path.read_bytes(), lock_before)

    def test_diagnostic_rejects_urls_paths_and_unstable_error_codes(self):
        claim = self.claim()
        for values in (
            {"diagnostic": "private path /tmp/secret"},
            {"diagnostic": "request https://token.example.test"},
            {"diagnostic": "token=ghp_secret"},
            {"last_error_code": "secret token"},
        ):
            with self.subTest(values=values), self.assertRaises(AutomationLedgerError):
                self.ledger.finish(claim.attempt_key, 0, "terminal_failure", **values)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symbolic links unavailable")
    def test_ledger_symlink_is_refused_without_reading_target(self):
        self.data.mkdir()
        target = self.data / "target.json"
        target.write_text(json.dumps({"schema_version": 1, "attempts": {}}))
        self.ledger.path.symlink_to(target)
        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.read()
        self.assertEqual(raised.exception.code, "automation_ledger_unavailable")

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symbolic links unavailable")
    def test_lock_symlink_is_refused_without_mutating_ledger(self):
        self.data.mkdir()
        target = self.data / "foreign-lock"
        target.write_text("do-not-touch")
        self.ledger.lock_path.symlink_to(target)
        with self.assertRaises(AutomationLedgerError) as raised:
            self.claim()
        self.assertEqual(raised.exception.code, "automation_ledger_lock_invalid")
        self.assertEqual(target.read_text(), "do-not-touch")
        self.assertFalse(self.ledger.path.exists())


if __name__ == "__main__":
    unittest.main()
