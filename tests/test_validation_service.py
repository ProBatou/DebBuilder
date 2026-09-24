import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import dependency_preparation, validation_service
from debbuilder.build_models import utc_now
from debbuilder.build_store import BuildStore
from tests.validation_helpers import record_canonical_validation


IMAGE = {"name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None}


class ValidationServiceTests(unittest.TestCase):
    def setUp(self):
        dependency_preparation.SUPERVISOR.open_admission()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = BuildStore(self.root / "builds")
        self.managers = []
        self.run = self.make_run("validation-run")
        self.current = dependency_preparation.ArtifactMetadata(
            Path(self.run["artifact"]["path"]), "validation-demo", "1.0-1", "all", "", "", 10, "b" * 64,
        )
        self.patches = [
            mock.patch("debbuilder.validation_service.admitted_image", return_value=IMAGE),
            mock.patch("debbuilder.validation_service.dependency_preparation.inspect_artifact", return_value=self.current),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown(3)
        for patcher in reversed(self.patches):
            patcher.stop()
        dependency_preparation.SUPERVISOR.open_admission()
        self.temporary.cleanup()

    def make_run(self, run_id):
        run = self.store.create({"schema_version": 5, "name": "validation-demo", "package": {"name": "validation-demo"}}, mode="build", run_id=run_id)
        artifact = Path(run["workspace"]) / "artifacts" / f"{run_id}.deb"
        artifact.write_bytes(b"controlled")
        run["status"] = "success"
        run["artifact"] = {"path": str(artifact), "name": artifact.name, "inspection": {"package": "validation-demo", "version": "1.0-1"}}
        self.store.save(run)
        return run

    def manager(self, execute, *, capacity=8, on_terminal=None):
        manager = validation_service.ValidationManager(
            self.store,
            execute=execute,
            registry_root=self.root / "validation-containers",
            workspace_root=self.root,
            queue_capacity=capacity,
            on_terminal=on_terminal,
        )
        manager.start()
        self.managers.append(manager)
        return manager

    def test_admission_is_durable_before_worker_observes_it(self):
        observed = {}
        finished = threading.Event()

        def execute(run_id, attempt_id, _event, _automation):
            observed.update(validation_service.load_attempt(self.store, run_id, attempt_id))
            finished.set()

        manager = self.manager(execute)
        admitted = manager.admit(self.run["id"], {})
        self.assertTrue(finished.wait(2))
        self.assertEqual(observed["id"], admitted["attempt_id"])
        self.assertEqual(observed["status"], "queued")
        self.assertTrue((validation_service.attempt_root(self.store, self.run["id"], admitted["attempt_id"]) / "attempt.json").is_file())

    def test_invalid_admission_leaves_no_attempt(self):
        manager = self.manager(lambda *_args: None)
        with mock.patch(
            "debbuilder.validation_service.dependency_preparation.inspect_artifact",
            side_effect=dependency_preparation.DependencyPreparationError("artifact_metadata_invalid", "invalid artifact"),
        ):
            with self.assertRaises(validation_service.ValidationAdmissionError) as raised:
                manager.admit(self.run["id"], {})
        self.assertEqual(raised.exception.code, "artifact_metadata_invalid")
        attempts = self.store.run_dir(self.run["id"]) / "manifests" / "validation-attempts"
        self.assertEqual(list(attempts.iterdir()) if attempts.exists() else [], [])

    def test_duplicate_and_concurrent_submission_reuse_one_active_attempt(self):
        release = threading.Event()
        entered = threading.Event()

        def execute(_run_id, _attempt_id, event, _automation):
            entered.set()
            while not release.is_set() and not event.wait(0.01):
                pass

        manager = self.manager(execute)
        barrier = threading.Barrier(3)
        results = []

        def submit():
            barrier.wait()
            results.append(manager.admit(self.run["id"], {}))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(3)
        self.assertEqual(len(results), 2)
        self.assertEqual({row["attempt_id"] for row in results}, {results[0]["attempt_id"]})
        self.assertEqual(sorted(row["duplicate"] for row in results), [False, True])
        self.assertEqual(len(validation_service.list_attempts(self.store, self.run["id"])), 1)
        release.set()

    def test_exact_active_cancellation_and_repeated_cancellation(self):
        entered = threading.Event()

        def execute(run_id, attempt_id, event, _automation):
            with self.store.locked_run(run_id):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                attempt.update({"status": "running", "started_at": utc_now()})
                validation_service._save_attempt(
                    validation_service.attempt_root(self.store, run_id, attempt_id) / "attempt.json", attempt,
                )
            entered.set()
            event.wait(2)
            with self.store.locked_run(run_id):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                validation_service._save_attempt(
                    validation_service.attempt_root(self.store, run_id, attempt_id) / "attempt.json",
                    validation_service._terminal_cancelled(
                        attempt, code="validation_cancelled", message="Validation was cancelled by the user",
                    ),
                )

        manager = self.manager(execute)
        admitted = manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        first = manager.cancel(self.run["id"], admitted["attempt_id"])
        self.assertTrue(first["accepted"])
        self.assertIn(first["validation"]["status"], {"cancelling", "cancelled"})
        for _ in range(100):
            current = validation_service.load_attempt(self.store, self.run["id"], admitted["attempt_id"])
            if current["status"] == "cancelled":
                break
            threading.Event().wait(0.01)
        repeated = manager.cancel(self.run["id"], admitted["attempt_id"])
        self.assertEqual(repeated["validation"]["status"], "cancelled")
        self.assertEqual(self.store.load(self.run["id"])["status"], "success")

    def test_queued_cancellation_invokes_terminal_continuation_immediately(self):
        second = self.make_run("validation-run-queued-callback")
        entered = threading.Event()
        release = threading.Event()
        callbacks = []

        def execute(_run_id, _attempt_id, event, _automation):
            entered.set()
            while not release.is_set() and not event.wait(0.01):
                pass

        manager = self.manager(
            execute,
            on_terminal=lambda run_id, attempt_id: callbacks.append((run_id, attempt_id)),
        )
        manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        queued = manager.admit(second["id"], {})
        cancelled = manager.cancel(second["id"], queued["attempt_id"])
        self.assertEqual(cancelled["validation"]["status"], "cancelled")
        self.assertEqual(callbacks, [(second["id"], queued["attempt_id"])])
        release.set()

    def test_active_cancellation_does_not_wait_for_the_owned_run_lease(self):
        entered = threading.Event()
        released = threading.Event()

        def execute(run_id, attempt_id, event, _automation):
            path = validation_service.attempt_root(self.store, run_id, attempt_id) / "attempt.json"
            with self.store.locked_run(run_id):
                with validation_service.storage.locked_path(path):
                    attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                    attempt.update({"status": "running", "started_at": utc_now()})
                    validation_service._save_attempt(path, attempt)
                entered.set()
                event.wait(2)
                released.set()

        manager = self.manager(execute)
        admitted = manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        cancelled = manager.cancel(self.run["id"], admitted["attempt_id"])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertTrue(released.wait(1))
        self.assertEqual(cancelled["validation"]["status"], "cancelling")

    def test_status_projection_is_read_only_and_ignores_noncanonical_run_rows(self):
        release = threading.Event()
        manager = self.manager(lambda _run, _attempt, event, _automation: release.wait(2) or event.is_set())
        admitted = manager.admit(self.run["id"], {})
        path = validation_service.attempt_root(self.store, self.run["id"], admitted["attempt_id"]) / "attempt.json"
        before = path.read_bytes()
        observed_run = self.store.load(self.run["id"])
        observed_run["validations"] = [{
            "id": admitted["attempt_id"], "status": "running",
            "checks": [{"name": "package_install", "status": "failed", "error": "/tmp/debbuilder-validation/private"}],
            "commands": [{"command": "gpg --import", "stdout": "BEGIN PGP PRIVATE KEY"}],
            "backend": {"workspace": "/tmp/debbuilder-validation/private"},
        }]
        status = validation_service.public_attempt(
            validation_service.load_attempt(self.store, self.run["id"], admitted["attempt_id"]),
            run=observed_run, store=self.store,
        )
        self.assertEqual(status["attempt_id"], admitted["attempt_id"])
        serialized = json.dumps(status)
        self.assertNotIn("BEGIN PGP PRIVATE KEY", serialized)
        self.assertNotIn("/tmp/debbuilder-validation", serialized)
        self.assertNotIn("commands", status)
        self.assertNotIn("backend", status)
        self.assertEqual(path.read_bytes(), before)
        noncanonical = self.store.load(self.run["id"])
        noncanonical["validations"] = [{
            "id": "untrusted-validation", "status": "success", "artifact": noncanonical["artifact"]["path"],
            "commands": [{"stdout": "BEGIN PGP PRIVATE KEY"}],
            "backend": {"workspace": "/tmp/debbuilder-validation/private"},
        }]
        projected = validation_service.project_run(noncanonical, self.store)
        self.assertNotIn("validations", projected)
        self.assertNotIn("validations", projected["artifact"])
        self.assertNotIn("untrusted-validation", {row["id"] for row in projected["_validation_attempts"]})
        projected_serialized = json.dumps(projected["_validation_attempts"])
        self.assertNotIn("BEGIN PGP PRIVATE KEY", projected_serialized)
        self.assertNotIn("/tmp/debbuilder-validation", projected_serialized)
        release.set()

    def test_running_manifest_never_inherits_raw_lifecycle_error(self):
        manager = self.manager(lambda *_args: None)
        admitted = manager.admit(self.run["id"], {})
        attempt = validation_service.load_attempt(self.store, self.run["id"], admitted["attempt_id"])
        attempt["status"] = "running"
        attempt["started_at"] = utc_now()
        validation_service._save_attempt(
            validation_service.attempt_root(self.store, self.run["id"], admitted["attempt_id"]) / "attempt.json",
            attempt,
        )
        run = self.store.load(self.run["id"])
        run["validations"] = [{
            "id": admitted["attempt_id"], "status": "failed",
            "error": {"code": "private", "message": "/tmp/private"},
        }]
        public = validation_service.project_run(run, self.store)["_validation_attempts"][-1]
        self.assertEqual(public["status"], "running")
        self.assertIsNone(public["error"])
        self.assertNotIn("/tmp/private", json.dumps(public))

    def test_unreadable_inventory_projects_an_operator_recovery_blocker(self):
        root = self.store.run_dir(self.run["id"]) / "manifests" / "validation-attempts" / "unsafe"
        root.mkdir(parents=True)
        (root / "attempt.json").write_text("not-json")
        projected = validation_service.project_run(self.store.load(self.run["id"]), self.store)
        validation = projected["_validation_attempts"][-1]
        self.assertEqual(validation["phase"], "recovery")
        self.assertEqual(validation["status"], "cancelling")
        self.assertEqual(validation["recovery_blocker"]["code"], "validation_attempt_recovery_unverifiable")

    def test_automatic_duplicate_upgrades_durable_publication_intent(self):
        release = threading.Event()
        entered = threading.Event()

        def execute(run_id, _attempt, event, _automation):
            with self.store.locked_run(run_id):
                entered.set()
                release.wait(2)
            return event.is_set()

        manager = self.manager(execute)
        manual = manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        automatic = manager.admit(
            self.run["id"], {}, automatic=True, publish_after_success=True,
        )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(automatic["duplicate"])
        self.assertEqual(automatic["attempt_id"], manual["attempt_id"])
        self.assertEqual(
            validation_service.load_automation(self.store, self.run["id"], manual["attempt_id"]),
            {"automatic": True, "publish_after_success": True, "publication_state": "pending"},
        )
        release.set()

    def test_unresolved_cleanup_blocks_admission_and_cancels_later_queue(self):
        second = self.make_run("validation-run-blocked-queue")
        entered = threading.Event()
        fail_cleanup = threading.Event()

        def execute(run_id, attempt_id, _event, _automation):
            path = validation_service.attempt_root(self.store, run_id, attempt_id) / "attempt.json"
            with validation_service.storage.locked_path(path):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                attempt.update({"status": "running", "started_at": utc_now()})
                validation_service._save_attempt(path, attempt)
            entered.set()
            fail_cleanup.wait(2)
            with validation_service.storage.locked_path(path):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                attempt["status"] = "cancelling"
                validation_service._save_attempt(path, attempt)
            dependency_preparation.SUPERVISOR.block("validation lifecycle cleanup is unresolved")

        manager = self.manager(execute)
        stuck = manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        queued = manager.admit(second["id"], {})
        fail_cleanup.set()
        for _ in range(200):
            queued_status = validation_service.load_attempt(
                self.store, second["id"], queued["attempt_id"],
            )["status"]
            if manager.blocker and queued_status == "cancelled":
                break
            threading.Event().wait(0.01)
        self.assertEqual(manager.blocker["code"], "validation_container_recovery_required")
        self.assertEqual(
            validation_service.load_attempt(self.store, second["id"], queued["attempt_id"])["status"],
            "cancelled",
        )
        stuck_public = validation_service.public_attempt(
            validation_service.load_attempt(self.store, self.run["id"], stuck["attempt_id"]),
            run=self.store.load(self.run["id"]), blocker=manager.blocker,
        )
        self.assertEqual(stuck_public["recovery_blocker"]["code"], "validation_container_recovery_required")
        with self.assertRaises(validation_service.ValidationAdmissionError) as raised:
            manager.admit(self.run["id"], {})
        self.assertEqual(raised.exception.status, 503)

    def test_future_attempt_contract_is_refused(self):
        root = validation_service.attempt_root(self.store, self.run["id"], "future-attempt")
        root.mkdir(parents=True)
        (root / "attempt.json").write_text(json.dumps({"contract_version": 999, "id": "future-attempt", "status": "queued"}))
        with self.assertRaises(validation_service.ValidationAdmissionError) as raised:
            validation_service.load_attempt(self.store, self.run["id"], "future-attempt")
        self.assertEqual(raised.exception.code, "future_contract_version")
        self.assertEqual(raised.exception.status, 409)

    def test_success_requires_matching_prepared_result_and_automation_manifests(self):
        mutations = {
            "missing-result": lambda root: (root / "result.json").unlink(),
            "malformed-result": lambda root: (root / "result.json").write_text("{}"),
            "mismatched-result": lambda root: validation_service.storage.save_json(
                root / "result.json",
                {
                    **validation_service.storage.load_json(root / "result.json", {}),
                    "attempt_id": "different-attempt",
                },
            ),
            "missing-prepared": lambda root: (root / "prepared.json").unlink(),
            "missing-automation": lambda root: (root / "automation.json").unlink(),
            "missing-publication-state": lambda root: validation_service.storage.save_json(
                root / "automation.json",
                {
                    key: value
                    for key, value in validation_service.storage.load_json(root / "automation.json", {}).items()
                    if key != "publication_state"
                },
            ),
        }
        expected = {
            "missing-result": "validation_result_missing",
            "malformed-result": "invalid_contract_version",
            "mismatched-result": "validation_result_identity_mismatch",
            "missing-prepared": "validation_prepared_missing",
            "missing-automation": "validation_automation_missing",
            "missing-publication-state": "validation_automation_unreadable",
        }
        for suffix, mutate in mutations.items():
            attempt_id = f"canonical-{suffix}"
            record_canonical_validation(
                self.store, self.run, attempt_id=attempt_id,
                artifact_identity=self.current.identity(),
            )
            root = validation_service.attempt_root(self.store, self.run["id"], attempt_id)
            mutate(root)
            with self.subTest(suffix=suffix), self.assertRaises(validation_service.ValidationAdmissionError) as raised:
                validation_service.load_attempt(self.store, self.run["id"], attempt_id)
            self.assertEqual(raised.exception.code, expected[suffix])

    def test_public_attempt_retains_bounded_preparation_diagnostics(self):
        attempt_id = "canonical-diagnostics"
        record_canonical_validation(
            self.store, self.run, attempt_id=attempt_id,
            artifact_identity=self.current.identity(),
        )
        root = validation_service.attempt_root(self.store, self.run["id"], attempt_id)
        prepared = validation_service.storage.load_json(root / "prepared.json", {})
        prepared["diagnostics"] = [{"code": "base_satisfied", "message": "two exact packages selected"}]
        validation_service.storage.save_json(root / "prepared.json", prepared)

        current = validation_service.load_attempt(self.store, self.run["id"], attempt_id)
        public = validation_service.public_attempt(
            current, run=self.store.load(self.run["id"]), store=self.store,
        )
        self.assertEqual(public["diagnostics"], prepared["diagnostics"])

    def test_worker_failure_is_terminal_and_terminal_cancellation_is_a_noop(self):
        manager = self.manager(lambda *_args: (_ for _ in ()).throw(RuntimeError("private host path /tmp/secret")))
        admitted = manager.admit(self.run["id"], {})
        for _ in range(200):
            attempt = validation_service.load_attempt(self.store, self.run["id"], admitted["attempt_id"])
            if attempt["status"] == "failed":
                break
            threading.Event().wait(0.01)
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["error"]["message"], "Validation execution failed unexpectedly")
        cancelled = manager.cancel(self.run["id"], admitted["attempt_id"])
        self.assertFalse(cancelled["accepted"])
        self.assertEqual(cancelled["validation"]["status"], "failed")

    def test_restart_requeues_a_durable_queued_attempt(self):
        manager = validation_service.ValidationManager(
            self.store,
            execute=lambda *_args: None,
            registry_root=self.root / "validation-containers",
            workspace_root=self.root,
        )
        with self.store.locked_run(self.run["id"]):
            attempt = manager._admit_locked(
                self.run["id"], self.store.load(self.run["id"]),
                profile_name="bookworm", previous_artifact="", automatic=False, publish_after_success=False,
            )
        observed = threading.Event()

        def execute(run_id, attempt_id, _event, _automation):
            self.assertEqual((run_id, attempt_id), (self.run["id"], attempt["id"]))
            observed.set()

        restarted = self.manager(execute)
        self.assertTrue(observed.wait(2))

    def test_restart_resumes_a_durable_pending_publication_intent(self):
        manager = validation_service.ValidationManager(
            self.store,
            execute=lambda *_args: None,
            registry_root=self.root / "validation-containers",
            workspace_root=self.root,
        )
        with self.store.locked_run(self.run["id"]):
            attempt = manager._admit_locked(
                self.run["id"], self.store.load(self.run["id"]),
                profile_name="bookworm", previous_artifact="", automatic=True, publish_after_success=True,
            )
        path = validation_service.attempt_root(self.store, self.run["id"], attempt["id"]) / "attempt.json"
        with validation_service.storage.locked_path(path):
            current = validation_service.load_attempt(self.store, self.run["id"], attempt["id"])
            prepared = {
                "contract_version": 1,
                "profile_name": current["inputs"]["profile"],
                "image": current["selected_profile"]["image"],
                "native_architecture": "amd64",
                "artifacts": {"current": current["inputs"]["artifact"], "previous": None},
                "repositories": [], "base_packages": [], "packages": [],
                "started_at": current["created_at"], "finished_at": utc_now(),
                "diagnostics": [], "enforcement": [],
            }
            root = path.parent
            lifecycle_started = prepared["finished_at"]
            lifecycle_finished = utc_now()
            validation_service.storage.save_json(root / "prepared.json", prepared)
            validation_service.storage.save_json(root / "result.json", {
                "contract_version": 1,
                "attempt_id": attempt["id"],
                "build_run_id": self.run["id"],
                "artifact": current["inputs"]["artifact"],
                "profile": current["selected_profile"],
                "status": "success",
                "started_at": lifecycle_started,
                "finished_at": lifecycle_finished,
                "checks": [{"name": name, "status": "success", "error": ""} for name in (
                    "lifecycle_network_disabled", "package_install", "package_status_installed",
                    "package_remove", "package_purge", "package_absent_after_purge",
                )],
                "execution": {
                    "network": "disabled", "network_verified": True,
                    "cleanup": {"status": "success", "absence_proved": True},
                },
                "commands": [], "error": None,
            })
            current.update({
                "status": "success", "started_at": current["created_at"], "finished_at": lifecycle_finished,
                "result": {"status": "success", "reference": "result.json"}, "error": None,
            })
            validation_service._save_attempt(path, current)
        resumed = []

        def resume(run_id, attempt_id):
            resumed.append((run_id, attempt_id))
            validation_service.complete_publication_intent(self.store, run_id, attempt_id)

        restarted = validation_service.ValidationManager(
            self.store,
            execute=lambda *_args: None,
            registry_root=self.root / "validation-containers",
            workspace_root=self.root,
            resume_publication=resume,
        )
        restarted.start()
        self.managers.append(restarted)
        self.assertEqual(resumed, [(self.run["id"], attempt["id"])])
        automation = validation_service.load_automation(self.store, self.run["id"], attempt["id"])
        self.assertEqual(automation["publication_state"], validation_service.PUBLICATION_COMPLETE)

    def test_shutdown_cancels_active_and_waiting_attempts(self):
        second = self.make_run("validation-run-two")
        entered = threading.Event()

        def execute(run_id, attempt_id, event, _automation):
            path = validation_service.attempt_root(self.store, run_id, attempt_id) / "attempt.json"
            with validation_service.storage.locked_path(path):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                attempt.update({"status": "running", "started_at": utc_now()})
                validation_service._save_attempt(path, attempt)
            entered.set()
            event.wait(2)
            with validation_service.storage.locked_path(path):
                attempt = validation_service.load_attempt(self.store, run_id, attempt_id)
                validation_service._save_attempt(
                    path,
                    validation_service._terminal_cancelled(
                        attempt, code="validation_shutdown_cancelled", message="Validation was cancelled during server shutdown",
                    ),
                )

        manager = self.manager(execute)
        active = manager.admit(self.run["id"], {})
        self.assertTrue(entered.wait(2))
        queued = manager.admit(second["id"], {})
        self.assertTrue(manager.shutdown(3))
        self.assertEqual(validation_service.load_attempt(self.store, self.run["id"], active["attempt_id"])["status"], "cancelled")
        self.assertEqual(validation_service.load_attempt(self.store, second["id"], queued["attempt_id"])["status"], "cancelled")
        with self.assertRaises(validation_service.ValidationAdmissionError) as raised:
            manager.admit(self.run["id"], {})
        self.assertEqual(raised.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
