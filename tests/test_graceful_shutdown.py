import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from debbuilder import build_pipeline
from debbuilder.build_store import BuildStore
from debbuilder.execution_cancellation import SERVER_SHUTDOWN, USER_REQUESTED
from debbuilder.execution_manager import ExecutionManager, ExecutionManagerError


def recipe(name: str) -> dict:
    return {
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Demo <demo@example.test>",
            "description": "Demo package",
        },
        "source": {"repository": f"owner/{name}"},
    }


class GracefulShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.managers = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for manager in self.managers:
            manager.stop(timeout=5)

    def create(self, name: str) -> dict:
        return build_pipeline.create_pipeline_run(recipe(name), store=self.store, dry_run=False)

    def manager(self, execute) -> ExecutionManager:
        manager = ExecutionManager(self.store, execute=execute)
        self.managers.append(manager)
        return manager

    def cancellable_executor(self):
        started = threading.Event()
        finished = threading.Event()
        observations = []

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            self.assertTrue(cancellation_control.event.wait(3))
            observations.append(cancellation_control.request)
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "cancelled"
                run["cancellation"] = dict(cancellation_control.request or {})
                store.save(run)
            finished.set()

        return execute, started, finished, observations

    def test_shutdown_closes_admission_cancels_active_and_all_queued_runs(self):
        execute, started, finished, observations = self.cancellable_executor()
        manager = self.manager(execute)
        runs = [self.create(f"shutdown-{index}") for index in range(4)]
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(started.wait(2))
        for run in runs[1:]:
            manager.submit(run["id"])

        result = manager.shutdown(timeout=3)

        self.assertTrue(result["complete"])
        self.assertTrue(result["admission_closed"])
        self.assertTrue(result["worker_joined"])
        self.assertEqual(result["queued_run_ids"], [run["id"] for run in runs[1:]])
        self.assertEqual(result["cancelled_run_ids"], [run["id"] for run in runs[1:]])
        self.assertEqual(observations[0]["reason"], SERVER_SHUTDOWN)
        self.assertTrue(finished.is_set())
        for run in runs[1:]:
            persisted = self.store.load(run["id"])
            self.assertEqual(persisted["status"], "cancelled")
            self.assertEqual(persisted["cancellation"]["reason"], SERVER_SHUTDOWN)
            self.assertEqual(persisted["cancellation"]["phase"], "queue")
            self.assertIsNone(persisted["started_at"])
        with self.assertRaises(ExecutionManagerError) as refused:
            manager.submit(self.create("after-shutdown")["id"])
        self.assertEqual(refused.exception.code, "execution_manager_not_accepting")

    def test_incomplete_target_retains_non_daemon_worker_ownership_until_completion(self):
        release = threading.Event()
        self.releases.append(release)
        started = threading.Event()

        def execute(_run_id, **_kwargs):
            started.set()
            release.wait(3)

        manager = self.manager(execute)
        run = self.create("owned-after-incomplete-target")
        manager.start()
        self.assertFalse(manager.worker.daemon)
        manager.submit(run["id"])
        self.assertTrue(started.wait(2))

        result = manager.shutdown(timeout=0)

        self.assertFalse(result["complete"])
        self.assertFalse(result["worker_joined"])
        continuation = {}
        waiter = threading.Thread(target=lambda: continuation.update(manager.shutdown(timeout=None)))
        waiter.start()
        self.assertTrue(manager.worker.is_alive())
        self.assertEqual(continuation, {})
        release.set()
        waiter.join(3)
        self.assertTrue(continuation["complete"])
        self.assertFalse(manager.worker.is_alive())

    def test_shutdown_persistence_does_not_hold_manager_condition(self):
        active_execute, active_started, _finished, _observations = self.cancellable_executor()
        manager = self.manager(active_execute)
        active = self.create("condition-active")
        queued = self.create("condition-queued")
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(queued["id"])
        persistence_entered = threading.Event()
        release_persistence = threading.Event()
        self.releases.append(release_persistence)
        original = manager._cancel_shutdown_queued_run

        def blocked_persistence(run_id):
            self.assertFalse(manager.accepting)
            persistence_entered.set()
            self.assertTrue(release_persistence.wait(3))
            original(run_id)

        shutdown_result = {}
        with mock.patch.object(manager, "_cancel_shutdown_queued_run", side_effect=blocked_persistence):
            shutdown_thread = threading.Thread(target=lambda: shutdown_result.update(manager.shutdown(timeout=4)))
            shutdown_thread.start()
            self.assertTrue(persistence_entered.wait(2))
            condition_observed = threading.Event()

            def observe_condition():
                with manager._condition:
                    condition_observed.set()

            observer = threading.Thread(target=observe_condition)
            observer.start()
            self.assertTrue(condition_observed.wait(2))
            release_persistence.set()
            observer.join(2)
            shutdown_thread.join(4)

        self.assertTrue(shutdown_result["complete"])

    def test_shutdown_waits_for_preexisting_unused_reservation_release(self):
        manager = self.manager(lambda *_args, **_kwargs: None)
        manager.start()
        reservation_context = manager.reserve()
        reservation = reservation_context.__enter__()

        result = manager.shutdown(timeout=0)

        self.assertFalse(result["complete"])
        self.assertEqual(result["outstanding_reservations"], 1)
        reservation_context.__exit__(None, None, None)
        completed = manager.shutdown(timeout=None)
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["outstanding_reservations"], 0)

    def test_pre_shutdown_reservation_may_create_submit_and_become_shutdown_owned(self):
        executed = []
        manager = self.manager(lambda run_id, **_kwargs: executed.append(run_id))
        manager.start()
        reservation_context = manager.reserve()
        reservation = reservation_context.__enter__()
        result = {}
        shutdown_thread = threading.Thread(target=lambda: result.update(manager.shutdown(timeout=4)))
        shutdown_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: manager._shutdown_started, timeout=2))
        self.assertTrue(shutdown_thread.is_alive())
        self.assertEqual(result, {})

        run = self.create("reserved-creation-during-shutdown")
        reservation.submit(run["id"])
        reservation_context.__exit__(None, None, None)
        shutdown_thread.join(4)

        self.assertTrue(result["complete"])
        self.assertEqual(result["outstanding_reservations"], 0)
        self.assertEqual(result["submitting_run_ids"], [])
        self.assertEqual(result["queued_run_ids"], [run["id"]])
        self.assertEqual(result["cancelled_run_ids"], [run["id"]])
        self.assertEqual(executed, [])
        persisted = self.store.load(run["id"])
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation"]["reason"], SERVER_SHUTDOWN)

    def test_submission_failure_keeps_admission_lease_through_terminalization(self):
        manager = self.manager(lambda *_args, **_kwargs: None)
        manager.start()
        lease_acquired = threading.Event()
        allow_submission = threading.Event()
        finalization_entered = threading.Event()
        release_finalization = threading.Event()
        request_errors = []
        created = {}

        def admitted_request():
            try:
                with manager.reserve() as reservation:
                    run = self.create("failed-submission-during-shutdown")
                    created.update(run)
                    lease_acquired.set()
                    self.assertTrue(allow_submission.wait(3))
                    try:
                        reservation.submit(run["id"])
                    except OSError:
                        finalization_entered.set()
                        self.assertTrue(release_finalization.wait(3))
                        with self.store.locked_run(run["id"]):
                            persisted = self.store.load(run["id"])
                            persisted["status"] = "failed"
                            self.store.save(persisted)
            except BaseException as exc:
                request_errors.append(exc)

        request_thread = threading.Thread(target=admitted_request)
        with mock.patch.object(manager, "_mark_queued", side_effect=OSError("storage unavailable")):
            request_thread.start()
            self.assertTrue(lease_acquired.wait(2))
            shutdown_result = {}
            shutdown_thread = threading.Thread(
                target=lambda: shutdown_result.update(manager.shutdown(timeout=4)),
            )
            shutdown_thread.start()
            with manager._condition:
                self.assertTrue(manager._condition.wait_for(lambda: manager._shutdown_started, timeout=2))
            allow_submission.set()
            self.assertTrue(finalization_entered.wait(2))
            self.assertTrue(shutdown_thread.is_alive())
            self.assertEqual(shutdown_result, {})
            release_finalization.set()
            request_thread.join(3)
            shutdown_thread.join(4)

        self.assertEqual(request_errors, [])
        self.assertTrue(shutdown_result["complete"])
        self.assertEqual(shutdown_result["outstanding_reservations"], 0)
        self.assertEqual(self.store.load(created["id"])["status"], "failed")

    def test_submission_persisting_during_shutdown_is_captured_and_never_executes(self):
        executed = []
        manager = self.manager(lambda run_id, **_kwargs: executed.append(run_id))
        manager.start()
        run = self.create("concurrent-submission")
        reservation_context = manager.reserve()
        reservation = reservation_context.__enter__()
        transition_entered = threading.Event()
        release_transition = threading.Event()
        self.releases.append(release_transition)
        original_transition = self.store.transition_status

        def delayed_transition(run_id, *, expected, status):
            transition_entered.set()
            self.assertTrue(release_transition.wait(3))
            return original_transition(run_id, expected=expected, status=status)

        self.store.transition_status = delayed_transition
        submit_errors = []
        submitter = threading.Thread(target=lambda: self._submit(reservation, run["id"], submit_errors))
        submitter.start()
        self.assertTrue(transition_entered.wait(2))
        shutdown_result = {}
        shutdown_thread = threading.Thread(target=lambda: shutdown_result.update(manager.shutdown(timeout=4)))
        shutdown_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: not manager._accepting, timeout=2))
        release_transition.set()
        submitter.join(3)
        reservation_context.__exit__(None, None, None)
        shutdown_thread.join(4)

        self.assertEqual(submit_errors, [])
        self.assertTrue(shutdown_result["complete"])
        self.assertEqual(shutdown_result["queued_run_ids"], [run["id"]])
        self.assertEqual(executed, [])
        self.assertEqual(self.store.load(run["id"])["cancellation"]["reason"], SERVER_SHUTDOWN)

    @staticmethod
    def _submit(manager, run_id, errors):
        try:
            manager.submit(run_id)
        except Exception as exc:
            errors.append(exc)

    def test_user_cancellation_reason_wins_before_shutdown(self):
        execute, started, _finished, observations = self.cancellable_executor()
        manager = self.manager(execute)
        run = self.create("user-first")
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(started.wait(2))

        cancellation = manager.cancel(run["id"])
        result = manager.shutdown(timeout=3)

        self.assertEqual(cancellation["cancellation"]["reason"], USER_REQUESTED)
        self.assertEqual(result["active_cancellation"]["cancellation"]["reason"], USER_REQUESTED)
        self.assertEqual(observations[0]["reason"], USER_REQUESTED)

    def test_shutdown_reason_wins_before_later_direct_cancellation(self):
        started = threading.Event()
        release = threading.Event()
        self.releases.append(release)
        observed = []

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            self.assertTrue(cancellation_control.event.wait(3))
            observed.append(cancellation_control.request)
            self.assertTrue(release.wait(3))

        manager = self.manager(execute)
        run = self.create("shutdown-first")
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(started.wait(2))
        result_holder = {}
        shutdown_thread = threading.Thread(target=lambda: result_holder.update(manager.shutdown(timeout=4)))
        shutdown_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: manager._shutdown_started, timeout=2))

        later = manager.cancel(run["id"])
        release.set()
        shutdown_thread.join(4)

        self.assertEqual(later["cancellation"]["reason"], SERVER_SHUTDOWN)
        self.assertEqual(observed[0]["reason"], SERVER_SHUTDOWN)
        self.assertTrue(result_holder["complete"])

    def test_normal_terminal_ownership_remains_authoritative(self):
        terminal_claimed = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            self.assertTrue(cancellation_control.claim_terminal())
            terminal_claimed.set()
            self.assertTrue(release.wait(3))
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "success"
                store.save(run)

        manager = self.manager(execute)
        run = self.create("terminal-first")
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(terminal_claimed.wait(2))
        result_holder = {}
        shutdown_thread = threading.Thread(target=lambda: result_holder.update(manager.shutdown(timeout=4)))
        shutdown_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: manager._shutdown_started, timeout=2))
        release.set()
        shutdown_thread.join(4)

        cancellation = result_holder["active_cancellation"]
        self.assertFalse(cancellation["accepted"])
        self.assertEqual(cancellation["owner"], "terminal_owned")
        self.assertIsNone(cancellation.get("cancellation"))
        self.assertEqual(self.store.load(run["id"])["status"], "success")

    def test_persistence_failure_is_incomplete_and_retried_idempotently(self):
        execute, started, _finished, _observations = self.cancellable_executor()
        manager = self.manager(execute)
        active = self.create("failure-active")
        queued = self.create("failure-queued")
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(started.wait(2))
        manager.submit(queued["id"])
        original = manager._cancel_shutdown_queued_run

        with mock.patch.object(manager, "_cancel_shutdown_queued_run", side_effect=OSError("denied")):
            first = manager.shutdown(timeout=3)

        self.assertFalse(first["complete"])
        self.assertTrue(first["worker_joined"])
        self.assertEqual(first["unresolved_run_ids"], [queued["id"]])
        self.assertEqual(self.store.load(queued["id"])["status"], "queued")

        with mock.patch.object(manager, "_cancel_shutdown_queued_run", side_effect=original) as retried:
            second = manager.shutdown(timeout=None)
        self.assertTrue(second["complete"])
        self.assertEqual(second["cancelled_run_ids"], [queued["id"]])
        retried.assert_called_once_with(queued["id"])

        repeated = manager.shutdown(timeout=2)
        self.assertEqual(repeated["status"], "complete")
        self.assertEqual(repeated["cancelled_run_ids"], [queued["id"]])

    def test_retry_recognizes_a_queued_cancellation_persisted_before_save_error(self):
        execute, started, _finished, _observations = self.cancellable_executor()
        manager = self.manager(execute)
        active = self.create("partial-save-active")
        queued = self.create("partial-save-queued")
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(started.wait(2))
        manager.submit(queued["id"])
        original_save = self.store.save
        raised = False

        def save_then_raise(run):
            nonlocal raised
            original_save(run)
            if run["id"] == queued["id"] and run["status"] == "cancelled" and not raised:
                raised = True
                raise OSError("post-commit metadata failure")

        with mock.patch.object(self.store, "save", side_effect=save_then_raise):
            first = manager.shutdown(timeout=3)

        self.assertFalse(first["complete"])
        self.assertEqual(self.store.load(queued["id"])["status"], "cancelled")
        second = manager.shutdown(timeout=2)
        self.assertTrue(second["complete"])
        self.assertEqual(second["cancelled_run_ids"], [queued["id"]])

    def test_unresolved_active_persistence_keeps_explicit_ownership_until_retry(self):
        executed = []
        entered = threading.Event()

        def execute(run_id, *, store, expected_initial_status, **_kwargs):
            executed.append(run_id)
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            entered.set()
            raise RuntimeError("callback failed")

        manager = self.manager(execute)
        run = self.create("active-terminal-persistence-failure")
        original_save = self.store.save

        def fail_terminal_save(value):
            if value["id"] == run["id"] and value["status"] == "failed":
                raise OSError("storage unavailable")
            return original_save(value)

        manager.start()
        with mock.patch.object(self.store, "save", side_effect=fail_terminal_save):
            manager.submit(run["id"])
            self.assertTrue(entered.wait(2))
            manager.worker.join(2)
            self.assertFalse(manager.worker.is_alive())
            self.assertEqual(self.store.load(run["id"])["status"], "running")
            first = manager.shutdown(timeout=0)

        self.assertFalse(first["complete"])
        self.assertEqual(first["unresolved_active_run_ids"], [run["id"]])
        self.assertEqual(first["errors"][0]["ownership"], "active")
        self.assertEqual(executed, [run["id"]])

        second = manager.shutdown(timeout=2)
        self.assertTrue(second["complete"])
        self.assertEqual(second["unresolved_active_run_ids"], [])
        self.assertEqual(self.store.load(run["id"])["status"], "failed")
        self.assertEqual(executed, [run["id"]])

    def test_baseexception_in_worker_finalizer_preserves_unresolved_active_ownership(self):
        class FatalFinalization(BaseException):
            pass

        executed = []
        entered = threading.Event()

        def execute(run_id, *, store, expected_initial_status, **_kwargs):
            executed.append(run_id)
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            entered.set()
            raise RuntimeError("callback failed")

        manager = self.manager(execute)
        run = self.create("baseexception-finalizer")
        original_save = self.store.save

        def abort_terminal_save(value):
            if value["id"] == run["id"] and value["status"] == "failed":
                raise FatalFinalization("fatal terminal persistence interruption")
            return original_save(value)

        manager.start()
        with mock.patch.object(threading, "excepthook") as thread_error, \
                mock.patch.object(self.store, "save", side_effect=abort_terminal_save):
            manager.submit(run["id"])
            self.assertTrue(entered.wait(2))
            manager.worker.join(2)
            self.assertFalse(manager.worker.is_alive())
            self.assertEqual(self.store.load(run["id"])["status"], "running")
            first = manager.shutdown(timeout=0)

        self.assertTrue(thread_error.called)
        self.assertFalse(first["complete"])
        self.assertTrue(first["worker_joined"])
        self.assertEqual(first["unresolved_active_run_ids"], [run["id"]])
        self.assertEqual(first["active_context_run_id"], None)
        self.assertEqual(first["worker_fatal_error"]["exception_type"], "FatalFinalization")
        self.assertEqual(executed, [run["id"]])

        second = manager.shutdown(timeout=2)
        self.assertFalse(second["complete"])
        self.assertEqual(second["unresolved_active_run_ids"], [])
        self.assertEqual(self.store.load(run["id"])["status"], "failed")
        self.assertEqual(second["worker_fatal_error"]["exception_type"], "FatalFinalization")
        self.assertEqual(executed, [run["id"]])

    def test_post_commit_baseexception_releases_active_run_but_not_fatal_status(self):
        class FatalPostCommit(BaseException):
            pass

        executed = []
        entered = threading.Event()

        def execute(run_id, *, store, expected_initial_status, **_kwargs):
            executed.append(run_id)
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            entered.set()
            raise RuntimeError("callback failed")

        manager = self.manager(execute)
        run = self.create("baseexception-after-terminal-commit")
        original_save = self.store.save
        raised = False

        def save_then_abort(value):
            nonlocal raised
            original_save(value)
            if value["id"] == run["id"] and value["status"] == "failed" and not raised:
                raised = True
                raise FatalPostCommit("fatal post-commit interruption")

        manager.start()
        with mock.patch.object(threading, "excepthook") as thread_error, \
                mock.patch.object(self.store, "save", side_effect=save_then_abort):
            manager.submit(run["id"])
            self.assertTrue(entered.wait(2))
            manager.worker.join(2)

        self.assertTrue(thread_error.called)
        self.assertFalse(manager.worker.is_alive())
        self.assertEqual(self.store.load(run["id"])["status"], "failed")
        result = manager.shutdown(timeout=2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unresolved_active_run_ids"], [])
        self.assertIsNone(result["active_context_run_id"])
        self.assertEqual(result["worker_fatal_error"]["exception_type"], "FatalPostCommit")
        self.assertEqual(executed, [run["id"]])

    def test_join_timeout_then_late_completion_is_safe(self):
        started = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            self.assertTrue(release.wait(3))
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "cancelled" if cancellation_control.event.is_set() else "success"
                store.save(run)

        manager = self.manager(execute)
        run = self.create("late-completion")
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(started.wait(2))

        first = manager.shutdown(timeout=0)

        self.assertFalse(first["complete"])
        self.assertTrue(first["timed_out"])
        self.assertFalse(first["worker_joined"])
        self.assertTrue(manager.worker.is_alive())
        release.set()
        second = manager.shutdown(timeout=3)
        self.assertTrue(second["complete"])
        self.assertTrue(second["worker_joined"])
        self.assertEqual(self.store.load(run["id"])["status"], "cancelled")

    def test_expired_deadline_leaves_queued_run_owned_for_later_retry(self):
        started = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            self.assertTrue(release.wait(3))
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "cancelled" if cancellation_control.event.is_set() else "success"
                store.save(run)

        manager = self.manager(execute)
        active = self.create("deadline-active")
        queued = self.create("deadline-queued")
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(started.wait(2))
        manager.submit(queued["id"])

        first = manager.shutdown(timeout=0)

        self.assertFalse(first["complete"])
        self.assertTrue(first["timed_out"])
        self.assertEqual(first["unresolved_run_ids"], [queued["id"]])
        self.assertEqual(self.store.load(queued["id"])["status"], "queued")
        release.set()
        second = manager.shutdown(timeout=3)
        self.assertTrue(second["complete"])
        self.assertEqual(self.store.load(queued["id"])["cancellation"]["reason"], SERVER_SHUTDOWN)

    def test_concurrent_repeated_shutdown_calls_converge(self):
        execute, started, _finished, _observations = self.cancellable_executor()
        manager = self.manager(execute)
        active = self.create("concurrent-active")
        queued = self.create("concurrent-queued")
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(started.wait(2))
        manager.submit(queued["id"])
        barrier = threading.Barrier(3)
        results = []
        result_lock = threading.Lock()

        def shutdown():
            barrier.wait()
            result = manager.shutdown(timeout=4)
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=shutdown) for _index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["complete"] for result in results))
        self.assertTrue(all(result["cancelled_run_ids"] == [queued["id"]] for result in results))


if __name__ == "__main__":
    unittest.main()
