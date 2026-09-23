"""Test-only setup and inspection for partial services and partial Run services."""
from __future__ import annotations

from debbuilder import app, command_containment, dependency_preparation, workspace_cleanup
from debbuilder.lifecycle import MutationGate


def stop_partial_manager(http_server, *, timeout=None):
    """Tear down a test server that did not enter serve_application's lifecycle."""
    validation_manager = getattr(http_server, "validation_manager", None)
    if validation_manager is not None:
        validation_manager.begin_shutdown()
    if not dependency_preparation.SUPERVISOR.shutdown(timeout):
        raise TimeoutError("Validation dependency preparations did not stop before the timeout")
    if validation_manager is not None and not validation_manager.shutdown(timeout):
        raise TimeoutError("Validation manager did not stop before the timeout")
    mutation_gate = getattr(http_server, "mutation_gate", None)
    if mutation_gate is not None:
        mutation_gate.begin_shutdown()
        if not mutation_gate.wait_for_quiescence(timeout)["complete"]:
            raise TimeoutError("Durable HTTP mutations did not stop before the timeout")
    manager = getattr(http_server, "execution_manager", None)
    if manager is not None:
        manager.stop(timeout=timeout)
    http_server.execution_manager = None
    http_server.validation_manager = None
    if app.APPLICATION_VALIDATION_MANAGER is validation_manager:
        app.APPLICATION_VALIDATION_MANAGER = None
    http_server.mutation_gate = MutationGate()


@command_containment.containment_safety_serialized
def clean_workspace(store, run_id, *, reason="manual", authorization=workspace_cleanup.OPEN_CLEANUP_AUTHORIZATION):
    authorization.require_global()
    with store.locked_run(run_id, blocking=False) as fd:
        run = workspace_cleanup.read_run(fd, store.root, run_id)
        return workspace_cleanup._clean_locked(
            fd, run, reason=reason, authorization=authorization,
        )


def cleanup_blockers():
    with command_containment._CLEANUP_GATE_LOCK:
        return tuple(
            command_containment._CLEANUP_BLOCKERS[key]
            for key in sorted(command_containment._CLEANUP_BLOCKERS)
        )


def active_cancellation_control(manager):
    with manager._condition:
        return manager._active_cancellation_control


def in_flight_count(service):
    with service._lock:
        return len(service._in_flight)
