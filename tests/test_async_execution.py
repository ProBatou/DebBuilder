import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from debbuilder import app as server
from debbuilder.build_store import BuildStore
from tests.admin_api_case import AdminApiCase


def recipe(name: str) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "active": True,
        "package": {
            "name": name,
            "architecture": "all",
            "maintainer": "Demo <demo@example.test>",
            "description": "Demo package",
        },
        "source": {
            "provider": "github",
            "repository": f"owner/{name}",
            "tracking": "latest_release",
            "version": {"source": "tag"},
        },
    }


class AsyncExecutionTests(AdminApiCase):
    def replace_manager(self, *, queue_capacity=8, execute):
        server.stop_execution_manager(self.httpd, timeout=5)
        manager = server.create_execution_manager(
            store=BuildStore(server.DATA / "builds"),
            queue_capacity=queue_capacity,
            execute=execute,
        )
        server.start_execution_manager(self.httpd, manager)
        self.execution_manager = manager
        return manager

    def blocking_executor(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            started.set()
            if not release.wait(3):
                raise TimeoutError("test did not release blocked execution")
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "prepared" if run["mode"] == "dry_run" else "success"
                store.save(run)
            finished.set()

        return execute, started, release, finished

    def error_response(self, method, path, body):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request(method, path, body)
        return captured.exception.code, json.loads(captured.exception.read())

    def test_build_and_dry_run_are_accepted_then_executed_asynchronously(self):
        for dry_run, terminal in ((True, "prepared"), (False, "success")):
            with self.subTest(dry_run=dry_run):
                execute, started, release, finished = self.blocking_executor()
                self.replace_manager(execute=execute)
                status, response = self.request(
                    "POST", "/api/run", {"workflow": recipe(f"async-{dry_run}"), "dry_run": dry_run},
                )
                self.assertEqual(status, 202)
                self.assertEqual(response["status"], "queued")
                self.assertTrue(started.wait(2))
                self.assertEqual(BuildStore(server.DATA / "builds").load(response["run_id"])["status"], "running")
                release.set()
                self.assertTrue(finished.wait(2))
                self.assertEqual(BuildStore(server.DATA / "builds").load(response["run_id"])["status"], terminal)

    def test_response_returns_while_worker_is_still_blocked(self):
        execute, started, release, _finished = self.blocking_executor()
        self.replace_manager(execute=execute)
        try:
            status, response = self.request(
                "POST", "/api/run", {"workflow": recipe("returns-first"), "dry_run": False},
            )
            self.assertEqual((status, response["status"]), (202, "queued"))
            self.assertTrue(started.wait(2))
            self.assertFalse(release.is_set())
        finally:
            release.set()

    def test_full_queue_returns_429_without_creating_a_run_or_workspace(self):
        execute, started, release, _finished = self.blocking_executor()
        manager = self.replace_manager(execute=execute)
        try:
            accepted = []
            for index in range(9):
                status, response = self.request(
                    "POST", "/api/run", {"workflow": recipe(f"capacity-{index}"), "dry_run": False},
                )
                self.assertEqual(status, 202)
                accepted.append(response["run_id"])
                if index == 0:
                    self.assertTrue(started.wait(2))
            before = {path.name for path in manager.store.root.iterdir()}
            status, response = self.error_response(
                "POST", "/api/run", {"workflow": recipe("rejected-full"), "dry_run": False},
            )
            after = {path.name for path in manager.store.root.iterdir()}
            self.assertEqual(status, 429)
            self.assertEqual(response["error"]["code"], "execution_queue_full")
            self.assertEqual(before, after)
            self.assertEqual(manager.active_run_id, accepted[0])
            self.assertEqual(len(manager.queued_run_ids), 8)
        finally:
            release.set()

    def test_concurrent_last_capacity_has_one_202_one_429_and_no_orphan(self):
        execute, started, release, _finished = self.blocking_executor()
        manager = self.replace_manager(queue_capacity=1, execute=execute)
        try:
            self.request("POST", "/api/run", {"workflow": recipe("active"), "dry_run": False})
            self.assertTrue(started.wait(2))
            before = {path.name for path in manager.store.root.iterdir()}
            barrier = threading.Barrier(2)

            def submit(index):
                barrier.wait()
                try:
                    status, response = self.request(
                        "POST", "/api/run", {"workflow": recipe(f"racer-{index}"), "dry_run": False},
                    )
                    return status, response
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(submit, range(2)))
            self.assertEqual(sorted(status for status, _response in results), [202, 429])
            rejection = next(response for status, response in results if status == 429)
            self.assertEqual(rejection["error"]["code"], "execution_queue_full")
            after = {path.name for path in manager.store.root.iterdir()}
            self.assertEqual(len(after - before), 1)
            self.assertEqual(len(manager.queued_run_ids), 1)
        finally:
            release.set()

    def test_unavailable_manager_returns_503_without_creating_a_run(self):
        server.stop_execution_manager(self.httpd, timeout=5)
        before = {path.name for path in (server.DATA / "builds").iterdir()}
        status, response = self.error_response(
            "POST", "/api/run", {"workflow": recipe("unavailable"), "dry_run": False},
        )
        after = {path.name for path in (server.DATA / "builds").iterdir()}
        self.assertEqual(status, 503)
        self.assertEqual(response["error"]["code"], "execution_manager_unavailable")
        self.assertEqual(before, after)

    def test_create_failure_releases_reserved_capacity(self):
        execute, _started, release, _finished = self.blocking_executor()
        manager = self.replace_manager(queue_capacity=1, execute=execute)
        original = server.build_pipeline.create_pipeline_run
        before = {path.name for path in manager.store.root.iterdir()}
        with mock.patch.object(server.build_pipeline, "create_pipeline_run", side_effect=ValueError("invalid recipe")):
            with self.assertRaises(ValueError):
                server.enqueue_recipe_run(manager, recipe("invalid"), dry_run=False)
        self.assertEqual({path.name for path in manager.store.root.iterdir()}, before)
        with manager._condition:
            self.assertEqual(manager._admission_leases, set())
            self.assertEqual(manager._reservation_run_ids, {})
        try:
            response = server.enqueue_recipe_run(manager, recipe("valid-after-error"), dry_run=False)
            self.assertEqual(response["status"], "queued")
        finally:
            release.set()
        self.assertIs(server.build_pipeline.create_pipeline_run, original)

    def test_partial_creation_without_run_json_is_discarded_and_lease_releases(self):
        manager = self.replace_manager(execute=lambda *_args, **_kwargs: self.fail("must not execute"))
        allocated = []

        def create_partial(*_args, **kwargs):
            run_id = kwargs["run_id"]
            allocated.append(run_id)
            manager.store.run_dir(run_id).mkdir(parents=True)
            raise OSError("metadata initialization failed")

        with mock.patch.object(server.build_pipeline, "create_pipeline_run", side_effect=create_partial):
            with self.assertRaises(OSError):
                server.enqueue_recipe_run(manager, recipe("partial-create"), dry_run=False)

        self.assertEqual(len(allocated), 1)
        self.assertFalse(manager.store.run_dir(allocated[0]).exists())
        with manager._condition:
            self.assertEqual(manager._admission_leases, set())
            self.assertEqual(manager._reservation_run_ids, {})
            self.assertEqual(manager._unresolved_admission_runs, {})

    def test_committed_creation_baseexception_remains_owned_until_shutdown_retry(self):
        class CreationAbort(BaseException):
            pass

        executed = []
        manager = self.replace_manager(execute=lambda run_id, **_kwargs: executed.append(run_id))
        original_create = server.build_pipeline.create_pipeline_run
        original_save = manager.store.save
        created = {}

        def create_then_abort(*args, **kwargs):
            run = original_create(*args, **kwargs)
            created.update(run)
            raise CreationAbort("caller did not observe committed creation")

        def reject_terminal(value):
            if value.get("status") == "failed":
                raise OSError("terminal storage unavailable")
            return original_save(value)

        with mock.patch.object(server.build_pipeline, "create_pipeline_run", side_effect=create_then_abort), \
                mock.patch.object(manager.store, "save", side_effect=reject_terminal):
            with self.assertRaises(CreationAbort):
                server.enqueue_recipe_run(manager, recipe("committed-create"), dry_run=False)
            run_id = created["id"]
            self.assertEqual(manager.store.load(run_id)["status"], "pending")
            first = manager.shutdown(timeout=0)

        self.assertFalse(first["complete"])
        self.assertEqual(first["unresolved_admission_run_ids"], [run_id])
        self.assertEqual(first["outstanding_reservations"], 0)
        self.assertEqual(executed, [])

        second = manager.shutdown(timeout=2)
        self.assertTrue(second["complete"])
        self.assertEqual(second["unresolved_admission_run_ids"], [])
        self.assertEqual(manager.store.load(run_id)["status"], "failed")
        self.assertEqual(executed, [])
        repeated = manager.shutdown(timeout=2)
        self.assertTrue(repeated["complete"])
        self.assertEqual(repeated["outstanding_reservations"], 0)

    def test_post_commit_creation_terminalization_exception_is_reread_as_terminal(self):
        class PostCommitAbort(BaseException):
            pass

        executed = []
        manager = self.replace_manager(execute=lambda run_id, **_kwargs: executed.append(run_id))
        original_create = server.build_pipeline.create_pipeline_run
        original_save = manager.store.save
        created = {}
        raised = False

        def create_then_fail(*args, **kwargs):
            run = original_create(*args, **kwargs)
            created.update(run)
            raise RuntimeError("metadata initialization failed after run commit")

        def save_then_abort(value):
            nonlocal raised
            original_save(value)
            if value.get("status") == "failed" and not raised:
                raised = True
                raise PostCommitAbort("post-commit callback aborted")

        with mock.patch.object(server.build_pipeline, "create_pipeline_run", side_effect=create_then_fail), \
                mock.patch.object(manager.store, "save", side_effect=save_then_abort):
            with self.assertRaises(RuntimeError):
                server.enqueue_recipe_run(manager, recipe("post-commit-create"), dry_run=False)

        run_id = created["id"]
        self.assertEqual(manager.store.load(run_id)["status"], "failed")
        result = manager.shutdown(timeout=2)
        self.assertTrue(result["complete"])
        self.assertEqual(result["unresolved_admission_run_ids"], [])
        self.assertEqual(result["outstanding_reservations"], 0)
        self.assertEqual(executed, [])

    def test_submit_failure_marks_created_run_failed(self):
        manager = self.execution_manager
        before = {run["id"] for run in manager.store.list(limit=100)}
        with mock.patch.object(manager, "_mark_queued", side_effect=RuntimeError("technical detail")):
            with self.assertRaises(server.RunAdmissionError) as captured:
                server.enqueue_recipe_run(manager, recipe("submit-failure"), dry_run=False)
        self.assertEqual(captured.exception.status, 500)
        self.assertEqual(captured.exception.code, "execution_enqueue_failed")
        created = [run for run in manager.store.list(limit=100) if run["id"] not in before]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["status"], "failed")
        self.assertEqual(created[0]["error"]["code"], "execution_enqueue_failed")
        self.assertNotIn("technical detail", json.dumps(created[0]["error"]))

    def test_failed_terminal_persistence_transfers_admitted_run_to_shutdown_retry(self):
        manager = self.replace_manager(execute=lambda *_args, **_kwargs: self.fail("must not execute"))
        original_mark_queued = manager._mark_queued
        original_save = manager.store.save

        def queue_then_raise(run_id):
            original_mark_queued(run_id)
            raise OSError("submission bookkeeping failed")

        def reject_failure(value):
            if value.get("status") == "failed":
                raise OSError("terminal storage unavailable")
            return original_save(value)

        with mock.patch.object(manager, "_mark_queued", side_effect=queue_then_raise), \
                mock.patch.object(manager.store, "save", side_effect=reject_failure):
            with self.assertRaises(server.RunAdmissionError) as captured:
                server.enqueue_recipe_run(manager, recipe("unresolved-admission"), dry_run=False)
            run_id = captured.exception.details["run_id"]
            self.assertEqual(manager.store.load(run_id)["status"], "queued")
            first = manager.shutdown(timeout=0)

        self.assertFalse(first["complete"])
        self.assertEqual(first["unresolved_admission_run_ids"], [run_id])
        self.assertEqual(first["outstanding_reservations"], 0)
        self.assertEqual(first["errors"][0]["ownership"], "admission")

        second = manager.shutdown(timeout=2)
        self.assertTrue(second["complete"])
        self.assertEqual(second["unresolved_admission_run_ids"], [])
        self.assertEqual(manager.store.load(run_id)["status"], "failed")
        self.assertTrue(manager.shutdown(timeout=2)["complete"])

    def test_post_commit_terminal_exception_settles_admission_once(self):
        manager = self.replace_manager(execute=lambda *_args, **_kwargs: self.fail("must not execute"))
        original_mark_queued = manager._mark_queued
        original_save = manager.store.save
        raised = False

        def queue_then_raise(run_id):
            original_mark_queued(run_id)
            raise OSError("submission bookkeeping failed")

        def save_then_raise(value):
            nonlocal raised
            original_save(value)
            if value.get("status") == "failed" and not raised:
                raised = True
                raise OSError("post-commit metadata failure")

        with mock.patch.object(manager, "_mark_queued", side_effect=queue_then_raise), \
                mock.patch.object(manager.store, "save", side_effect=save_then_raise):
            with self.assertRaises(server.RunAdmissionError) as captured:
                server.enqueue_recipe_run(manager, recipe("terminal-admission"), dry_run=False)

        run_id = captured.exception.details["run_id"]
        self.assertEqual(manager.store.load(run_id)["status"], "failed")
        result = manager.shutdown(timeout=2)
        self.assertTrue(result["complete"])
        self.assertEqual(result["unresolved_admission_run_ids"], [])
        self.assertEqual(result["outstanding_reservations"], 0)

    def test_shutdown_waits_for_admitted_daemon_request_and_cancels_its_run(self):
        executed = []
        manager = self.replace_manager(execute=lambda run_id, **_kwargs: executed.append(run_id))
        self.assertTrue(self.httpd.daemon_threads)
        creation_entered = threading.Event()
        release_creation = threading.Event()
        original_create = server.build_pipeline.create_pipeline_run

        def delayed_create(*args, **kwargs):
            creation_entered.set()
            self.assertTrue(release_creation.wait(3))
            return original_create(*args, **kwargs)

        response = {}
        request_errors = []

        def request_run():
            try:
                response["value"] = self.request(
                    "POST", "/api/run", {"workflow": recipe("request-shutdown-race"), "dry_run": False},
                )
            except BaseException as exc:
                request_errors.append(exc)

        shutdown_result = {}
        with mock.patch.object(server.build_pipeline, "create_pipeline_run", side_effect=delayed_create):
            request_thread = threading.Thread(target=request_run)
            request_thread.start()
            self.assertTrue(creation_entered.wait(2))
            shutdown_thread = threading.Thread(
                target=lambda: shutdown_result.update(manager.shutdown(timeout=4)),
            )
            shutdown_thread.start()
            with manager._condition:
                self.assertTrue(manager._condition.wait_for(lambda: manager._shutdown_started, timeout=2))
            self.assertTrue(request_thread.is_alive())
            self.assertTrue(shutdown_thread.is_alive())
            release_creation.set()
            request_thread.join(3)
            shutdown_thread.join(4)

        self.assertEqual(request_errors, [])
        status, payload = response["value"]
        self.assertEqual((status, payload["status"]), (202, "queued"))
        self.assertTrue(shutdown_result["complete"])
        self.assertEqual(executed, [])
        persisted = manager.store.load(payload["run_id"])
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation"]["reason"], "server_shutdown")

    def test_worker_runs_build_automation_and_only_requests_async_maintenance(self):
        completed = threading.Event()

        def pipeline(run_id, *, store, expected_initial_status, **_kwargs):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "success"
                store.save(run)
            return {"run_id": run_id, "status": "success"}

        notification = mock.Mock()
        automation = {"validation": None, "publication": None}
        with mock.patch.object(server.build_pipeline, "execute_pipeline_run", side_effect=pipeline), \
                mock.patch.object(server, "run_post_build_automation", return_value=automation) as automate, \
                mock.patch.object(server, "notification_service", return_value=notification), \
                mock.patch.object(server, "request_maintenance", side_effect=lambda **_kwargs: completed.set()) as request, \
                mock.patch.object(server.workspace_cleanup, "apply_retention") as cleanup:
            status, response = self.request(
                "POST", "/api/run", {"workflow": recipe("automated"), "dry_run": False},
            )
            self.assertEqual(status, 202)
            self.assertTrue(completed.wait(2))
        automate.assert_called_once_with(response["run_id"], dry_run=False, store=self.execution_manager.store)
        notification.notify_automatic_completion.assert_called_once()
        request.assert_called_once_with(cleanup=True)
        cleanup.assert_not_called()

    def test_server_manager_lifecycle_is_explicit_and_joins_worker(self):
        class HttpServer:
            pass

        http_server = HttpServer()
        manager = server.create_execution_manager(
            store=BuildStore(server.DATA / "separate-builds"),
            execute=lambda *_args, **_kwargs: None,
        )
        self.assertIsNone(manager.worker)
        server.start_execution_manager(http_server, manager)
        worker = manager.worker
        self.assertIs(http_server.execution_manager, manager)
        self.assertTrue(worker.is_alive())
        self.assertFalse(worker.daemon)
        server.stop_execution_manager(http_server, timeout=2)
        self.assertIsNone(http_server.execution_manager)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    import unittest

    unittest.main()
