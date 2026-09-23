"""Authoritative advisory observation ownership and concurrency tests."""
from __future__ import annotations
from tests.lifecycle_helpers import in_flight_count

import json
import tempfile
import threading
import unittest
from pathlib import Path

from debbuilder import recipe_store
from debbuilder.build_store import canonical_recipe_sha256
from debbuilder.upstream_detection import UpstreamDetectionError
from debbuilder.upstream_observation import (
    MAX_DISPLAY_LENGTH,
    UpstreamObservationError,
    UpstreamObservationService,
    UpstreamObservationStore,
)


def recipe(name="demo", *, active=True, enabled=False, policy="manual"):
    return {
        "schema_version": 5,
        "name": name,
        "active": active,
        "automation": {"enabled": enabled, "policy": policy},
        "package": {"name": name, "architecture": "all"},
        "source": {
            "provider": "github", "repository": f"owner/{name}", "tracking": "latest_release",
            "version": {"source": "tag"},
        },
    }


class ObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = UpstreamObservationStore(self.root, wall_clock=lambda: 2_000_000_000.0)
        self.recipe = recipe()
        self.sha = canonical_recipe_sha256(self.recipe)

    def tearDown(self):
        self.temporary.cleanup()

    def test_absent_v1_matching_and_hash_mismatch_are_read_only(self):
        self.assertEqual(self.store.read(), {"schema_version": 1, "recipes": {}})
        self.assertIsNone(self.store.projection("demo", self.sha))
        self.assertFalse(self.store.path.exists())
        saved = self.store.record_success(
            "demo", self.sha, {"display_version": "1.2.3", "display_ref": "v1.2.3"},
        )
        self.assertEqual(saved["last_success"]["display_version"], "1.2.3")
        self.assertEqual(self.store.projection("demo", self.sha), saved)
        self.assertIsNone(self.store.projection("demo", "f" * 64))

    def test_failure_preserves_matching_last_success(self):
        success = self.store.record_success(
            "demo", self.sha, {"display_version": "1.2.3", "display_ref": "v1.2.3"},
        )["last_success"]
        failed = self.store.record_failure(
            "demo", self.sha, classification="upstream_unavailable", diagnostic_code="github_unavailable",
        )
        self.assertEqual(failed["last_success"], success)
        self.assertEqual(failed["last_attempt"]["status"], "failed")

    def test_document_is_bounded_and_contains_no_exact_identity_or_urls(self):
        with self.assertRaises(UpstreamObservationError):
            self.store.record_success(
                "demo", self.sha,
                {"display_version": "x" * (MAX_DISPLAY_LENGTH + 1), "display_ref": "v1"},
            )
        with self.assertRaises(UpstreamObservationError):
            self.store.record_success(
                "demo", self.sha,
                {"display_version": "1", "display_ref": "https://token@example.invalid/release"},
            )
        self.store.record_success(
            "demo", self.sha, {
                "display_version": "1.2.3", "display_ref": "v1.2.3",
                "identity": {"commit_sha": "a" * 40, "token": "secret"},
                "download_url": "https://token@example.invalid/private",
            },
        )
        persisted = self.store.path.read_text()
        self.assertNotIn("commit_sha", persisted)
        self.assertNotIn("secret", persisted)
        self.assertNotIn("http", persisted)

    def test_corrupt_and_future_documents_are_unavailable_without_repair(self):
        for raw in ("not-json", json.dumps({"schema_version": 99, "recipes": {}})):
            with self.subTest(raw=raw):
                self.store.path.write_text(raw)
                before = self.store.path.read_bytes()
                self.assertIsNone(self.store.projection("demo", self.sha))
                self.assertEqual(self.store.path.read_bytes(), before)
                with self.assertRaises(UpstreamObservationError):
                    self.store.read()


class ObservationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "demo.json"
        self.recipe = recipe()
        recipe_store.save_recipe(self.path, self.recipe)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def result(version="1.2.3"):
        return {
            "display_version": version, "display_ref": f"v{version}",
            "identity": {"resolved_ref": f"v{version}", "commit_sha": "a" * 40},
        }

    def test_same_recipe_single_flight_shares_one_fresh_result_and_cleans_up(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def resolver(_recipe, *, token=""):
            calls.append(token)
            entered.set()
            self.assertTrue(release.wait(3))
            return self.result()

        service = UpstreamObservationService(UpstreamObservationStore(self.root), resolver=resolver)
        results = []
        errors = []

        def observe():
            try:
                results.append(service.observe("demo", self.recipe, self.path, token="bounded"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=observe) for _index in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(2))
        threading.Event().wait(0.05)
        release.set()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(calls, ["bounded"])
        self.assertEqual(len(results), 6)
        self.assertTrue(all(row["identity"] == results[0]["identity"] for row in results))
        self.assertEqual(in_flight_count(service), 0)

    def test_waiters_receive_same_error_and_failure_is_persisted(self):
        error = UpstreamDetectionError("github_unavailable", "bounded")
        entered = threading.Event()
        release = threading.Event()

        def fail(_recipe, token=""):
            entered.set()
            self.assertTrue(release.wait(3))
            raise error

        service = UpstreamObservationService(
            UpstreamObservationStore(self.root), resolver=fail,
        )
        errors = []
        threads = [threading.Thread(target=lambda: self._capture(
            errors, lambda: service.observe("demo", self.recipe, self.path),
        )) for _index in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(2))
        threading.Event().wait(0.05)
        release.set()
        for thread in threads:
            thread.join(3)
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(raised is error for raised in errors))
        row = service.store.projection("demo", canonical_recipe_sha256(self.recipe))
        self.assertEqual(row["last_attempt"]["diagnostic_code"], "github_unavailable")
        self.assertEqual(in_flight_count(service), 0)

    def test_recipe_mutation_during_network_does_not_persist_stale_result(self):
        entered = threading.Event()
        release = threading.Event()

        def resolver(_recipe, *, token=""):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.result()

        service = UpstreamObservationService(UpstreamObservationStore(self.root), resolver=resolver)
        errors = []
        thread = threading.Thread(target=lambda: self._capture(
            errors, lambda: service.observe("demo", self.recipe, self.path),
        ))
        thread.start()
        self.assertTrue(entered.wait(2))
        changed = recipe()
        changed["source"]["repository"] = "owner/changed"
        recipe_store.save_recipe(self.path, changed)
        release.set()
        thread.join(3)
        self.assertEqual(getattr(errors[0], "code", None), "recipe_changed_during_detection")
        self.assertFalse(service.store.path.exists())
        self.assertEqual(in_flight_count(service), 0)

    @staticmethod
    def _capture(errors, operation):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    def test_in_flight_coordination_is_bounded(self):
        entered = threading.Event()
        release = threading.Event()
        other = recipe("other")
        other_path = self.root / "other.json"
        recipe_store.save_recipe(other_path, other)

        def resolver(_recipe, *, token=""):
            entered.set()
            release.wait(3)
            return self.result()

        service = UpstreamObservationService(
            UpstreamObservationStore(self.root), resolver=resolver, max_in_flight=1,
        )
        thread = threading.Thread(target=lambda: service.observe("demo", self.recipe, self.path))
        thread.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(UpstreamObservationError) as raised:
            service.observe("other", other, other_path)
        self.assertEqual(raised.exception.code, "observation_capacity_exhausted")
        release.set()
        thread.join(3)


if __name__ == "__main__":
    unittest.main()
