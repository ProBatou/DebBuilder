import json
import shlex
import sys
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from debbuilder import app as server, build_pipeline, command_runner
from debbuilder.build_models import utc_now
from debbuilder.build_store import BuildStore
from debbuilder.execution_cancellation import ExecutionCancelled
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
            "description": "Cancellation API test",
        },
        "source": {
            "provider": "github",
            "repository": f"owner/{name}",
            "tracking": "latest_release",
            "version": {"source": "tag"},
        },
    }


class CancellationApiTests(AdminApiCase):
    def replace_manager(self, execute):
        server.stop_execution_manager(self.httpd, timeout=5)
        manager = server.create_execution_manager(
            store=BuildStore(server.DATA / "builds"), execute=execute,
        )
        server.start_execution_manager(self.httpd, manager)
        self.execution_manager = manager
        return manager

    def create_run(self, name: str, *, mode="build"):
        return self.execution_manager.store.create(
            recipe(name), recipe_id=name, mode=mode,
        )

    def error_response(self, path: str, body=None, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.request("POST", path, body, headers=headers)
        return captured.exception.code, json.loads(captured.exception.read())

    @staticmethod
    def finish_from_control(store, run_id, cancellation_control, *, status="success"):
        with store.locked_run(run_id):
            run = store.load(run_id)
            if cancellation_control.event.is_set():
                completed_at = utc_now()
                run.update({
                    "status": "cancelled",
                    "error": None,
                    "finished_at": completed_at,
                    "cancellation": {
                        **(cancellation_control.request or {}),
                        "phase": "pipeline",
                        "stage": "build",
                        "completed_at": completed_at,
                    },
                })
            else:
                run["status"] = status
            store.save(run)

    def blocking_executor(self, *, transition_before_wait=True):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            if transition_before_wait:
                store.transition_status(run_id, expected=expected_initial_status, status="running")
            entered.set()
            release.wait(3)
            if not transition_before_wait:
                store.transition_status(run_id, expected=expected_initial_status, status="running")
            self.finish_from_control(store, run_id, cancellation_control)
            finished.set()

        return execute, entered, release, finished

    def test_queued_cancel_returns_200_and_runs_cleanup_outside_manager_lock(self):
        execute, entered, release, _finished = self.blocking_executor()
        manager = self.replace_manager(execute)
        active = self.create_run("queued-cleanup-active")
        queued = self.create_run("queued-cleanup-target")
        manager.submit(active["id"])
        self.assertTrue(entered.wait(2))
        manager.submit(queued["id"])
        lock_observations = []

        try:
            with mock.patch.object(
                server, "cleanup_workspaces",
                side_effect=lambda: lock_observations.append(manager._condition._is_owned()) or {},
            ) as cleanup:
                status, response = self.request("POST", f"/api/executions/{queued['id']}/cancel")
                repeated_status, repeated = self.request("POST", f"/api/executions/{queued['id']}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(repeated_status, 200)
            self.assertEqual(response["cancellation"]["status"], "cancelled")
            self.assertEqual(repeated["cancellation"], response["cancellation"])
            self.assertEqual(response["cancellation"]["code"], "execution_cancelled")
            self.assertTrue(response["cancellation"]["completed_at"])
            self.assertEqual(manager.store.load(queued["id"])["status"], "cancelled")
            self.assertEqual(lock_observations, [False])
            cleanup.assert_called_once_with()
        finally:
            release.set()

    def test_cleanup_exception_does_not_undo_queued_cancellation(self):
        execute, entered, release, _finished = self.blocking_executor()
        manager = self.replace_manager(execute)
        active = self.create_run("cleanup-error-active")
        queued = self.create_run("cleanup-error-target")
        manager.submit(active["id"])
        self.assertTrue(entered.wait(2))
        manager.submit(queued["id"])
        try:
            with mock.patch.object(server, "cleanup_workspaces", side_effect=RuntimeError("cleanup failed")):
                with self.assertLogs("debbuilder.app", level="ERROR"):
                    status, response = self.request("POST", f"/api/executions/{queued['id']}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(response["cancellation"]["status"], "cancelled")
            self.assertEqual(manager.store.load(queued["id"])["status"], "cancelled")
        finally:
            release.set()

    def test_queued_cancellation_applies_retention_without_erasing_history(self):
        execute, entered, release, _finished = self.blocking_executor()
        manager = self.replace_manager(execute)
        active = self.create_run("queued-retention-active")
        queued = self.create_run("queued-retention-target", mode="dry_run")
        workspace = Path(queued["workspace"])
        (workspace / "source/disposable.txt").write_text("temporary")
        manager.submit(active["id"])
        self.assertTrue(entered.wait(2))
        manager.submit(queued["id"])
        server.update_settings({"workspace_cleanup": {"enabled": True, "failed_workspaces_to_retain": 0}})

        try:
            status, response = self.request("POST", f"/api/executions/{queued['id']}/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(response["cancellation"]["status"], "cancelled")
            self.assertFalse((workspace / "source").exists())
            self.assertTrue((workspace / "run.json").is_file())
            self.assertTrue((workspace / "recipe.json").is_file())
            detail_status, detail = self.request("GET", f"/api/executions/{queued['id']}")
            log_status, log = self.request("GET", f"/api/executions/{queued['id']}/logs?verbosity=normal")
            self.assertEqual((detail_status, log_status), (200, 200))
            self.assertEqual(detail["execution"]["status"], "cancelled")
            self.assertEqual(detail["execution"]["cancellation"]["phase"], "queue")
            self.assertTrue(log["log"]["complete"])
            self.assertNotIn(queued["id"], manager.queued_run_ids)
        finally:
            release.set()

    def test_failed_queued_persistence_does_not_sweep_or_release_queue_entry(self):
        execute, entered, release, _finished = self.blocking_executor()
        manager = self.replace_manager(execute)
        active = self.create_run("persist-error-active")
        queued = self.create_run("persist-error-target")
        manager.submit(active["id"])
        self.assertTrue(entered.wait(2))
        manager.submit(queued["id"])
        original_save = manager.store.save

        def fail_cancelled_save(run):
            if run["id"] == queued["id"] and run["status"] == "cancelled":
                raise OSError("simulated persistence failure")
            return original_save(run)

        try:
            with mock.patch.object(manager.store, "save", side_effect=fail_cancelled_save), \
                 mock.patch.object(server, "cleanup_workspaces") as cleanup:
                status, response = self.error_response(f"/api/executions/{queued['id']}/cancel", {})
            self.assertEqual(status, 500)
            self.assertEqual(response["error"]["code"], "execution_cancellation_failed")
            self.assertEqual(manager.store.load(queued["id"])["status"], "queued")
            self.assertIn(queued["id"], manager.queued_run_ids)
            cleanup.assert_not_called()
        finally:
            release.set()

    def test_running_cancel_returns_before_worker_finishes_and_http_does_not_save(self):
        worker_running = threading.Event()
        cancellation_seen = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            worker_running.set()
            cancellation_control.event.wait(3)
            cancellation_seen.set()
            release.wait(3)
            self.finish_from_control(store, run_id, cancellation_control)
            finished.set()

        manager = self.replace_manager(execute)
        run = self.create_run("active-no-http-save")
        manager.submit(run["id"])
        self.assertTrue(worker_running.wait(2))

        try:
            with mock.patch.object(manager.store, "save", wraps=manager.store.save) as save, \
                 mock.patch.object(server, "cleanup_workspaces") as cleanup:
                status, response = self.request("POST", f"/api/executions/{run['id']}/cancel")
                self.assertTrue(cancellation_seen.wait(2))
                self.assertFalse(release.is_set())
                save.assert_not_called()
                cleanup.assert_not_called()
            self.assertEqual(status, 202)
            self.assertEqual(response["cancellation"]["status"], "cancelling")
            self.assertEqual(manager.store.load(run["id"])["status"], "running")
        finally:
            release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(manager.store.load(run["id"])["status"], "cancelled")

    def test_active_manager_ownership_beats_persisted_queued_status(self):
        execute, entered, release, finished = self.blocking_executor(transition_before_wait=False)
        manager = self.replace_manager(execute)
        run = self.create_run("active-still-persisted-queued")
        manager.submit(run["id"])
        self.assertTrue(entered.wait(2))
        self.assertEqual(manager.store.load(run["id"])["status"], "queued")
        try:
            with mock.patch.object(server, "cleanup_workspaces") as cleanup:
                status, response = self.request("POST", f"/api/executions/{run['id']}/cancel", {})
            self.assertEqual(status, 202)
            self.assertEqual(response["cancellation"]["status"], "cancelling")
            self.assertEqual(manager.store.load(run["id"])["status"], "queued")
            cleanup.assert_not_called()
        finally:
            release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(manager.store.load(run["id"])["status"], "cancelled")

    def test_repeated_and_concurrent_active_requests_share_one_signal_and_timestamp(self):
        execute, entered, release, finished = self.blocking_executor()
        manager = self.replace_manager(execute)
        run = self.create_run("concurrent-cancel")
        manager.submit(run["id"])
        self.assertTrue(entered.wait(2))
        control = manager.active_cancellation_control

        def cancel(_index):
            return self.request("POST", f"/api/executions/{run['id']}/cancel", {})

        try:
            with mock.patch.object(control.event, "set", wraps=control.event.set) as signal:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    responses = list(pool.map(cancel, range(6)))
            self.assertTrue(all(status == 202 for status, _response in responses))
            timestamps = {response["cancellation"]["requested_at"] for _status, response in responses}
            self.assertEqual(len(timestamps), 1)
            self.assertEqual(signal.call_count, 1)
        finally:
            release.set()
        self.assertTrue(finished.wait(2))
        status, response = self.request("POST", f"/api/executions/{run['id']}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(response["cancellation"]["status"], "cancelled")
        self.assertEqual(response["cancellation"]["requested_at"], next(iter(timestamps)))

    def test_concurrent_api_requests_terminate_one_real_process_group_once(self):
        ready = threading.Event()
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            with store.locked_run(run_id):
                run = store.load(run_id)
                run.update({"status": "running", "started_at": utc_now()})
                store.save(run)
                command = (
                    f"{shlex.quote(sys.executable)} -c "
                    "'import signal,time; "
                    "signal.signal(signal.SIGTERM, lambda *_: exit(0)); "
                    "print(\"api-process-ready\", flush=True); time.sleep(10)'"
                )
                try:
                    command_runner.run_command(
                        command,
                        workspace=run["workspace"],
                        inactivity_timeout=None,
                        maximum_runtime=20,
                        cancellation_event=cancellation_control.event,
                        on_cancel=lambda: build_pipeline._observe_cancellation(
                            cancellation_control, run, store, "build",
                        ),
                        on_output=lambda item: ready.set() if "api-process-ready" in item["text"] else None,
                    )
                except ExecutionCancelled as cancelled:
                    completed_at = utc_now()
                    run.update({
                        "status": "cancelled", "error": None, "finished_at": completed_at,
                        "cancellation": {
                            **cancelled.cancellation, "phase": "pipeline", "stage": "build",
                            "completed_at": completed_at,
                        },
                    })
                    store.save(run)
                finally:
                    finished.set()

        manager = self.replace_manager(execute)
        run = self.create_run("concurrent-real-process")
        manager.submit(run["id"])
        self.assertTrue(ready.wait(3))
        clients = 8
        barrier = threading.Barrier(clients)

        def cancel(_index):
            barrier.wait()
            return self.request("POST", f"/api/executions/{run['id']}/cancel", {})

        original_terminate = command_runner._terminate_process_group
        with mock.patch.object(command_runner, "_terminate_process_group", wraps=original_terminate) as terminate:
            with ThreadPoolExecutor(max_workers=clients) as pool:
                responses = list(pool.map(cancel, range(clients)))
            self.assertTrue(finished.wait(3))

        self.assertEqual(terminate.call_count, 1)
        self.assertTrue(all(status in {200, 202} for status, _response in responses))
        timestamps = {response["cancellation"]["requested_at"] for _status, response in responses}
        self.assertEqual(len(timestamps), 1)
        persisted = manager.store.load(run["id"])
        self.assertEqual(persisted["status"], "cancelled")
        self.assertEqual(persisted["cancellation"]["requested_at"], next(iter(timestamps)))
        self.assertEqual(
            sum("Cancellation requested" in event["message"] for event in persisted["events"]), 1,
        )

    def test_terminal_gate_win_returns_409_then_run_completes_normally(self):
        terminal_claimed = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            self.assertTrue(cancellation_control.claim_terminal())
            terminal_claimed.set()
            release.wait(3)
            self.finish_from_control(store, run_id, cancellation_control, status="success")
            finished.set()

        manager = self.replace_manager(execute)
        run = self.create_run("terminal-wins")
        manager.submit(run["id"])
        self.assertTrue(terminal_claimed.wait(2))
        try:
            status, response = self.error_response(f"/api/executions/{run['id']}/cancel", {})
            self.assertEqual(status, 409)
            self.assertEqual(response["error"]["code"], "execution_not_cancellable")
            self.assertEqual(response["error"]["details"]["status"], "running")
            self.assertFalse(manager.active_cancellation_control.event.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(manager.store.load(run["id"])["status"], "success")

    def test_cancellation_gate_win_returns_202_and_terminal_claim_cannot_succeed(self):
        active = threading.Event()
        cancellation_seen = threading.Event()
        terminal_claim = []
        finished = threading.Event()

        def execute(run_id, *, store, expected_initial_status, cancellation_control):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            active.set()
            cancellation_control.event.wait(3)
            terminal_claim.append(cancellation_control.claim_terminal())
            cancellation_seen.set()
            self.finish_from_control(store, run_id, cancellation_control)
            finished.set()

        manager = self.replace_manager(execute)
        run = self.create_run("cancellation-wins")
        manager.submit(run["id"])
        self.assertTrue(active.wait(2))
        status, response = self.request("POST", f"/api/executions/{run['id']}/cancel", {})
        self.assertEqual(status, 202)
        self.assertEqual(response["cancellation"]["status"], "cancelling")
        self.assertTrue(cancellation_seen.wait(2))
        self.assertTrue(finished.wait(2))
        self.assertEqual(terminal_claim, [False])
        self.assertEqual(manager.store.load(run["id"])["status"], "cancelled")

    def test_terminal_pending_unknown_malformed_and_unavailable_matrix(self):
        store = self.execution_manager.store
        cancelled = self.create_run("already-cancelled")
        cancelled.update({
            "status": "cancelled",
            "error": None,
            "cancellation": {
                "code": "execution_cancelled", "reason": "user_requested",
                "phase": "queue", "stage": "queue", "requested_at": utc_now(),
                "completed_at": utc_now(),
            },
        })
        store.save(cancelled)
        status, response = self.request("POST", f"/api/executions/{cancelled['id']}/cancel")
        self.assertEqual((status, response["cancellation"]["status"]), (200, "cancelled"))

        for terminal in ("prepared", "success", "failed"):
            with self.subTest(terminal=terminal):
                run = self.create_run(f"terminal-{terminal}")
                run["status"] = terminal
                store.save(run)
                status, response = self.error_response(f"/api/executions/{run['id']}/cancel", {})
                self.assertEqual(status, 409)
                self.assertEqual(response["error"]["code"], "execution_not_cancellable")
                self.assertEqual(response["error"]["details"]["status"], terminal)

        pending = self.create_run("pending-not-owned")
        status, response = self.error_response(f"/api/executions/{pending['id']}/cancel", {})
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["details"]["status"], "pending")
        status, response = self.error_response("/api/executions/missing-run/cancel", {})
        self.assertEqual((status, response["error"]["code"]), (404, "build_run_not_found"))
        status, response = self.error_response("/api/executions/..%2Funsafe/cancel", {})
        self.assertEqual((status, response["error"]["code"]), (400, "invalid_execution_id"))

        server.stop_execution_manager(self.httpd, timeout=5)
        status, response = self.error_response(f"/api/executions/{pending['id']}/cancel", {})
        self.assertEqual((status, response["error"]["code"]), (503, "execution_manager_unavailable"))

    def test_cancellation_request_rejects_pid_signal_and_other_fields(self):
        pending = self.create_run("no-process-api")
        status, response = self.error_response(
            f"/api/executions/{pending['id']}/cancel",
            {"pid": 123, "pgid": 123, "signal": "SIGKILL", "force": True, "reason": "client"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "invalid_cancellation_request")
        self.assertEqual(self.execution_manager.store.load(pending["id"])["status"], "pending")

    def test_cancellation_route_uses_existing_authentication_boundary(self):
        pending = self.create_run("cancel-auth")
        server.AUTH_MODE = "header"
        status, response = self.error_response(f"/api/executions/{pending['id']}/cancel", {})
        self.assertEqual(status, 401)
        self.assertEqual(response["error"], "unauthorized")
        status, response = self.error_response(
            f"/api/executions/{pending['id']}/cancel", {},
            headers={"X-Forwarded-User": "operator"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "execution_not_cancellable")
