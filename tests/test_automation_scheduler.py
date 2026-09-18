import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from debbuilder import recipe_store, source_acquisition, upstream_archive, upstream_artifact
from debbuilder.automation_identity import normalize_upstream_identity
from debbuilder.automation_ledger import AutomationLedger, AutomationLedgerError
from debbuilder.automation_scheduler import (
    MAX_BACKOFF_SECONDS,
    AutomationRetryStore,
    AutomationScheduler,
    AutomationSchedulerError,
)
from debbuilder.build_store import canonical_recipe_sha256
from debbuilder.lifecycle import MutationGate, MutationGateClosed
from debbuilder.recipe_schema import validate_recipe_metadata
from debbuilder.upstream_detection import AutomationDetectionService, UpstreamDetectionError, detect_upstream


def recipe(name="demo", *, policy="build", active=True, enabled=True, tracking="latest_release"):
    return validate_recipe_metadata({
        "schema_version": 5, "name": name, "active": active,
        "automation": {"enabled": enabled, "policy": policy},
        "package": {"name": name, "architecture": "amd64"},
        "source": {"repository": "example/demo", "tracking": tracking, "ref": "main" if tracking == "manual" else ""},
    })


def identity(asset_id="20"):
    return normalize_upstream_identity({
        "provider": "github", "repository": "example/demo", "tracking": "latest_release",
        "source_type": "release_asset", "payload_kind": "deb", "release_id": "10",
        "asset_id": asset_id, "asset_name": "demo_1.2.3_amd64.deb", "expected_size": 7,
        "resolved_ref": "v1.2.3", "resolved_version": "1.2.3",
        "expected_package": "demo", "expected_architecture": "amd64",
    })


def source_mode_recipe(name, *, mode="source_build", tracking="latest_release", ref="", asset_name=""):
    artifact = {"mode": mode, "architecture": "amd64"}
    if mode == "upstream_archive":
        artifact.update({
            "type": "archive", "archive_source": "release_asset", "asset_selection": "exact",
            "asset_name": asset_name, "payload": {"mode": "entire_archive"},
        })
    elif mode == "upstream_deb":
        artifact["name_pattern"] = "demo_*_amd64.deb"
    return validate_recipe_metadata({
        "schema_version": 5, "name": name, "active": True,
        "automation": {"enabled": True, "policy": "build"},
        "package": {"name": name, "architecture": "amd64"},
        "source": {
            "repository": "example/demo", "tracking": tracking, "ref": ref,
            "version": {
                "source": "tag" if tracking == "latest_release" else "regex",
                "expression": "" if tracking == "latest_release" else r"([0-9]+(?:\.[0-9]+)*)",
            },
        },
        "artifact": artifact,
    })


def success(recipe_id, *, policy="build"):
    return {
        "recipe_id": recipe_id, "recipe_sha256": "a" * 64, "policy": policy,
        "identity": identity(), "classification": "detected", "change": "new",
        "attempt_key": "automation-v1-" + "b" * 64, "generation": 0,
        "attempt_state": "claimed", "claim_eligible": policy != "detect", "diagnostic": None,
        "retry_after_seconds": None, "rate_limit_reset": None,
    }


class FakeService:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []

    def check(self, recipe_id, *, token="", mutation_lease=None):
        self.calls.append(recipe_id)
        if self.callback:
            return self.callback(recipe_id, mutation_lease)
        return success(recipe_id)


class InjectedDetectionService:
    def __init__(self, service, detector):
        self.service = service
        self.detector = detector

    def check(self, recipe_id, *, token="", mutation_lease=None):
        return self.service.check(recipe_id, token=token, detector=self.detector, mutation_lease=mutation_lease)


class AutomationSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recipes = self.root / "recipes"
        self.recipes.mkdir()
        self.data = self.root / "data"
        self.ledger = AutomationLedger(self.data)
        self.retry = AutomationRetryStore(self.data)
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.stop()
            scheduler.join(3)
        self.temporary.cleanup()

    def save(self, configured):
        return recipe_store.save_recipe(self.recipes / f"{configured['name']}.json", configured)

    def scheduler(self, service, **kwargs):
        scheduler = AutomationScheduler(
            self.recipes, service, self.ledger, self.retry,
            interval_seconds=kwargs.pop("interval_seconds", 3600),
            concurrency=kwargs.pop("concurrency", 2), **kwargs,
        )
        self.schedulers.append(scheduler)
        return scheduler

    def test_scheduled_policies_claim_without_creating_any_lifecycle_state(self):
        policies = ("detect", "test", "build", "build_validate", "full")
        for index, policy in enumerate(policies):
            self.save(recipe(f"demo{index}", policy=policy))
        self.save(recipe("manual", policy="manual"))
        self.save(recipe("disabled", enabled=False))
        self.save(recipe("inactive", active=False))
        self.save(recipe("debbuilder", policy="detect"))

        base = AutomationDetectionService(self.recipes, self.ledger)
        service = InjectedDetectionService(base, lambda _recipe, token="": {
            "identity": identity(), "display_version": "1.2.3", "display_ref": "v1.2.3",
        })
        with mock.patch("debbuilder.build_store.BuildStore.allocate_run_id") as allocate, \
                mock.patch("debbuilder.build_store.BuildStore.create") as create, \
                mock.patch("debbuilder.execution_manager.ExecutionManager.submit") as submit, \
                mock.patch("debbuilder.validation_service.ValidationManager.admit") as validate, \
                mock.patch("debbuilder.artifact_publication.publish_artifact") as publish, \
                mock.patch("debbuilder.app.enqueue_recipe_run") as enqueue:
            results = self.scheduler(service, concurrency=3).run_pass()
        allocate.assert_not_called()
        create.assert_not_called()
        submit.assert_not_called()
        validate.assert_not_called()
        publish.assert_not_called()
        enqueue.assert_not_called()

        self.assertEqual({row["recipe_id"] for row in results}, {f"demo{i}" for i in range(5)})
        entries = self.ledger.read()["attempts"].values()
        self.assertEqual(len(list(entries)), 5)
        states = {entry["recipe_id"]: entry["generations"][0]["state"] for entry in entries}
        self.assertEqual(states["demo0"], "terminal")
        self.assertTrue(all(states[f"demo{i}"] == "claimed" for i in range(1, 5)))
        self.assertFalse((self.data / "builds").exists())
        self.assertFalse(any(self.data.rglob("run.json")))

    def test_operator_check_converges_with_an_inflight_scheduled_check(self):
        self.save(recipe("demo"))
        entered = threading.Event()
        release = threading.Event()

        def callback(recipe_id, _lease):
            entered.set()
            release.wait(3)
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback), concurrency=1)
        scheduler.start()
        self.assertTrue(entered.wait(2))
        accepted = scheduler.request_recipe("demo")
        self.assertTrue(accepted["accepted"])
        self.assertFalse(accepted["created"])
        self.assertTrue(scheduler.recipe_activity("demo")["checking"])
        release.set()
        deadline = threading.Event()
        for _ in range(100):
            if not scheduler.recipe_activity("demo")["checking"]:
                break
            deadline.wait(0.01)
        scheduler.stop()
        scheduler.join(3)
        self.assertEqual(scheduler.detection_service.calls, ["demo"])

    def test_operator_check_during_pass_inventory_does_not_add_followup_detection(self):
        self.save(recipe("demo"))
        scheduler = self.scheduler(FakeService(), concurrency=1)
        entered = threading.Event()
        release = threading.Event()
        original = scheduler._eligible

        def delayed_inventory(retries):
            entered.set()
            release.wait(3)
            return original(retries)

        scheduler._eligible = delayed_inventory
        scheduler.start()
        self.assertTrue(entered.wait(2))
        accepted = scheduler.request_recipe("demo")
        self.assertTrue(accepted["created"])
        release.set()
        for _ in range(200):
            if scheduler.detection_service.calls and not scheduler.status()["pass_active"]:
                break
            threading.Event().wait(0.01)
        scheduler.stop()
        scheduler.join(3)
        self.assertEqual(scheduler.detection_service.calls, ["demo"])

    def test_operator_check_after_recipe_result_queues_one_followup_pass(self):
        self.save(recipe("demo"))
        self.save(recipe("other"))
        other_entered = threading.Event()
        release_other = threading.Event()
        demo_twice = threading.Event()
        counts = {"demo": 0, "other": 0}

        def callback(recipe_id, _lease):
            counts[recipe_id] += 1
            if recipe_id == "other" and counts[recipe_id] == 1:
                other_entered.set()
                release_other.wait(3)
            if recipe_id == "demo" and counts[recipe_id] == 2:
                demo_twice.set()
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback), concurrency=2)
        scheduler.start()
        self.assertTrue(other_entered.wait(2))
        for _ in range(200):
            with scheduler._condition:
                demo_complete = "demo" not in scheduler._pass_recipes and "demo" not in scheduler._inflight
            if demo_complete:
                break
            threading.Event().wait(0.01)
        self.assertTrue(demo_complete)
        changed = recipe("demo")
        changed["package"]["description"] = "new revision"
        self.save(changed)
        accepted = scheduler.request_recipe("demo")
        self.assertTrue(accepted["created"])
        release_other.set()
        self.assertTrue(demo_twice.wait(3))
        scheduler.stop()
        scheduler.join(3)
        self.assertEqual(counts["demo"], 2)

    def test_operator_check_is_rejected_after_scheduler_shutdown(self):
        self.save(recipe("demo"))
        scheduler = self.scheduler(FakeService())
        scheduler.start()
        scheduler.stop()
        with self.assertRaises(AutomationSchedulerError) as raised:
            scheduler.request_recipe("demo")
        self.assertEqual(raised.exception.code, "automation_scheduler_unavailable")

    def test_scheduled_checks_use_canonical_detection_for_every_supported_source_mode(self):
        configurations = (
            source_mode_recipe("generated"),
            source_mode_recipe("archive", mode="upstream_archive", asset_name="demo-linux.tar.gz"),
            source_mode_recipe("raw", mode="upstream_archive", asset_name="install.sh"),
            source_mode_recipe("deb", mode="upstream_deb"),
            source_mode_recipe("tag", tracking="tag", ref="v1.2.3"),
            source_mode_recipe("branch", tracking="manual", ref="branch-1.2.3"),
            source_mode_recipe("commit", tracking="manual", ref="a1" * 20),
        )
        for configured in configurations:
            self.save(configured)

        release = {
            "repository": "example/demo", "release_id": 10, "tag": "v1.2.3", "ref": "v1.2.3",
            "upstream_version": "1.2.3", "commit": "a1" * 20, "ref_object_sha": "b2" * 20,
            "assets": [
                {"asset_id": 20, "name": "demo-linux.tar.gz", "size": 7, "digest": "", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz"},
                {"asset_id": 21, "name": "install.sh", "size": 8, "digest": "", "url": "https://github.com/example/demo/releases/download/v1.2.3/install.sh"},
                {"asset_id": 22, "name": "demo_1.2.3_amd64.deb", "size": 9, "digest": "", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo_1.2.3_amd64.deb"},
            ],
        }

        def resolved_source(configured, token="", exact=False):
            tracking = configured["source"]["tracking"]
            if tracking == "latest_release":
                selected = normalize_upstream_identity({
                    "provider": "github", "repository": "example/demo", "tracking": tracking,
                    "source_type": "github_source_archive", "payload_kind": "source_archive",
                    "release_id": "10", "commit_sha": "a1" * 20, "ref_object_sha": "b2" * 20,
                    "resolved_ref": "v1.2.3", "resolved_version": "1.2.3", "source_archive_format": "tar.gz",
                })
                selected_ref = "v1.2.3"
            else:
                selected_ref = configured["source"]["ref"]
                selected = normalize_upstream_identity({
                    "provider": "github", "repository": "example/demo", "tracking": tracking,
                    "source_type": "git_ref", "payload_kind": "source_archive", "requested_ref": selected_ref,
                    "commit_sha": "a1" * 20, "ref_object_sha": "b2" * 20 if tracking == "tag" else "",
                    "resolved_ref": selected_ref, "resolved_version": "1.2.3", "source_archive_format": "tar.gz",
                })
            return {"upstream_identity": selected, "upstream_version": "1.2.3", "ref": selected_ref}

        base = AutomationDetectionService(self.recipes, self.ledger)
        service = InjectedDetectionService(base, detect_upstream)
        with mock.patch.object(source_acquisition, "resolve_source", side_effect=resolved_source), \
                mock.patch.object(upstream_archive, "resolve_release", return_value=release), \
                mock.patch.object(upstream_artifact, "resolve_release", return_value=release):
            results = self.scheduler(service, concurrency=3).run_pass()
        self.assertEqual({row["recipe_id"] for row in results}, {row["name"] for row in configurations})
        identities = {row["recipe_id"]: row["identity"] for row in results}
        self.assertEqual(identities["generated"]["source_type"], "github_source_archive")
        self.assertEqual(identities["archive"]["payload_kind"], "archive")
        self.assertEqual(identities["raw"]["payload_kind"], "raw_file")
        self.assertEqual(identities["deb"]["payload_kind"], "deb")
        self.assertTrue(all(identities[name]["source_type"] == "git_ref" for name in ("tag", "branch", "commit")))

    def test_same_identity_reuses_claim_and_changed_recipe_or_identity_is_new(self):
        configured = recipe("demo")
        self.save(configured)
        chosen = [identity()]
        base = AutomationDetectionService(self.recipes, self.ledger)
        service = InjectedDetectionService(base, lambda _recipe, token="": {
            "identity": chosen[0], "display_version": "1.2.3", "display_ref": "v1.2.3",
        })
        scheduler = self.scheduler(service)
        first = scheduler.run_pass()[0]
        second = scheduler.run_pass()[0]
        changed = copy.deepcopy(configured)
        changed["package"]["description"] = "changed"
        self.save(changed)
        third = scheduler.run_pass()[0]
        chosen[0] = identity("21")
        fourth = scheduler.run_pass()[0]
        self.assertEqual(second["change"], "existing")
        self.assertEqual(len({first["attempt_key"], third["attempt_key"], fourth["attempt_key"]}), 3)
        self.assertEqual(len(self.ledger.read()["attempts"]), 3)

    def test_bounded_workers_allow_another_recipe_to_finish_while_one_is_blocked(self):
        for name in ("one", "two", "three"):
            self.save(recipe(name))
        blocked = threading.Event()
        release = threading.Event()
        quick = threading.Event()
        active = 0
        maximum = 0
        lock = threading.Lock()

        def callback(recipe_id, _lease):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if recipe_id == "one":
                    blocked.set()
                    release.wait(3)
                else:
                    quick.set()
                return success(recipe_id)
            finally:
                with lock:
                    active -= 1

        scheduler = self.scheduler(FakeService(callback), concurrency=2)
        thread = threading.Thread(target=scheduler.run_pass)
        thread.start()
        self.assertTrue(blocked.wait(2))
        self.assertTrue(quick.wait(2))
        self.assertLessEqual(maximum, 2)
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_active_pass_coalesces_wake_burst_to_one_followup(self):
        self.save(recipe("demo"))
        entered = threading.Event()
        release = threading.Event()
        twice = threading.Event()
        calls = 0

        def callback(recipe_id, _lease):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(3)
            if calls == 2:
                twice.set()
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback))
        scheduler.start()
        self.assertTrue(entered.wait(2))
        for _index in range(20):
            scheduler.request()
        release.set()
        self.assertTrue(twice.wait(2))
        for _index in range(20):
            scheduler.request()
        threading.Event().wait(0.1)
        scheduler.stop()
        scheduler.join(3)
        self.assertEqual(calls, 2)

    def test_monotonic_periodic_deadline_starts_one_normal_future_pass(self):
        self.save(recipe("demo"))
        monotonic = [0.0]
        completed = threading.Event()
        calls = 0

        def callback(recipe_id, _lease):
            nonlocal calls
            calls += 1
            if calls == 2:
                completed.set()
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback), monotonic=lambda: monotonic[0])
        scheduler.start()
        for _index in range(100):
            if calls == 1:
                break
            threading.Event().wait(0.01)
        self.assertEqual(calls, 1)
        monotonic[0] = 3601.0
        with scheduler._condition:
            scheduler._condition.notify_all()
        self.assertTrue(completed.wait(2))
        self.assertEqual(calls, 2)

    def test_shutdown_during_network_detection_prevents_late_claim(self):
        self.save(recipe("demo"))
        entered = threading.Event()
        release = threading.Event()
        base = AutomationDetectionService(self.recipes, self.ledger)

        def detector(_recipe, token=""):
            entered.set()
            release.wait(3)
            return {"identity": identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}

        scheduler = self.scheduler(InjectedDetectionService(base, detector))
        scheduler.start()
        self.assertTrue(entered.wait(2))
        scheduler.stop()
        release.set()
        scheduler.join(3)
        self.assertEqual(self.ledger.read()["attempts"], {})
        self.assertFalse(scheduler.is_alive())
        self.assertFalse(scheduler.status()["admission_open"])

    def test_shutdown_cancels_queued_checks_without_inflight_leaks(self):
        for name in ("one", "two", "three"):
            self.save(recipe(name))
        entered = threading.Event()
        release = threading.Event()

        def callback(recipe_id, _lease):
            entered.set()
            release.wait(3)
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback), concurrency=1)
        scheduler.start()
        self.assertTrue(entered.wait(2))
        scheduler.stop()
        release.set()
        scheduler.join(3)
        self.assertFalse(scheduler.is_alive())
        self.assertEqual(scheduler.status()["active_recipe_checks"], 0)

    def test_join_uses_one_shared_timeout_budget(self):
        monotonic = [10.0]
        scheduler = self.scheduler(FakeService(), monotonic=lambda: monotonic[0])
        observed = []

        class SlowThread:
            def join(self, timeout=None):
                observed.append(timeout)
                monotonic[0] += timeout or 0

            def is_alive(self):
                return True

        scheduler._thread = SlowThread()
        scheduler._inflight.add("demo")
        scheduler.join(2)
        self.assertEqual(observed, [2.0])
        self.assertEqual(scheduler._inflight, {"demo"})
        scheduler._inflight.clear()
        scheduler._thread = None

    def test_recipe_deleted_disabled_or_made_manual_during_detection_cannot_leave_a_claim(self):
        for change in ("delete", "disable", "manual"):
            with self.subTest(change=change):
                self.save(recipe("demo"))
                base = AutomationDetectionService(self.recipes, self.ledger)

                def detector(_recipe, token=""):
                    path = self.recipes / "demo.json"
                    if change == "delete":
                        path.unlink()
                    else:
                        changed = recipe("demo", enabled=change != "disable", policy="manual" if change == "manual" else "build")
                        recipe_store.save_recipe(path, changed)
                    return {"identity": identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}

                result = self.scheduler(InjectedDetectionService(base, detector)).run_pass()[0]
                self.assertEqual(result["classification"], "recipe_changed")
                self.assertEqual(self.ledger.read()["attempts"], {})

    def test_network_detection_holds_no_mutation_lease_and_claim_holds_it_briefly(self):
        self.save(recipe("demo"))
        application_gate = MutationGate()
        observed = []

        class ObservedLedger(AutomationLedger):
            def claim_current_recipe(inner_self, *args, **kwargs):
                observed.append(("claim", application_gate.active))
                return super().claim_current_recipe(*args, **kwargs)

        ledger = ObservedLedger(self.data)
        base = AutomationDetectionService(self.recipes, ledger)

        def detector(_recipe, token=""):
            observed.append(("network", application_gate.active))
            return {"identity": identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}

        scheduler = AutomationScheduler(
            self.recipes, InjectedDetectionService(base, detector), ledger, self.retry,
            mutation_gate=application_gate, interval_seconds=3600,
        )
        self.schedulers.append(scheduler)
        scheduler.run_pass()
        self.assertEqual(observed, [("network", 0), ("claim", 1)])

    def test_scheduled_and_internal_checks_race_to_one_durable_claim(self):
        self.save(recipe("demo"))
        barrier = threading.Barrier(2)
        base = AutomationDetectionService(self.recipes, self.ledger)

        def detector(_recipe, token=""):
            barrier.wait(3)
            return {"identity": identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}

        scheduler = self.scheduler(InjectedDetectionService(base, detector))
        scheduled = []
        thread = threading.Thread(target=lambda: scheduled.extend(scheduler.run_pass()))
        thread.start()
        internal = base.check("demo", detector=detector)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.ledger.read()["attempts"]), 1)
        self.assertEqual({internal["change"], scheduled[0]["change"]}, {"new", "existing"})

    def test_transient_retry_is_persisted_bounded_jittered_and_restart_safe(self):
        now = [2_000_000_000.0]
        self.save(recipe("demo"))
        failure = {
            **success("demo"), "identity": None, "classification": "rate_limited",
            "change": "none", "diagnostic": "github_rate_limited",
            "retry_after_seconds": 120, "rate_limit_reset": int(now[0] + 180),
        }
        failure["recipe_sha256"] = canonical_recipe_sha256(recipe("demo"))
        first_service = FakeService(lambda *_args: failure)
        first = self.scheduler(first_service, wall_clock=lambda: now[0])
        first.run_pass()
        row = self.retry.read()["recipes"]["demo"]
        delay = datetime.fromisoformat(row["not_before"]).timestamp() - now[0]
        self.assertGreaterEqual(delay, 180)
        self.assertLessEqual(delay, MAX_BACKOFF_SECONDS)
        self.assertEqual(row["retry_count"], 1)

        first.stop()
        restarted_service = FakeService()
        restarted = self.scheduler(restarted_service, wall_clock=lambda: now[0])
        restarted.run_pass()
        self.assertEqual(restarted_service.calls, [])
        now[0] += delay + 1
        restarted.run_pass()
        self.assertEqual(restarted_service.calls, ["demo"])
        self.assertNotIn("demo", self.retry.read()["recipes"])

    def test_overdue_retry_cannot_spin_when_it_cannot_be_consumed(self):
        now = 2_000_000_000.0
        configured = recipe("demo")
        recipe_sha = canonical_recipe_sha256(configured)
        retry_row = {
            "recipe_id": "demo", "recipe_sha256": recipe_sha,
            "retry_class": "transient", "classification": "upstream_unavailable",
            "diagnostic": "github_unavailable", "retry_count": 1,
            "not_before": datetime.fromtimestamp(now - 1, timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(now - 2, timezone.utc).isoformat(),
        }
        cases = ("deleted", "admission_blocked", "ledger_capacity")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                recipes = root / "recipes"
                recipes.mkdir()
                data = root / "data"
                retry = AutomationRetryStore(data, wall_clock=lambda: now)
                retry.record(retry_row)
                ledger = AutomationLedger(data, max_attempts=1)
                admission = lambda: case != "admission_blocked"
                if case != "deleted":
                    recipe_store.save_recipe(recipes / "demo.json", configured)
                if case == "ledger_capacity":
                    ledger.claim("other", identity(), "b" * 64, "build")
                scheduler = AutomationScheduler(
                    recipes, FakeService(), ledger, retry, admission_open=admission,
                    interval_seconds=3600, wall_clock=lambda: now,
                )
                self.schedulers.append(scheduler)
                passes = 0
                first = threading.Event()
                original = scheduler.run_pass

                def counted(*args, **kwargs):
                    nonlocal passes
                    passes += 1
                    result = original(*args, **kwargs)
                    first.set()
                    return result

                scheduler.run_pass = counted
                scheduler.start()
                self.assertTrue(first.wait(2))
                threading.Event().wait(0.05)
                self.assertEqual(passes, 1)
                self.assertIn("demo", retry.read()["recipes"])
                scheduler.stop()
                scheduler.join(2)

    def test_submit_rejection_rolls_back_recipe_inflight_ownership(self):
        self.save(recipe("demo"))
        scheduler = self.scheduler(FakeService())
        scheduler._executor.shutdown(wait=True, cancel_futures=True)
        self.assertEqual(scheduler.run_pass(), [])
        self.assertEqual(scheduler.status()["active_recipe_checks"], 0)
        self.assertFalse(scheduler.is_alive())
        self.assertEqual(scheduler.status()["global_blocker"]["code"], "automation_scheduler_internal_error")

    def test_backoff_is_deterministic_clamped_and_counter_is_finite(self):
        self.save(recipe("demo"))
        now = [2_000_000_000.0]
        scheduler = self.scheduler(FakeService(), wall_clock=lambda: now[0])
        result = {**success("demo"), "retry_after_seconds": 10**9, "rate_limit_reset": int(now[0] + 10**9)}
        self.assertEqual(scheduler._retry_delay("demo", 8, result), MAX_BACKOFF_SECONDS)
        self.assertEqual(scheduler._retry_delay("demo", 3, success("demo")), scheduler._retry_delay("demo", 3, success("demo")))
        failure = {
            **success("demo"), "identity": None, "classification": "upstream_unavailable",
            "diagnostic": "github_unavailable", "recipe_sha256": canonical_recipe_sha256(recipe("demo")),
        }
        with self.assertLogs("debbuilder.automation_scheduler", level="INFO"):
            for _index in range(12):
                previous = self.retry.read()["recipes"].get("demo")
                scheduler._record_outcome(failure, previous)
                now[0] += MAX_BACKOFF_SECONDS + 1
        self.assertEqual(self.retry.read()["recipes"]["demo"]["retry_count"], 8)

    def test_operator_failure_waits_for_normal_cycle_without_hot_retry(self):
        self.save(recipe("demo"))
        result = {**success("demo"), "identity": None, "classification": "ambiguous_asset", "diagnostic": "ambiguous_release_asset"}
        scheduler = self.scheduler(FakeService(lambda *_args: result))
        scheduler.run_pass()
        row = self.retry.read()["recipes"]["demo"]
        self.assertEqual(row["retry_class"], "operator")
        self.assertIsNone(row["not_before"])

    def test_malformed_future_state_and_ledger_capacity_block_without_network(self):
        self.save(recipe("demo"))
        for filename, document, expected in (
            ("automation-scheduler.json", {"schema_version": 99, "recipes": {}}, "automation_scheduler_state_future_version"),
            ("automation-ledger.json", {"schema_version": 99, "attempts": {}}, "automation_ledger_future_version"),
        ):
            with self.subTest(filename=filename):
                isolated = self.root / filename.replace(".json", "")
                isolated.mkdir()
                (isolated / filename).write_text(json.dumps(document))
                service = FakeService()
                ledger = AutomationLedger(isolated, max_attempts=1)
                retry = AutomationRetryStore(isolated)
                scheduler = AutomationScheduler(self.recipes, service, ledger, retry, interval_seconds=3600)
                self.schedulers.append(scheduler)
                self.assertEqual(scheduler.run_pass(), [])
                self.assertEqual(scheduler.status()["global_blocker"]["code"], expected)
                self.assertEqual(service.calls, [])

    def test_orchestrator_malformed_ledger_keeps_the_exact_fail_closed_blocker(self):
        self.save(recipe("demo"))
        service = FakeService()
        orchestrator = mock.Mock()
        orchestrator.advance_all.side_effect = AutomationLedgerError(
            "automation_ledger_malformed", "Automation ledger is malformed",
        )
        scheduler = self.scheduler(service, orchestrator=orchestrator)

        self.assertEqual(scheduler.run_pass(), [])
        self.assertEqual(
            scheduler.status()["global_blocker"]["code"],
            "automation_ledger_malformed",
        )
        self.assertEqual(service.calls, [])

    def test_global_admission_and_disabled_scheduler_skip_everything(self):
        self.save(recipe("demo"))
        for enabled, admission in ((False, lambda: True), (True, lambda: False)):
            with self.subTest(enabled=enabled):
                service = FakeService()
                scheduler = self.scheduler(service, enabled=enabled, admission_open=admission)
                self.assertEqual(scheduler.run_pass(), [])
                self.assertEqual(service.calls, [])

    def test_disabled_detection_still_reconciles_durable_lifecycle(self):
        service = FakeService()
        orchestrator = mock.Mock()
        orchestrator.next_retry_delay.return_value = None
        scheduler = self.scheduler(service, enabled=False, orchestrator=orchestrator)
        self.assertEqual(scheduler.run_pass(), [])
        orchestrator.advance_all.assert_called_once_with()
        self.assertEqual(service.calls, [])

        reconciled = threading.Event()
        orchestrator.advance_all.side_effect = reconciled.set
        scheduler.start()
        self.assertTrue(reconciled.wait(2))
        scheduler.stop()
        scheduler.join(2)
        self.assertEqual(service.calls, [])

    def test_worker_exception_isolated_and_recorded_without_killing_other_checks(self):
        self.save(recipe("bad"))
        self.save(recipe("good"))

        def callback(recipe_id, _lease):
            if recipe_id == "bad":
                raise RuntimeError("private /tmp/path")
            return success(recipe_id)

        scheduler = self.scheduler(FakeService(callback))
        with self.assertLogs("debbuilder.automation_scheduler", level="ERROR") as logs:
            results = scheduler.run_pass()
        self.assertEqual({row["recipe_id"] for row in results}, {"bad", "good"})
        self.assertNotIn("/tmp/path", json.dumps(self.retry.read()))
        self.assertTrue(any("bad" in line for line in logs.output))
        self.assertNotIn("/tmp/path", "\n".join(logs.output))

    def test_ledger_capacity_blocks_checks_without_pruning_or_run_creation(self):
        self.save(recipe("demo"))
        limited = AutomationLedger(self.data, max_attempts=1)
        limited.claim("other", identity(), "a" * 64, "build")
        service = FakeService()
        scheduler = AutomationScheduler(self.recipes, service, limited, self.retry, interval_seconds=3600)
        self.schedulers.append(scheduler)
        self.assertEqual(scheduler.run_pass(), [])
        self.assertEqual(scheduler.status()["global_blocker"]["code"], "automation_ledger_capacity_exhausted")
        self.assertEqual(service.calls, [])
        self.assertEqual(len(limited.read()["attempts"]), 1)


class RetryStoreTests(unittest.TestCase):
    def test_read_is_non_mutating_and_malformed_or_future_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = AutomationRetryStore(root)
            self.assertEqual(store.read()["recipes"], {})
            self.assertFalse(store.path.exists())
            for document, code in (
                ({"schema_version": 2, "recipes": {}}, "automation_scheduler_state_future_version"),
                ({"schema_version": 1, "recipes": []}, "automation_scheduler_state_capacity"),
            ):
                store.path.write_text(json.dumps(document))
                with self.assertRaises(Exception) as caught:
                    store.read()
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
