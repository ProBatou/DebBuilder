import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from debbuilder import build_pipeline
from debbuilder.build_store import BuildStore
from debbuilder.execution_cancellation import CancellationControl
from debbuilder.execution_manager import ExecutionManager, ExecutionManagerError


def recipe(name="demo"):
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


class ExecutionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BuildStore(Path(self.temporary.name) / "builds")
        self.managers = []

    def manager(self, **kwargs):
        manager = ExecutionManager(self.store, **kwargs)
        self.managers.append(manager)
        return manager

    def tearDown(self):
        for manager in self.managers:
            manager.stop(timeout=5)

    def create(self, name, *, dry_run=False):
        return build_pipeline.create_pipeline_run(recipe(name), store=self.store, dry_run=dry_run)

    def finish(self, run_id, *, status="success"):
        self.store.transition_status(run_id, expected="queued", status="running")
        with self.store.locked_run(run_id):
            run = self.store.load(run_id)
            run["status"] = status
            self.store.save(run)

    def test_worker_lifecycle_is_explicit_and_non_daemon(self):
        manager = self.manager(execute=lambda *_args, **_kwargs: None)
        self.assertIsNone(manager.worker)
        self.assertFalse(manager.accepting)

        with self.assertLogs("debbuilder.execution_manager", level="INFO") as captured:
            manager.start()
            worker = manager.worker
            manager.start()
            self.assertIs(worker, manager.worker)
            self.assertTrue(worker.is_alive())
            self.assertFalse(worker.daemon)
            manager.stop(timeout=2)
        self.assertFalse(manager.worker.is_alive())
        self.assertTrue(any("Execution worker started" in row for row in captured.output))
        self.assertTrue(any("Execution worker stopped" in row for row in captured.output))

    def test_fifo_order_is_preserved(self):
        order = []
        first_started = threading.Event()
        release_first = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            order.append(run_id)
            if len(order) == 1:
                first_started.set()
                release_first.wait(2)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(name) for name in ("fifo-a", "fifo-b", "fifo-c")]
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(first_started.wait(2))
        manager.submit(runs[1]["id"])
        manager.submit(runs[2]["id"])
        release_first.set()
        manager.stop(timeout=3)

        self.assertEqual(order, [run["id"] for run in runs])

    def test_single_worker_never_runs_more_than_one_submission(self):
        state_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()
        active = 0
        maximum = 0

        def execute(run_id, **_kwargs):
            nonlocal active, maximum
            self.store.transition_status(run_id, expected="queued", status="running")
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            if not first_started.is_set():
                first_started.set()
                release_first.wait(2)
            with state_lock:
                active -= 1
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(f"single-{index}") for index in range(8)]
        manager = self.manager(execute=execute)
        manager.start()
        barrier = threading.Barrier(len(runs))

        def submit(run_id):
            barrier.wait()
            manager.submit(run_id)

        threads = [threading.Thread(target=submit, args=(run["id"],)) for run in runs]
        for thread in threads:
            thread.start()
        self.assertTrue(first_started.wait(2))
        for thread in threads:
            thread.join()
        self.assertEqual(maximum, 1)
        release_first.set()
        manager.stop(timeout=4)

        self.assertEqual(maximum, 1)
        self.assertTrue(all(self.store.load(run["id"])["status"] == "success" for run in runs))

    def test_capacity_allows_one_active_plus_eight_waiting(self):
        active_started = threading.Event()
        release_active = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not active_started.is_set():
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(f"capacity-{index}") for index in range(10)]
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(active_started.wait(2))
        self.assertEqual(manager.active_run_id, runs[0]["id"])
        for run in runs[1:9]:
            manager.submit(run["id"])

        with self.assertRaises(ExecutionManagerError) as full:
            manager.submit(runs[9]["id"])
        self.assertEqual(full.exception.code, "execution_queue_full")
        self.assertEqual(len(manager.queued_run_ids), 8)
        self.assertEqual(self.store.load(runs[9]["id"])["status"], "pending")
        release_active.set()
        manager.stop(timeout=4)
        self.assertIsNone(manager.active_run_id)

    def test_last_place_reservation_is_atomic_between_threads(self):
        active_started = threading.Event()
        release_active = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not active_started.is_set():
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(f"reserve-fill-{index}") for index in range(8)]
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(active_started.wait(2))
        for run in runs[1:]:
            manager.submit(run["id"])

        release_reservation = threading.Event()
        results = []
        results_changed = threading.Condition()
        barrier = threading.Barrier(5)

        def reserve_last_place():
            barrier.wait()
            try:
                with manager.reserve():
                    with results_changed:
                        results.append("reserved")
                        results_changed.notify_all()
                    release_reservation.wait(2)
            except ExecutionManagerError as exc:
                with results_changed:
                    results.append(exc.code)
                    results_changed.notify_all()

        threads = [threading.Thread(target=reserve_last_place) for _index in range(5)]
        for thread in threads:
            thread.start()
        with results_changed:
            self.assertTrue(results_changed.wait_for(lambda: len(results) == len(threads), timeout=2))
        self.assertEqual(results.count("reserved"), 1)
        self.assertEqual(results.count("execution_queue_full"), 4)
        release_reservation.set()
        for thread in threads:
            thread.join()
        release_active.set()
        manager.stop(timeout=4)

    def test_failed_creation_releases_reservation(self):
        manager = self.manager(queue_capacity=1, execute=lambda *_args, **_kwargs: None)
        manager.start()
        invalid = recipe("invalid")
        invalid["package"]["name"] = "INVALID PACKAGE"

        with self.assertRaises(ValueError):
            with manager.reserve():
                build_pipeline.create_pipeline_run(invalid, store=self.store, dry_run=False)

        with manager.reserve():
            pass
        manager.stop(timeout=2)

    def test_submit_persists_queued_before_worker_can_reach_it(self):
        active_started = threading.Event()
        release_active = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not active_started.is_set():
                active_started.set()
                release_active.wait(2)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        active = self.create("queued-active")
        queued = self.create("queued-persisted")
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(active_started.wait(2))

        submitted = manager.submit(queued["id"])

        self.assertEqual(submitted["status"], "queued")
        self.assertEqual(self.store.load(queued["id"])["status"], "queued")
        release_active.set()
        manager.stop(timeout=3)

    def test_cancel_sole_queued_run_persists_canonical_result_idempotently(self):
        active_started = threading.Event()
        release_active = threading.Event()
        self.addCleanup(release_active.set)
        executed = []

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            executed.append(run_id)
            if len(executed) == 1:
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        active = self.create("cancel-sole-active")
        queued = self.create("cancel-sole-queued")
        original_recipe_sha256 = queued["recipe_sha256"]
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(queued["id"])

        result = manager.cancel(queued["id"])
        persisted = self.store.load(queued["id"])
        repeated = manager.cancel(queued["id"])

        self.assertEqual(result["outcome"], "queued_cancelled")
        self.assertEqual(repeated["outcome"], "already_cancelled")
        self.assertEqual(repeated["cancellation"], result["cancellation"])
        self.assertEqual(manager.queued_run_ids, ())
        self.assertEqual(persisted["status"], "cancelled")
        self.assertIsNone(persisted["error"])
        self.assertIsNone(persisted["started_at"])
        self.assertEqual(persisted["finished_at"], persisted["cancellation"]["completed_at"])
        self.assertEqual(persisted["duration"], 0.0)
        self.assertEqual(persisted["recipe_sha256"], original_recipe_sha256)
        self.assertEqual(
            {key: persisted["cancellation"][key] for key in ("code", "reason", "phase", "stage")},
            {
                "code": "execution_cancelled",
                "reason": "user_requested",
                "phase": "queue",
                "stage": "queue",
            },
        )
        self.assertTrue(persisted["cancellation"]["requested_at"])
        self.assertTrue(persisted["cancellation"]["completed_at"])
        self.assertTrue(all(step["status"] == "pending" for step in persisted["steps"]))

        release_active.set()
        manager.stop(timeout=3)
        self.assertEqual(executed, [active["id"]])

    def test_cancelling_queue_head_middle_and_tail_preserves_fifo(self):
        for cancelled_index in range(3):
            with self.subTest(cancelled_index=cancelled_index):
                active_started = threading.Event()
                release_active = threading.Event()
                self.addCleanup(release_active.set)
                order = []

                def execute(run_id, **_kwargs):
                    self.store.transition_status(run_id, expected="queued", status="running")
                    order.append(run_id)
                    if len(order) == 1:
                        active_started.set()
                        release_active.wait(3)
                    with self.store.locked_run(run_id):
                        run = self.store.load(run_id)
                        run["status"] = "success"
                        self.store.save(run)

                prefix = f"cancel-position-{cancelled_index}"
                active = self.create(f"{prefix}-active")
                waiting = [self.create(f"{prefix}-{position}") for position in range(3)]
                manager = self.manager(execute=execute)
                manager.start()
                manager.submit(active["id"])
                self.assertTrue(active_started.wait(2))
                for run in waiting:
                    manager.submit(run["id"])

                manager.cancel(waiting[cancelled_index]["id"])

                expected_waiting = [
                    run["id"] for index, run in enumerate(waiting) if index != cancelled_index
                ]
                self.assertEqual(manager.queued_run_ids, tuple(expected_waiting))
                release_active.set()
                manager.stop(timeout=3)
                self.assertEqual(order, [active["id"], *expected_waiting])
                self.assertEqual(self.store.load(waiting[cancelled_index]["id"])["status"], "cancelled")

    def test_cancelled_queue_capacity_is_reusable_exactly_once(self):
        active_started = threading.Event()
        release_active = threading.Event()
        self.addCleanup(release_active.set)

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not active_started.is_set():
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(f"cancel-capacity-{index}") for index in range(4)]
        manager = self.manager(queue_capacity=2, execute=execute)
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(runs[1]["id"])
        manager.submit(runs[2]["id"])
        with self.assertRaises(ExecutionManagerError) as full:
            with manager.reserve():
                pass
        self.assertEqual(full.exception.code, "execution_queue_full")

        self.assertEqual(manager.cancel(runs[1]["id"])["outcome"], "queued_cancelled")
        self.assertEqual(manager.cancel(runs[1]["id"])["outcome"], "already_cancelled")
        with manager.reserve() as reservation:
            reservation.submit(runs[3]["id"])
        with self.assertRaises(ExecutionManagerError) as full_again:
            with manager.reserve():
                pass
        self.assertEqual(full_again.exception.code, "execution_queue_full")
        self.assertEqual(manager.queued_run_ids, (runs[2]["id"], runs[3]["id"]))

        release_active.set()
        manager.stop(timeout=3)

    def test_cancel_persistence_failure_keeps_queue_capacity_and_allows_execution(self):
        active_started = threading.Event()
        release_active = threading.Event()
        self.addCleanup(release_active.set)
        order = []

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            order.append(run_id)
            if len(order) == 1:
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        active = self.create("cancel-save-failure-active")
        queued = self.create("cancel-save-failure-queued")
        replacement = self.create("cancel-save-failure-replacement")
        manager = self.manager(queue_capacity=1, execute=execute)
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(queued["id"])
        original_save = self.store.save

        def fail_cancellation_save(run):
            if run["id"] == queued["id"] and run["status"] == "cancelled":
                raise OSError("simulated cancellation persistence failure")
            original_save(run)

        with mock.patch.object(self.store, "save", side_effect=fail_cancellation_save):
            with self.assertRaisesRegex(OSError, "simulated cancellation persistence failure"):
                manager.cancel(queued["id"])

        self.assertEqual(manager.queued_run_ids, (queued["id"],))
        self.assertEqual(self.store.load(queued["id"])["status"], "queued")
        with self.assertRaises(ExecutionManagerError) as full:
            manager.submit(replacement["id"])
        self.assertEqual(full.exception.code, "execution_queue_full")
        self.assertEqual(self.store.load(replacement["id"])["status"], "pending")

        release_active.set()
        manager.stop(timeout=3)
        self.assertEqual(order, [active["id"], queued["id"]])
        self.assertEqual(self.store.load(queued["id"])["status"], "success")

    def test_dequeue_installs_active_context_before_cancellation_can_observe_run(self):
        class AttemptObservedCondition(threading.Condition):
            def __init__(self):
                super().__init__()
                self.watched_thread_id = None
                self.acquire_attempted = threading.Event()

            def __enter__(self):
                if threading.get_ident() == self.watched_thread_id:
                    self.acquire_attempted.set()
                return super().__enter__()

        install_started = threading.Event()
        install_timed_out = threading.Event()
        worker_entered = threading.Event()
        release_worker = threading.Event()
        self.addCleanup(release_worker.set)
        observation = {}

        def execute(run_id, **_kwargs):
            observation["active_run_id"] = manager.active_run_id
            observation["control"] = manager.active_cancellation_control
            observation["persisted_status"] = self.store.load(run_id)["status"]
            worker_entered.set()
            release_worker.wait(3)
            self.store.transition_status(run_id, expected="queued", status="running")
            with self.store.locked_run(run_id):
                persisted = self.store.load(run_id)
                persisted["status"] = "cancelled" if observation["control"].event.is_set() else "success"
                self.store.save(persisted)

        run = self.create("dequeue-owns-before-cancel")
        manager = self.manager(execute=execute)
        condition = AttemptObservedCondition()
        manager._condition = condition

        def install_control():
            # This factory runs after popleft while the worker still owns the
            # manager Condition.  Hold it until cancellation attempts that
            # same lock, making the ownership-window assertion deterministic.
            install_started.set()
            if not condition.acquire_attempted.wait(2):
                install_timed_out.set()
            return CancellationControl()

        result = {}

        def cancel_while_installing():
            condition.watched_thread_id = threading.get_ident()
            result.update(manager.cancel(run["id"]))

        with mock.patch("debbuilder.execution_manager.CancellationControl", side_effect=install_control):
            manager.start()
            manager.submit(run["id"])
            self.assertTrue(install_started.wait(2))
            cancel_thread = threading.Thread(target=cancel_while_installing)
            cancel_thread.start()
            cancel_thread.join(3)

        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(install_timed_out.is_set())
        self.assertTrue(worker_entered.wait(2))

        self.assertEqual(observation["active_run_id"], run["id"])
        self.assertIsNotNone(observation["control"])
        self.assertEqual(observation["persisted_status"], "queued")
        self.assertEqual(result["outcome"], "active_cancel_requested")
        self.assertTrue(result["first_request"])
        self.assertTrue(observation["control"].event.is_set())
        self.assertEqual(self.store.load(run["id"])["status"], "queued")
        self.assertNotIn(run["id"], manager.queued_run_ids)

        release_worker.set()
        manager.stop(timeout=3)
        self.assertEqual(self.store.load(run["id"])["status"], "cancelled")

    def test_active_cancellation_is_idempotent_and_wrong_id_cannot_signal_it(self):
        worker_entered = threading.Event()
        release_worker = threading.Event()
        self.addCleanup(release_worker.set)

        def execute(run_id, *, cancellation_control, **_kwargs):
            worker_entered.set()
            release_worker.wait(3)
            self.store.transition_status(run_id, expected="queued", status="running")
            with self.store.locked_run(run_id):
                persisted = self.store.load(run_id)
                persisted["status"] = "cancelled" if cancellation_control.event.is_set() else "success"
                self.store.save(persisted)

        run = self.create("active-cancel-idempotent")
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(worker_entered.wait(2))
        control = manager.active_cancellation_control

        self.assertEqual(manager.cancel("different-run")["outcome"], "not_found")
        self.assertFalse(control.event.is_set())
        with mock.patch.object(control.event, "set", wraps=control.event.set) as signal:
            first = manager.cancel(run["id"])
            repeated = manager.cancel(run["id"])

        self.assertEqual(first["outcome"], "active_cancel_requested")
        self.assertTrue(first["first_request"])
        self.assertFalse(repeated["first_request"])
        self.assertEqual(repeated["cancellation"], first["cancellation"])
        self.assertEqual(signal.call_count, 1)
        release_worker.set()
        manager.stop(timeout=3)

    def test_terminal_owned_active_run_rejects_cancellation(self):
        terminal_claimed = threading.Event()
        release_worker = threading.Event()
        self.addCleanup(release_worker.set)

        def execute(run_id, *, cancellation_control, **_kwargs):
            self.assertTrue(cancellation_control.claim_terminal())
            terminal_claimed.set()
            release_worker.wait(3)
            self.finish(run_id)

        run = self.create("active-terminal-owned")
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(run["id"])
        self.assertTrue(terminal_claimed.wait(2))

        result = manager.cancel(run["id"])

        self.assertEqual(result["outcome"], "terminal_not_cancellable")
        self.assertFalse(manager.active_cancellation_control.event.is_set())
        release_worker.set()
        manager.stop(timeout=3)
        self.assertEqual(self.store.load(run["id"])["status"], "success")

    def test_cancel_result_categories_validate_ids_and_distinguish_terminal_state(self):
        manager = self.manager(execute=lambda *_args, **_kwargs: None)
        with self.assertRaises(ValueError):
            manager.cancel("../unsafe")
        self.assertEqual(manager.cancel("missing-run"), {"outcome": "not_found", "run_id": "missing-run"})

        terminal = self.create("cancel-terminal")
        terminal["status"] = "success"
        self.store.save(terminal)
        self.assertEqual(
            manager.cancel(terminal["id"]),
            {"outcome": "terminal_not_cancellable", "run_id": terminal["id"], "status": "success"},
        )

    def test_queued_build_executes_to_success(self):
        def acquire(_recipe, workspace, token=""):
            source = Path(workspace) / "source"
            (source / "package.json").write_text("{}")
            return {"repository":"owner/queued-build","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}

        available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
        configured = recipe("queued-build")
        configured["build"] = {
            "commands": [f"{shlex.quote(sys.executable)} -c 'import pathlib; pathlib.Path(\"dist\").mkdir(); pathlib.Path(\"dist/result\").write_text(\"ok\")'"],
            "output": {"mode": "path", "path": "dist"},
        }
        run = build_pipeline.create_pipeline_run(configured, store=self.store, dry_run=False)

        def execute(run_id, **kwargs):
            return build_pipeline.execute_pipeline_run(run_id, acquire=acquire, dependency_check=available, **kwargs)

        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(run["id"])
        manager.stop(timeout=5)

        persisted = self.store.load(run["id"])
        self.assertEqual(persisted["status"], "success")
        self.assertTrue(Path(persisted["artifact"]["path"]).is_file())

    def test_queued_dry_run_executes_to_prepared(self):
        def acquire(_recipe, workspace, token=""):
            source = Path(workspace) / "source"
            (source / "package.json").write_text("{}")
            return {"repository":"owner/queued-dry","ref":"v1","tag":"v1","upstream_version":"1.0","debian_version":"1.0-1","source_directory":str(source)}

        available = lambda detected, manual, **_kwargs: {"detected":detected,"manually_added":manual,"required":detected,"available":detected,"missing":[],"checks":[],"installation_attempted":False}
        run = self.create("queued-dry", dry_run=True)

        def execute(run_id, **kwargs):
            return build_pipeline.execute_pipeline_run(run_id, acquire=acquire, dependency_check=available, **kwargs)

        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(run["id"])
        manager.stop(timeout=4)

        self.assertEqual(self.store.load(run["id"])["status"], "prepared")

    def test_unexpected_exception_fails_run_and_worker_continues(self):
        first_started = threading.Event()
        fail_first = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not first_started.is_set():
                with self.store.locked_run(run_id):
                    run = self.store.load(run_id)
                    run["steps"][0]["status"] = "running"
                    self.store.save(run)
                first_started.set()
                fail_first.wait(2)
                raise RuntimeError("sensitive worker detail")
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        first = self.create("worker-error")
        second = self.create("worker-survivor")
        manager = self.manager(execute=execute)
        with self.assertLogs("debbuilder.execution_manager", level="ERROR") as captured:
            manager.start()
            manager.submit(first["id"])
            self.assertTrue(first_started.wait(2))
            manager.submit(second["id"])
            fail_first.set()
            manager.stop(timeout=3)

        failed = self.store.load(first["id"])
        self.assertEqual(failed["error"]["code"], "execution_worker_error")
        self.assertEqual(failed["steps"][0]["status"], "failed")
        self.assertNotIn("sensitive worker detail", self.store.log_text(first["id"]))
        self.assertNotIn("sensitive worker detail", "\n".join(captured.output))
        self.assertTrue(any("RuntimeError" in row for row in captured.output))
        self.assertEqual(self.store.load(second["id"])["status"], "success")

    def test_unexpected_worker_exception_while_cancelling_becomes_failed(self):
        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "cancelling"
                run["cancellation"] = {
                    "code": "execution_cancelled",
                    "reason": "user_requested",
                    "phase": "pipeline",
                    "stage": "build",
                    "requested_at": run["started_at"] or run["created_at"],
                }
                run["steps"][4]["status"] = "running"
                self.store.save(run)
            raise RuntimeError("unexpected cancellation finalizer failure")

        run = self.create("cancelling-worker-error")
        manager = self.manager(execute=execute)
        with self.assertLogs("debbuilder.execution_manager", level="ERROR"):
            manager.start()
            manager.submit(run["id"])
            manager.stop(timeout=3)

        persisted = self.store.load(run["id"])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["error"]["code"], "execution_worker_error")
        self.assertEqual(persisted["steps"][4]["status"], "failed")
        self.assertEqual(persisted["cancellation"]["reason"], "user_requested")

    def test_terminal_state_is_not_overwritten_by_worker_error(self):
        def execute(run_id, **_kwargs):
            self.finish(run_id)
            raise RuntimeError("after completion")

        run = self.create("terminal-preserved")
        manager = self.manager(execute=execute)
        with self.assertLogs("debbuilder.execution_manager", level="ERROR"):
            manager.start()
            manager.submit(run["id"])
            manager.stop(timeout=2)

        self.assertEqual(self.store.load(run["id"])["status"], "success")
        self.assertIsNone(self.store.load(run["id"])["error"])

    def test_double_submit_is_rejected_deterministically(self):
        active_started = threading.Event()
        release_active = threading.Event()

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            if not active_started.is_set():
                active_started.set()
                release_active.wait(2)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        active = self.create("double-active")
        duplicate = self.create("double-queued")
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(active["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(duplicate["id"])

        with self.assertRaises(ExecutionManagerError) as repeated:
            manager.submit(duplicate["id"])
        self.assertEqual(repeated.exception.as_dict(), {
            "code": "build_run_already_submitted",
            "message": "Build Run is already queued or running",
            "details": {"run_id": duplicate["id"]},
        })
        release_active.set()
        manager.stop(timeout=3)

    def test_submit_rejects_unknown_and_non_pending_runs(self):
        manager = self.manager(execute=lambda *_args, **_kwargs: None)
        manager.start()
        with self.assertRaises(ExecutionManagerError) as missing:
            manager.submit("missing-run")
        self.assertEqual(missing.exception.code, "build_run_not_found")

        for status in ("queued", "running", "cancelling", "prepared", "success", "failed", "cancelled"):
            with self.subTest(status=status):
                run = self.create(f"wrong-{status}")
                run["status"] = status
                self.store.save(run)
                with self.assertRaises(ExecutionManagerError) as refused:
                    manager.submit(run["id"])
                self.assertEqual(refused.exception.code, "build_run_not_pending")
                self.assertEqual(refused.exception.details["status"], status)
        manager.stop(timeout=2)

    def test_stop_revokes_abandoned_reservations_and_drains_queued_runs(self):
        active_started = threading.Event()
        release_active = threading.Event()
        order = []

        def execute(run_id, **_kwargs):
            self.store.transition_status(run_id, expected="queued", status="running")
            order.append(run_id)
            if len(order) == 1:
                active_started.set()
                release_active.wait(3)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run["status"] = "success"
                self.store.save(run)

        runs = [self.create(f"stop-{index}") for index in range(3)]
        manager = self.manager(execute=execute)
        manager.start()
        manager.submit(runs[0]["id"])
        self.assertTrue(active_started.wait(2))
        manager.submit(runs[1]["id"])
        manager.submit(runs[2]["id"])

        reservation_context = manager.reserve()
        abandoned_reservation = reservation_context.__enter__()
        stopped = threading.Event()
        stop_thread = threading.Thread(target=lambda: (manager.stop(timeout=5), stopped.set()))
        stop_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: not manager._accepting, timeout=2))
        self.assertFalse(stopped.is_set())
        self.assertEqual(manager.queued_run_ids, (runs[1]["id"], runs[2]["id"]))

        release_active.set()
        stop_thread.join()

        self.assertTrue(stopped.is_set())
        self.assertFalse(manager.worker.is_alive())
        self.assertEqual(manager.queued_run_ids, ())
        self.assertEqual(order, [run["id"] for run in runs])
        self.assertTrue(all(self.store.load(run_id)["status"] == "success" for run_id in order))
        with self.assertRaises(ExecutionManagerError) as revoked:
            abandoned_reservation.submit(self.create("stop-reserved")["id"])
        self.assertEqual(revoked.exception.code, "execution_reservation_inactive")
        reservation_context.__exit__(None, None, None)
        with self.assertRaises(ExecutionManagerError) as refused:
            manager.submit(self.create("after-stop")["id"])
        self.assertEqual(refused.exception.code, "execution_manager_not_accepting")

    def test_stop_drains_a_submission_already_persisting(self):
        transition_started = threading.Event()
        release_transition = threading.Event()
        original_transition = self.store.transition_status

        def delayed_transition(run_id, *, expected, status):
            if expected == "pending":
                transition_started.set()
                release_transition.wait(2)
            return original_transition(run_id, expected=expected, status=status)

        self.store.transition_status = delayed_transition
        run = self.create("stop-submitting")
        manager = self.manager(execute=lambda run_id, **_kwargs: self.finish(run_id))
        manager.start()
        submit_errors = []

        def submit():
            try:
                manager.submit(run["id"])
            except Exception as exc:
                submit_errors.append(exc)

        submit_thread = threading.Thread(target=submit)
        submit_thread.start()
        self.assertTrue(transition_started.wait(2))
        stopped = threading.Event()
        stop_thread = threading.Thread(target=lambda: (manager.stop(timeout=4), stopped.set()))
        stop_thread.start()
        with manager._condition:
            self.assertTrue(manager._condition.wait_for(lambda: not manager._accepting, timeout=2))
        self.assertFalse(stopped.is_set())

        release_transition.set()
        submit_thread.join()
        stop_thread.join()

        self.assertEqual(submit_errors, [])
        self.assertTrue(stopped.is_set())
        self.assertFalse(manager.worker.is_alive())
        self.assertEqual(self.store.load(run["id"])["status"], "success")


if __name__ == "__main__":
    unittest.main()
