import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from debbuilder import builtin_recipe, recipe_store, validation_service
from debbuilder.automation_identity import normalize_upstream_identity
from debbuilder.automation_ledger import AutomationLedger, AutomationLedgerError
from debbuilder.automation_scheduler import AutomationRetryStore
from debbuilder.automation_status import AutomationActionError, AutomationStatusService
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.recipe_schema import validate_recipe_metadata


def recipe(*, active=True, enabled=True, policy="build", description="demo"):
    return validate_recipe_metadata({
        "schema_version": 5,
        "name": "demo",
        "active": active,
        "automation": {"enabled": enabled, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64", "description": description},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
    })


def identity():
    return normalize_upstream_identity({
        "provider": "github",
        "repository": "example/demo",
        "tracking": "latest_release",
        "source_type": "release_asset",
        "payload_kind": "deb",
        "release_id": "10",
        "asset_id": "20",
        "asset_name": "demo_1.2.3_amd64.deb",
        "expected_size": 7,
        "resolved_ref": "v1.2.3",
        "resolved_version": "1.2.3",
        "expected_package": "demo",
        "expected_architecture": "amd64",
    })


class FakeScheduler:
    def __init__(self):
        self.requests = []
        self.wakes = 0
        self.activity = {"queued": False, "checking": False}
        self.running = True

    def status(self):
        return {
            "state": "running" if self.running else "stopped",
            "admission_open": self.running,
            "pass_active": False,
            "last_pass_started": "2026-09-15T10:00:00+00:00",
            "last_pass_finished": "2026-09-15T10:00:01+00:00",
            "next_scheduled_check": "2026-09-15T11:00:00+00:00",
            "active_recipe_checks": 1 if self.activity["checking"] else 0,
            "global_blocker": None,
        }

    def recipe_activity(self, _recipe_id):
        return dict(self.activity)

    def request_recipe(self, recipe_id):
        existing = recipe_id in self.requests
        if not existing:
            self.requests.append(recipe_id)
        self.activity["queued"] = True
        return {"accepted": True, "created": not existing, "recipe_id": recipe_id}

    def request(self):
        self.wakes += 1
        return True

    def request_orchestration(self):
        return self.request()


class AutomationStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recipes = self.root / "recipes"
        self.recipes.mkdir()
        self.data = self.root / "data"
        self.ledger = AutomationLedger(self.data)
        self.retry = AutomationRetryStore(self.data)
        self.builds = BuildStore(self.data / "builds")
        self.scheduler = FakeScheduler()
        self.service = AutomationStatusService(
            self.recipes,
            self.ledger,
            self.retry,
            self.builds,
            scheduler=lambda: self.scheduler,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, configured):
        return recipe_store.save_recipe(self.recipes / "demo.json", configured)

    def claim(self, policy="build"):
        configured = self.save(recipe(policy=policy))
        return self.ledger.claim_current_recipe(
            self.recipes / "demo.json",
            "demo",
            identity(),
            canonical_recipe_sha256(configured),
            policy,
            detect_only=policy == "detect",
        )

    def test_safe_defaults_distinguish_inactive_manual_and_watching(self):
        cases = (
            (recipe(active=False), "inactive", False),
            (recipe(enabled=False), "manual", False),
            (recipe(enabled=True, policy="manual"), "manual", False),
            (recipe(), "watching", True),
        )
        for configured, state, eligible in cases:
            with self.subTest(state=state, eligible=eligible):
                self.save(configured)
                projected = self.service.status("demo")
                self.assertEqual(projected["state"], state)
                self.assertEqual(projected["eligible"], eligible)
                self.assertEqual(projected["recipe_active"], configured["active"])
        with self.assertRaises(AutomationActionError) as missing:
            self.service.status("missing")
        self.assertEqual((missing.exception.code, missing.exception.status), ("recipe_not_found", 404))

    def test_latest_in_memory_check_observation_updates_display_without_a_write(self):
        claim = self.claim("detect")
        configured = recipe_store.load_recipe(self.recipes / "demo.json", write_back=False)
        checked_at = (
            datetime.fromisoformat(claim.record["generations"][-1]["updated_at"])
            + timedelta(seconds=1)
        ).isoformat()
        self.scheduler.activity["observation"] = {
            "checked_at": checked_at,
            "recipe_sha256": canonical_recipe_sha256(configured),
            "display_version": "1.2.3 display",
            "display_ref": "release/v1.2.3",
        }
        projected = self.service.status("demo")
        self.assertEqual(projected["last_check_at"], checked_at)
        self.assertEqual(projected["detected"], {
            "version": "1.2.3 display", "ref": "release/v1.2.3",
        })
        self.save(recipe(policy="detect", description="changed"))
        stale_filtered = self.service.status("demo")
        self.assertEqual(stale_filtered["detected"], {"version": "1.2.3", "ref": "v1.2.3"})

    def test_builtin_is_managed_manual_and_has_no_operator_actions(self):
        configured = builtin_recipe.load_builtin_definition()
        recipe_store.save_recipe(self.recipes / "debbuilder.json", configured)
        projected = self.service.status("debbuilder")
        self.assertEqual(projected["state"], "manual")
        self.assertEqual(projected["automation"], {
            "enabled": False, "policy": "manual", "managed": True,
        })
        self.assertFalse(projected["can_check_now"])
        self.assertFalse(projected["can_retry"])

    def test_detect_only_status_has_identity_without_fabricated_run(self):
        self.claim("detect")
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "up_to_date")
        self.assertEqual(projected["result"], "success")
        self.assertEqual(projected["detected"], {"version": "1.2.3", "ref": "v1.2.3"})
        self.assertIsNone(projected["run"])

    def test_linked_run_state_is_read_from_canonical_run(self):
        claim = self.claim("build")
        row = self.ledger.preallocate_run(claim.attempt_key, 0)
        configured = recipe_store.load_recipe(self.recipes / "demo.json", write_back=False)
        run = self.builds.create(
            configured,
            recipe_id="demo",
            mode="build",
            run_id=row["preallocated_run_id"],
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation={
                "attempt_key": claim.attempt_key,
                "generation": 0,
                "policy": "build",
                "expected_upstream_identity": identity(),
            },
        )
        self.ledger.link_run(claim.attempt_key, 0, run["id"], store=self.builds)
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "build_queued")
        with self.builds.locked_run(run["id"]):
            current = self.builds.load(run["id"])
            current["status"] = "failed"
            self.builds.save(current)
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "failed")
        self.assertEqual(projected["run"], {
            "id": run["id"], "status": "failed", "mode": "build", "url": f"/#logs/{run['id']}",
        })

    def test_canonical_stage_matrix_uses_captured_attempt_policy(self):
        row = {"desired_policy": "full", "state": "admitted", "terminal_classification": None}
        cases = (
            ({"status": "pending", "mode": "dry_run"}, None, None, "test_queued"),
            ({"status": "running", "mode": "dry_run"}, None, None, "testing"),
            ({"status": "pending", "mode": "build"}, None, None, "build_queued"),
            ({"status": "running", "mode": "build"}, None, None, "building"),
            ({"status": "success", "mode": "build"}, None, None, "validation_pending"),
            ({"status": "success", "mode": "build"}, {"status": "running"}, None, "validating"),
            ({"status": "success", "mode": "build"}, {"status": "success"}, None, "publication_pending"),
            ({"status": "success", "mode": "build"}, {"status": "success"}, {"status": "running"}, "publishing"),
            ({"status": "success", "mode": "build"}, {"status": "success"}, {"status": "success"}, "up_to_date"),
            ({"status": "success", "mode": "build"}, {"status": "failed"}, None, "failed"),
            ({"status": "success", "mode": "build"}, {"status": "success"}, {"status": "failed"}, "failed"),
        )
        for run, validation, publication, expected in cases:
            with self.subTest(expected=expected):
                state, _result = self.service._state(row, run, validation, publication)
                self.assertEqual(state, expected)

    def test_durable_retry_and_terminal_outcomes_override_earlier_canonical_stages(self):
        claim = self.claim("full")
        run = {"id": "run-one", "status": "success", "mode": "build"}
        validation = {"attempt_id": "old", "status": "failed", "phase": "preparation"}
        self.ledger.finish(
            claim.attempt_key, 0, "transient_retryable", retry_class="transient",
            retry_count=1, not_before="2030-01-01T00:00:00+00:00",
            last_error_code="validation_preparation_failed", diagnostic="validation_transient_retry",
        )
        with mock.patch.object(self.service, "_canonical_links", return_value=(run, validation, None, None)):
            delayed = self.service.status("demo")
        self.assertEqual(delayed["state"], "retry_scheduled")
        self.assertTrue(delayed["retry"]["scheduled"])
        self.assertTrue(delayed["state_active"])
        self.assertEqual(delayed["validation"]["attempt_id"], "old")

        self.ledger.finish(
            claim.attempt_key, 0, "terminal_failure", retry_class="none", retry_count=1,
            last_error_code="validation_admission_failed", diagnostic="validation_admission_failed",
        )
        with mock.patch.object(self.service, "_canonical_links", return_value=(run, None, None, None)):
            failed = self.service.status("demo")
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["can_retry"])

        self.ledger.finish(
            claim.attempt_key, 0, "disabled", retry_class="none", retry_count=1,
            last_error_code="automation_downstream_disabled", diagnostic="downstream_admission_disabled",
        )
        with mock.patch.object(self.service, "_canonical_links", return_value=(run, None, None, None)):
            disabled = self.service.status("demo")
        self.assertEqual(disabled["state"], "manual")
        self.assertEqual(disabled["result"], "disabled")
        self.assertFalse(disabled["can_retry"])

    def test_projection_excludes_internal_identity_and_paths(self):
        self.claim("build")
        projected = self.service.status("demo")
        encoded = json.dumps(projected, sort_keys=True)
        for forbidden in (
            "attempt_key", "recipe_sha256", "upstream_identity", "expected_upstream_identity",
            "workspace", "ledger_path", "lock_name", "worker_id", "token", "command", "asset_url", "api_url",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_status_read_does_not_write_recipe_ledger_run_validation_or_publication(self):
        claim = self.claim("full")
        allocation = self.ledger.preallocate_run(claim.attempt_key, 0)
        configured = recipe_store.load_recipe(self.recipes / "demo.json", write_back=False)
        run = self.builds.create(
            configured,
            recipe_id="demo",
            mode="build",
            run_id=allocation["preallocated_run_id"],
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation={
                "attempt_key": claim.attempt_key,
                "generation": 0,
                "policy": "full",
                "expected_upstream_identity": identity(),
            },
        )
        self.ledger.link_run(claim.attempt_key, 0, run["id"], store=self.builds)
        with self.builds.locked_run(run["id"]):
            current = self.builds.load(run["id"])
            current["status"] = "success"
            current["publications"] = [{
                "id": "publication-one", "status": "running",
                "requested_at": "2026-09-15T12:00:00+00:00", "finished_at": None,
            }]
            self.builds.save(current)
        attempt_path = validation_service.attempt_root(
            self.builds, run["id"], "validation-one",
        ) / "attempt.json"
        attempt_path.parent.mkdir(parents=True)
        validation_service._save_attempt(attempt_path, {
            "contract_version": 1,
            "id": "validation-one",
            "build_run_id": run["id"],
            "inputs": {
                "profile": "debian-package",
                "artifact": {
                    "package": "demo", "version": "1.2.3", "architecture": "amd64",
                    "size": 7, "sha256": "a" * 64,
                },
                "previous_artifact": None,
            },
            "selected_profile": {
                "name": "debian-package",
                "image": {"name": "debian:bookworm", "id": None, "digest": None},
            },
            "created_at": "2026-09-15T11:59:00+00:00",
            "started_at": None,
            "finished_at": None,
            "status": "queued",
            "prepared_dependencies": None,
            "result": None,
            "error": None,
        })
        self.ledger.advance_lifecycle(
            claim.attempt_key, 0, "publication",
            validation_attempt_id="validation-one", publication_attempt_id="publication-one",
        )
        watched = [
            self.recipes / "demo.json", self.ledger.path,
            self.builds.run_dir(run["id"]) / "run.json", attempt_path,
        ]
        before = {path: path.read_bytes() for path in watched}
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "publishing")
        self.assertEqual(projected["validation"]["attempt_id"], "validation-one")
        self.assertEqual(projected["publication"]["attempt_id"], "publication-one")
        self.assertEqual(before, {path: path.read_bytes() for path in watched})
        self.assertFalse(self.retry.path.exists())

    def test_non_retryable_publication_conflict_does_not_offer_retry(self):
        claim = self.claim("full")
        self.ledger.finish(
            claim.attempt_key, 0, "terminal_failure",
            last_error_code="publication_identity_conflict",
            diagnostic="publication_identity_conflict",
        )
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "failed")
        self.assertFalse(projected["can_retry"])

    def test_malformed_or_oversized_publication_history_is_a_bounded_blocker(self):
        row = {
            "run_id": "run-one", "validation_attempt_id": None,
            "publication_attempt_id": "publication-one",
        }
        for publications in (["malformed"], [{}] * 513):
            with self.subTest(size=len(publications)), mock.patch.object(
                self.builds, "load", return_value={
                    "id": "run-one", "status": "success", "mode": "build",
                    "publications": publications,
                },
            ):
                _run, _validation, publication, blocker = self.service._canonical_links({}, row)
                self.assertIsNone(publication)
                self.assertEqual(blocker["code"], "publication_state_unavailable")

    def test_malformed_ledger_is_a_bounded_blocker_without_repair(self):
        self.save(recipe())
        self.data.mkdir()
        self.ledger.path.write_text('{"schema_version":99,"attempts":{}}\n')
        before = self.ledger.path.read_bytes()
        projected = self.service.status("demo")
        self.assertEqual(projected["state"], "blocked")
        self.assertEqual(projected["blocked"]["code"], "automation_ledger_future_version")
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_check_now_is_queued_and_duplicate_requests_coalesce(self):
        self.save(recipe())
        first = self.service.check_now("demo")
        second = self.service.check_now("demo")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(self.scheduler.requests, ["demo"])
        self.assertEqual(first["status"]["state"], "checking")
        self.assertFalse(first["status"]["can_check_now"])

    def test_check_now_rejects_inactive_disabled_and_manual(self):
        cases = (
            (recipe(active=False), "recipe_inactive"),
            (recipe(enabled=False), "automation_disabled"),
            (recipe(policy="manual"), "automation_disabled"),
        )
        for configured, code in cases:
            with self.subTest(code=code):
                self.save(configured)
                with self.assertRaises(AutomationActionError) as raised:
                    self.service.check_now("demo")
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.scheduler.requests, [])

    def test_retry_creates_one_generation_and_rejects_stale_recipe(self):
        claim = self.claim("build")
        self.ledger.finish(
            claim.attempt_key, 0, "terminal_failure",
            last_error_code="build_command_failed", diagnostic="run_failed",
        )
        observed = self.service.status("demo")
        retried = self.service.retry("demo", {
            "generation": observed["generation"], "revision": observed["revision"],
        })
        self.assertTrue(retried["created"])
        self.assertEqual(retried["generation"], 1)
        self.assertEqual(len(self.ledger.read()["attempts"][claim.attempt_key]["generations"]), 2)
        self.assertEqual(self.scheduler.wakes, 1)

        changed = recipe(description="changed")
        self.save(changed)
        current = self.service.status("demo")
        self.assertFalse(current["can_retry"])
        with self.assertRaises(AutomationActionError) as raised:
            self.service.retry("demo", {"generation": 1, "revision": current["revision"]})
        self.assertEqual(raised.exception.code, "automation_retry_not_allowed")

    def test_retry_revision_cannot_slide_to_a_newer_upstream_attempt(self):
        original = self.claim("build")
        self.ledger.finish(
            original.attempt_key, 0, "terminal_failure",
            last_error_code="build_command_failed", diagnostic="run_failed",
        )
        original_status = self.service.status
        newer = {}

        def status_then_detect_newer(recipe_id):
            projected = original_status(recipe_id)
            newer_identity = identity()
            newer_identity["release_id"] = "11"
            newer_identity["asset_id"] = "21"
            claim = self.ledger.claim_current_recipe(
                self.recipes / "demo.json", "demo", newer_identity,
                canonical_recipe_sha256(recipe()), "build",
                detected_at="2099-01-01T00:00:00+00:00",
            )
            self.ledger.finish(
                claim.attempt_key, 0, "terminal_failure",
                last_error_code="build_command_failed", diagnostic="run_failed",
            )
            newer["key"] = claim.attempt_key
            return projected

        with mock.patch.object(self.service, "status", side_effect=status_then_detect_newer):
            with self.assertRaises(AutomationActionError) as raised:
                self.service.retry("demo", {
                    "generation": 0,
                    "revision": original_status("demo")["revision"],
                })
        self.assertEqual(raised.exception.code, "automation_retry_stale")
        document = self.ledger.read()
        self.assertEqual(len(document["attempts"][original.attempt_key]["generations"]), 1)
        self.assertEqual(len(document["attempts"][newer["key"]]["generations"]), 1)

    def test_retry_cannot_bypass_disabled_recipe_or_generation_bound(self):
        claim = self.claim("build")
        self.ledger.finish(claim.attempt_key, 0, "terminal_failure", diagnostic="run_failed")
        configured = recipe(enabled=False)
        self.save(configured)
        projected = self.service.status("demo")
        self.assertFalse(projected["can_retry"])
        with self.assertRaises(AutomationActionError) as disabled:
            self.service.retry("demo", {"generation": 0, "revision": projected["revision"]})
        self.assertEqual(disabled.exception.code, "automation_disabled")

        self.save(recipe())
        # Restoring the exact Recipe returns to the immutable attempt SHA.
        for generation in range(7):
            result = self.ledger.explicit_retry_current_recipe(
                self.recipes / "demo.json", "demo", claim.attempt_key, generation,
            )
            self.ledger.finish(result.attempt_key, result.generation, "terminal_failure", diagnostic="run_failed")
        self.assertEqual(len(self.ledger.read()["attempts"][claim.attempt_key]["generations"]), 8)
        self.assertFalse(self.service.status("demo")["can_retry"])

    def test_atomic_duplicate_retry_reuses_one_generation(self):
        claim = self.claim("build")
        self.ledger.finish(claim.attempt_key, 0, "terminal_failure", diagnostic="run_failed")
        gate = threading.Barrier(3)
        results = []

        def retry():
            gate.wait()
            results.append(self.ledger.explicit_retry_current_recipe(
                self.recipes / "demo.json", "demo", claim.attempt_key, 0,
            ))

        threads = [threading.Thread(target=retry) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(3)
        self.assertEqual(sorted(result.created for result in results), [False, True])
        self.assertEqual({result.generation for result in results}, {1})
        self.assertEqual(len(self.ledger.read()["attempts"][claim.attempt_key]["generations"]), 2)

    def test_duplicate_retry_reuse_still_revalidates_current_recipe(self):
        claim = self.claim("build")
        self.ledger.finish(claim.attempt_key, 0, "terminal_failure", diagnostic="run_failed")
        created = self.ledger.explicit_retry_current_recipe(
            self.recipes / "demo.json", "demo", claim.attempt_key, 0,
        )
        self.assertTrue(created.created)
        self.save(recipe(enabled=False))
        with self.assertRaises(AutomationActionError):
            self.service.retry("demo", {"generation": 0, "revision": "stale"})
        with self.assertRaises(AutomationLedgerError) as raised:
            self.ledger.explicit_retry_current_recipe(
                self.recipes / "demo.json", "demo", claim.attempt_key, 0,
            )
        self.assertEqual(getattr(raised.exception, "code", None), "automation_recipe_stale")


if __name__ == "__main__":
    unittest.main()
