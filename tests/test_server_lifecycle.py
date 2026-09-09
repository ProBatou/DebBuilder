import signal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import server as server_entrypoint
from debbuilder import app
from debbuilder import maintenance, workspace_cleanup
from debbuilder.build_store import BuildStore


class FakeManager:
    def __init__(self, events, *, accepting=True, shutdown_result=None):
        self.events = events
        self.accepting = accepting
        self.worker = None
        self.shutdown_result = shutdown_result or {
            "status": "complete",
            "complete": True,
            "worker_joined": True,
            "unresolved_run_ids": [],
        }
        self.shutdown_timeouts = []

    def begin_shutdown(self):
        self.events.append("admission_closed")

    def shutdown(self, timeout):
        self.shutdown_timeouts.append(timeout)
        self.events.append("manager_shutdown")
        if timeout is None and not self.shutdown_result["complete"]:
            return {
                "status": "complete",
                "complete": True,
                "worker_joined": True,
                "unresolved_run_ids": [],
            }
        return self.shutdown_result


class FakeServer:
    def __init__(self, events, serve, *, retention_started=None):
        self.events = events
        self._serve = serve
        self.retention_started = retention_started
        self.closed = False

    def serve_forever(self):
        if self.retention_started is not None:
            if not self.retention_started.wait(2):
                raise AssertionError("retention worker did not start")
        self.events.append("serve")
        return self._serve()

    def server_close(self):
        self.closed = True
        self.events.append("listener_closed")

    def shutdown(self):
        self.events.append("server_shutdown_requested")


class ServerLifecycleTests(unittest.TestCase):
    def lifecycle(self, *, serve=lambda: None, accepting=True, shutdown_result=None, retention_target=None):
        events = []
        retention_started = threading.Event()
        manager = FakeManager(events, accepting=accepting, shutdown_result=shutdown_result)
        server = FakeServer(events, serve, retention_started=retention_started)

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            self.assertIs(http_server, server)
            self.assertIs(selected, manager)
            self.assertFalse(prepare_directories)
            shutdown_check()
            selected.worker = object()
            http_server.execution_manager = selected
            events.append("manager_started")
            return selected

        def retention(stop):
            events.append("retention_started")
            retention_started.set()
            stop.wait()
            events.append("retention_stopped")

        with mock.patch("debbuilder.app.prepare_application_directories", side_effect=lambda: events.append("directories")), \
                mock.patch("debbuilder.app.start_execution_manager", side_effect=start):
            outcome = app.serve_application(
                object(),
                server_factory=lambda *_args: events.append("listener_bound") or server,
                manager_factory=lambda: events.append("manager_constructed") or manager,
                retention_target=retention_target or retention,
                install_signal_handlers=False,
                shutdown_timeout=7,
            )
        return outcome, events, manager, server

    def test_normal_return_uses_one_ordered_lifecycle(self):
        outcome, events, manager, server = self.lifecycle()

        self.assertEqual(outcome, 0)
        self.assertEqual(events[:6], [
            "directories", "manager_constructed", "listener_bound", "manager_started",
            "retention_started", "serve",
        ])
        self.assertLess(events.index("admission_closed"), events.index("manager_shutdown"))
        self.assertLess(events.index("manager_shutdown"), events.index("retention_stopped"))
        self.assertLess(events.index("manager_shutdown"), events.index("listener_closed"))
        self.assertEqual(len(manager.shutdown_timeouts), 1)
        self.assertGreaterEqual(manager.shutdown_timeouts[0], 0)
        self.assertLessEqual(manager.shutdown_timeouts[0], 7)
        self.assertTrue(server.closed)
        self.assertFalse(any(thread.name == "storage-maintenance" for thread in threading.enumerate()))

    def test_unexpected_server_exception_is_preserved_when_cleanup_also_incomplete(self):
        failure = RuntimeError("server loop failed")
        incomplete = {
            "status": "incomplete",
            "complete": False,
            "worker_joined": False,
            "unresolved_run_ids": ["queued"],
        }

        with self.assertLogs("debbuilder.app", level="ERROR"), self.assertRaises(RuntimeError) as raised:
            self.lifecycle(serve=lambda: (_ for _ in ()).throw(failure), shutdown_result=incomplete)

        self.assertIs(raised.exception, failure)
        self.assertIn("Cleanup also failed during execution manager shutdown", "\n".join(failure.__notes__))

    def test_incomplete_manager_shutdown_returns_nonzero(self):
        incomplete = {
            "status": "incomplete",
            "complete": False,
            "worker_joined": False,
            "unresolved_run_ids": [],
        }
        with self.assertLogs("debbuilder.app", level="ERROR"):
            outcome, _events, _manager, _server = self.lifecycle(shutdown_result=incomplete)
        self.assertEqual(outcome, 1)

    def test_recovery_blocked_manager_still_starts_read_only_maintenance(self):
        outcome, events, _manager, _server = self.lifecycle(accepting=False)

        self.assertEqual(outcome, 0)
        self.assertIn("retention_started", events)
        self.assertIn("retention_stopped", events)

    def test_clean_startup_requests_one_asynchronous_cleanup(self):
        events = []
        manager = FakeManager(events)
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        requests = []

        class Inventory:
            def collect(self):
                return {"state": "ready"}

        class RecordingService(maintenance.MaintenanceService):
            def request(self, *, refresh=True, cleanup=False):
                requests.append((refresh, cleanup))
                return super().request(refresh=refresh, cleanup=cleanup)

        def create_maintenance(_http_server):
            return RecordingService(
                Inventory(),
                cleanup=lambda: cleanup_entered.set() or release_cleanup.wait(2),
                refresh_interval=3600,
            )

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            selected.worker = object()
            http_server.execution_manager = selected
            return selected

        def serve():
            self.assertTrue(cleanup_entered.wait(2))
            events.append("served_while_cleanup_active")
            release_cleanup.set()

        server = FakeServer(events, serve)
        with mock.patch("debbuilder.app.prepare_application_directories"), \
                mock.patch("debbuilder.app.start_execution_manager", side_effect=start):
            outcome = app.serve_application(
                object(),
                server_factory=lambda *_args: server,
                manager_factory=lambda: manager,
                maintenance_factory=create_maintenance,
                install_signal_handlers=False,
            )

        self.assertEqual(outcome, 0)
        self.assertEqual(requests, [(True, True)])
        self.assertIn("served_while_cleanup_active", events)
        self.assertFalse(any(
            thread.name == "storage-maintenance" for thread in threading.enumerate()
        ))

    def test_startup_cleanup_obeys_global_and_resolved_recovery_authorization(self):
        for blocked in (True, False):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as temporary:
                events = []
                manager = FakeManager(events, accepting=not blocked)
                store = BuildStore(Path(temporary) / "builds")
                run = store.create({
                    "name": "recovered",
                    "package": {
                        "name": "recovered",
                        "maintainer": "A <a@example.test>",
                        "description": "A",
                    },
                    "source": {"repository": "owner/recovered"},
                }, mode="build", run_id="recovered")
                source = Path(run["workspace"]) / "source/evidence"
                source.write_text("evidence")
                run["status"] = "failed"
                run["recovery"] = {
                    "status": "blocked" if blocked else "resolved",
                    "code": "execution_recovery_unresolved" if blocked else "execution_interrupted",
                    "backend": "systemd_cgroup",
                    "reason": "ownership unresolved" if blocked else "workload absent",
                    "resolved_at": None if blocked else "2026-09-09T00:00:00+00:00",
                }
                store.save(run)
                cleanup_finished = threading.Event()
                inventory_finished = threading.Event()

                class Inventory:
                    def collect(self):
                        inventory_finished.set()
                        return {"state": "ready"}

                def create_maintenance(http_server):
                    service = None

                    def cleanup():
                        try:
                            return workspace_cleanup.apply_retention(
                                store,
                                {"failed_workspaces_to_retain": 0},
                                authorization=http_server.cleanup_authorization,
                                should_stop=service.stop_requested,
                            )
                        finally:
                            cleanup_finished.set()

                    service = maintenance.MaintenanceService(
                        Inventory(), cleanup=cleanup, refresh_interval=3600,
                    )
                    return service

                def start(http_server, selected, *, prepare_directories, shutdown_check):
                    if blocked:
                        http_server.cleanup_authorization.update_global_blocker({
                            "code": "execution_recovery_unresolved",
                            "message": "global recovery remains unresolved",
                        })
                    selected.worker = object()
                    http_server.execution_manager = selected
                    return selected

                def serve():
                    self.assertTrue(cleanup_finished.wait(2))
                    self.assertTrue(inventory_finished.wait(2))

                server = FakeServer(events, serve)
                with mock.patch("debbuilder.app.prepare_application_directories"), \
                        mock.patch("debbuilder.app.start_execution_manager", side_effect=start):
                    outcome = app.serve_application(
                        object(),
                        server_factory=lambda *_args: server,
                        manager_factory=lambda: manager,
                        maintenance_factory=create_maintenance,
                        install_signal_handlers=False,
                    )

                self.assertEqual(outcome, 0)
                self.assertEqual(source.exists(), blocked)

    def test_server_bind_failure_never_starts_or_shutdowns_manager(self):
        manager = FakeManager([])
        failure = OSError("address unavailable")
        with mock.patch("debbuilder.app.prepare_application_directories"), \
                mock.patch("debbuilder.app.start_execution_manager") as start, \
                self.assertRaises(OSError) as raised:
            app.serve_application(
                object(), server_factory=lambda *_args: (_ for _ in ()).throw(failure),
                manager_factory=lambda: manager, install_signal_handlers=False,
            )

        self.assertIs(raised.exception, failure)
        start.assert_not_called()
        self.assertEqual(manager.events, [])

    def test_startup_failure_before_worker_start_closes_only_listener(self):
        events = []
        manager = FakeManager(events)
        server = FakeServer(events, lambda: None)
        failure = RuntimeError("recipe startup failed")
        with mock.patch("debbuilder.app.prepare_application_directories"), \
                mock.patch("debbuilder.app.start_execution_manager", side_effect=failure), \
                self.assertRaises(RuntimeError) as raised:
            app.serve_application(
                object(), server_factory=lambda *_args: server,
                manager_factory=lambda: manager, install_signal_handlers=False,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(events, ["listener_closed"])

    def test_auxiliary_start_failure_unwinds_started_manager(self):
        events = []
        manager = FakeManager(events)
        server = FakeServer(events, lambda: None)
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread start failed")
        thread.is_alive.return_value = False

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            selected.worker = object()
            http_server.execution_manager = selected
            events.append("manager_started")
            return selected

        with mock.patch("debbuilder.app.prepare_application_directories"), \
                mock.patch("debbuilder.app.start_execution_manager", side_effect=start), \
                mock.patch("debbuilder.app.threading.Thread", return_value=thread), \
                self.assertRaisesRegex(RuntimeError, "thread start failed"):
            app.serve_application(
                object(), server_factory=lambda *_args: server,
                manager_factory=lambda: manager, install_signal_handlers=False,
            )

        self.assertEqual(events, ["manager_started", "admission_closed", "manager_shutdown", "listener_closed"])

    def test_maintenance_quiesces_after_run_shutdown_even_after_target_expires(self):
        events = []
        manager = FakeManager(events)
        retention_started = threading.Event()
        retention_stop_requested = threading.Event()
        retention_join_attempted = threading.Event()
        retention_ownership_continued = threading.Event()
        release_retention = threading.Event()
        server = FakeServer(events, lambda: None, retention_started=retention_started)

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            selected.worker = object()
            http_server.execution_manager = selected
            return selected

        def retention(stop):
            retention_started.set()
            self.assertTrue(stop.wait(2))
            retention_stop_requested.set()
            release_retention.wait()
            events.append("retention_stopped")

        outcome = {}
        errors = []
        real_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            selected = real_thread(*args, **kwargs)
            if kwargs.get("name") == "storage-maintenance":
                original_join = selected.join

                def join(timeout=None):
                    if timeout is not None:
                        retention_join_attempted.set()
                    else:
                        retention_ownership_continued.set()
                    return original_join(timeout)

                selected.join = join
            return selected

        def run_lifecycle():
            try:
                outcome["value"] = app.serve_application(
                    object(), server_factory=lambda *_args: server,
                    manager_factory=lambda: manager, retention_target=retention,
                    install_signal_handlers=False, shutdown_timeout=0,
                )
            except BaseException as exc:
                errors.append(exc)

        lifecycle_thread = real_thread(target=run_lifecycle)
        with self.assertLogs("debbuilder.app", level="ERROR"):
            with mock.patch("debbuilder.app.prepare_application_directories"), \
                    mock.patch("debbuilder.app.start_execution_manager", side_effect=start), \
                    mock.patch("debbuilder.app.threading.Thread", side_effect=thread_factory):
                lifecycle_thread.start()
                self.assertTrue(retention_started.wait(2))
                self.assertTrue(retention_stop_requested.wait(2))
                self.assertTrue(retention_join_attempted.wait(2))
                self.assertTrue(retention_ownership_continued.wait(2))
                self.assertIn("manager_shutdown", events)
                self.assertTrue(lifecycle_thread.is_alive())
                release_retention.set()
                lifecycle_thread.join(3)

        self.assertEqual(errors, [])
        self.assertEqual(outcome["value"], 1)
        self.assertLess(events.index("manager_shutdown"), events.index("retention_stopped"))
        if lifecycle_thread.is_alive():
            release_retention.set()
            lifecycle_thread.join(2)

    def test_http_mutation_ownership_continues_after_target_before_manager_shutdown(self):
        events = []
        manager = FakeManager(events, accepting=False)
        mutation_started = threading.Event()
        release_mutation = threading.Event()
        mutation_thread = None

        def serve():
            nonlocal mutation_thread

            def mutate():
                with server.mutation_gate.lease():
                    mutation_started.set()
                    release_mutation.wait()
                    events.append("mutation_finished")

            mutation_thread = threading.Thread(target=mutate, name="test-http-mutation")
            mutation_thread.start()
            self.assertTrue(mutation_started.wait(2))

        server = FakeServer(events, serve)

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            selected.worker = object()
            http_server.execution_manager = selected
            return selected

        outcome = {}
        lifecycle_thread = threading.Thread(target=lambda: outcome.update(value=app.serve_application(
            object(),
            server_factory=lambda *_args: server,
            manager_factory=lambda: manager,
            retention_target=None,
            install_signal_handlers=False,
            shutdown_timeout=0,
        )))
        try:
            with self.assertLogs("debbuilder.app", level="ERROR"), \
                    mock.patch("debbuilder.app.prepare_application_directories"), \
                    mock.patch("debbuilder.app.start_execution_manager", side_effect=start):
                lifecycle_thread.start()
                self.assertTrue(mutation_started.wait(2))
                lifecycle_thread.join(0.05)
                # The lifecycle must not claim manager/server completion while
                # a daemon-capable request still owns a mutation.
                self.assertTrue(lifecycle_thread.is_alive())
                self.assertNotIn("manager_shutdown", events)
                self.assertNotIn("listener_closed", events)
                release_mutation.set()
                lifecycle_thread.join(3)
        finally:
            release_mutation.set()
            lifecycle_thread.join(2)
            if mutation_thread is not None:
                mutation_thread.join(2)

        self.assertEqual(outcome["value"], 1)
        self.assertLess(events.index("mutation_finished"), events.index("manager_shutdown"))
        self.assertLess(events.index("manager_shutdown"), events.index("listener_closed"))

    def test_signal_handler_wakes_for_repeated_and_mixed_signals(self):
        read_fd, write_fd = app.os.pipe()
        app.os.set_blocking(write_fd, False)
        restore = app._install_shutdown_signal_handlers(write_fd)
        try:
            term = signal.getsignal(signal.SIGTERM)
            interrupt = signal.getsignal(signal.SIGINT)
            term(signal.SIGTERM, None)
            interrupt(signal.SIGINT, None)
            term(signal.SIGTERM, None)
            self.assertEqual(app.os.read(read_fd, 3), bytes((signal.SIGTERM, signal.SIGINT, signal.SIGTERM)))
        finally:
            restore()
            app.os.close(write_fd)
            app.os.close(read_fd)

    def test_signal_infrastructure_precedes_stateful_startup(self):
        manager_factory = mock.Mock()
        observed = []

        def directories():
            handler = signal.getsignal(signal.SIGTERM)
            observed.append(callable(handler))
            handler(signal.SIGTERM, None)

        with mock.patch("debbuilder.app.prepare_application_directories", side_effect=directories):
            outcome = app.serve_application(object(), manager_factory=manager_factory)

        self.assertEqual(outcome, 0)
        self.assertEqual(observed, [True])
        manager_factory.assert_not_called()
        self.assertFalse(any(
            thread.name == "signal-shutdown-coordinator" for thread in threading.enumerate()
        ))

    def test_full_self_pipe_still_records_a_pending_shutdown_request(self):
        read_fd, write_fd = app.os.pipe()
        app.os.set_blocking(write_fd, False)
        requested = [False]
        restore = app._install_shutdown_signal_handlers(write_fd, requested_state=requested)
        try:
            while True:
                try:
                    app.os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(requested[0])
            self.assertEqual(app.os.read(read_fd, 1), b"x")
        finally:
            restore()
            app.os.close(write_fd)
            app.os.close(read_fd)

    def test_signal_racing_normal_server_return_cannot_interrupt_cleanup(self):
        read_fd, write_fd = app.os.pipe()
        cleanup_started = threading.Event()
        restore = app._install_shutdown_signal_handlers(write_fd)
        try:
            term = signal.getsignal(signal.SIGTERM)
            cleanup_started.set()
            term(signal.SIGTERM, None)
            self.assertTrue(cleanup_started.is_set())
            self.assertEqual(app.os.read(read_fd, 1), bytes((signal.SIGTERM,)))
        finally:
            restore()
            app.os.close(write_fd)
            app.os.close(read_fd)

    def test_signal_and_normal_server_return_converge_on_one_cleanup(self):
        events = []
        manager = FakeManager(events, accepting=False)

        def serve():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        server = FakeServer(events, serve)

        def start(http_server, selected, *, prepare_directories, shutdown_check):
            selected.worker = object()
            http_server.execution_manager = selected
            return selected

        with mock.patch("debbuilder.app.prepare_application_directories"), \
                mock.patch("debbuilder.app.start_execution_manager", side_effect=start):
            outcome = app.serve_application(
                object(), server_factory=lambda *_args: server,
                manager_factory=lambda: manager,
            )

        self.assertEqual(outcome, 0)
        self.assertTrue(server.closed)
        self.assertIn("manager_shutdown", events)
        self.assertFalse(any(
            thread.name == "signal-shutdown-coordinator" for thread in threading.enumerate()
        ))

    def test_sigint_is_an_equivalent_first_shutdown_request(self):
        read_fd, write_fd = app.os.pipe()
        restore = app._install_shutdown_signal_handlers(write_fd)
        try:
            interrupt = signal.getsignal(signal.SIGINT)
            interrupt(signal.SIGINT, None)
            self.assertEqual(app.os.read(read_fd, 1), bytes((signal.SIGINT,)))
        finally:
            restore()
            app.os.close(write_fd)
            app.os.close(read_fd)

    def test_both_executable_entrypoints_delegate_to_shared_lifecycle(self):
        with mock.patch("debbuilder.app.serve_application", return_value=7) as serve:
            self.assertEqual(app.main(), 7)
            serve.assert_called_once_with(app.Handler)
        with mock.patch("debbuilder.app.serve_application", return_value=9) as serve:
            self.assertEqual(server_entrypoint.main(), 9)
            serve.assert_called_once_with(server_entrypoint.Handler)


if __name__ == "__main__":
    unittest.main()
