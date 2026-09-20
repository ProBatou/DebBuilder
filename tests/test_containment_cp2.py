"""CP2 runtime blocker reconciliation and atomic admission recovery."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app
import debbuilder.command_containment as containment
from debbuilder.automation_identity import automation_attempt_key, normalize_upstream_identity
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.execution_manager import ExecutionManager
from debbuilder.resource_limits import empty_policy


def recipe(name="cp2-recipe"):
    return {
        "schema_version": 3,
        "name": name,
        "active": True,
        "resource_limits": empty_policy(),
        "package": {"name": name},
        "source": {"repository": f"owner/{name}"},
    }


def identity(run_id, command, invocation):
    value = containment.starting_metadata(run_id, command * 32)
    value.update({
        "invocation_id": invocation * 32,
        "control_group": containment.expected_control_group(value["unit_name"]),
        "containment_state": "active",
    })
    return value


def proof(status, reason="test proof"):
    return containment.AbsenceProof(status, reason)


class CleanupReconciliationTests(unittest.TestCase):
    def setUp(self):
        blockers = mock.patch.object(containment, "_CLEANUP_BLOCKERS", {})
        blockers.start()
        self.addCleanup(blockers.stop)
        generations = mock.patch.object(containment, "_CLEANUP_BLOCKER_GENERATIONS", {})
        generations.start()
        self.addCleanup(generations.stop)
        revision = mock.patch.object(containment, "_CLEANUP_BLOCKER_REVISION", 0)
        revision.start()
        self.addCleanup(revision.stop)
        saturation = mock.patch.object(containment, "_CLEANUP_BLOCKERS_SATURATED", False)
        saturation.start()
        self.addCleanup(saturation.stop)

    def publish(self, kind, run_id, command, invocation):
        value = identity(run_id, command, invocation)
        containment._publish_cleanup_blocker(kind, f"{run_id} unresolved", value)
        return value

    def test_only_proven_gone_exact_blockers_are_removed_independently(self):
        gone = self.publish("runtime", "gone", "a", "1")
        present = self.publish("runtime", "present", "b", "2")
        ambiguous = self.publish("probe", "ambiguous", "c", "3")
        unavailable = self.publish("probe", "unavailable", "d", "4")
        statuses = {
            gone["unit_name"]: containment.AbsenceStatus.PROVEN_GONE,
            present["unit_name"]: containment.AbsenceStatus.STILL_PRESENT_AND_OWNED,
            ambiguous["unit_name"]: containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS,
            unavailable["unit_name"]: containment.AbsenceStatus.PROOF_UNAVAILABLE,
        }
        with mock.patch.object(
            containment, "prove_containment_absent",
            side_effect=lambda blocker: proof(statuses[blocker.unit_name]),
        ):
            result = containment.reconcile_cleanup_blockers()

        self.assertEqual((result.examined, result.removed, result.remaining), (4, 1, 3))
        self.assertEqual((result.proven_gone, result.still_present, result.ambiguous, result.unavailable),
                         (1, 1, 1, 1))
        self.assertNotIn(gone["unit_name"], {item.unit_name for item in containment.cleanup_blockers()})
        self.assertEqual({item.kind for item in containment.cleanup_blockers()}, {"runtime", "probe"})
        self.assertTrue(result.admission_blocked)

    def test_two_runtime_blockers_reconcile_across_bounded_passes(self):
        first = self.publish("runtime", "first-runtime", "a", "1")
        second = self.publish("runtime", "second-runtime", "b", "2")
        states = {
            first["unit_name"]: containment.AbsenceStatus.PROVEN_GONE,
            second["unit_name"]: containment.AbsenceStatus.STILL_PRESENT_AND_OWNED,
        }
        with mock.patch.object(
            containment, "prove_containment_absent",
            side_effect=lambda blocker: proof(states[blocker.unit_name]),
        ):
            first_pass = containment.reconcile_cleanup_blockers()
            self.assertEqual((first_pass.removed, first_pass.remaining), (1, 1))
            states[second["unit_name"]] = containment.AbsenceStatus.PROVEN_GONE
            second_pass = containment.reconcile_cleanup_blockers()
        self.assertEqual((second_pass.removed, second_pass.remaining), (1, 0))

    def test_stale_proof_cannot_remove_same_key_republication(self):
        published = self.publish("runtime", "same-key", "a", "1")
        key, original = next(iter(containment._CLEANUP_BLOCKERS.items()))
        original_generation = containment._CLEANUP_BLOCKER_GENERATIONS[key]
        proof_started = threading.Event()
        proof_release = threading.Event()

        def prove(_blocker):
            proof_started.set()
            self.assertTrue(proof_release.wait(2))
            return proof(containment.AbsenceStatus.PROVEN_GONE)

        completed = []
        with mock.patch.object(containment, "prove_containment_absent", side_effect=prove):
            thread = threading.Thread(
                target=lambda: completed.append(containment.reconcile_cleanup_blockers())
            )
            thread.start()
            self.assertTrue(proof_started.wait(2))
            containment._publish_cleanup_blocker(
                "runtime", "republished record", published,
            )
            proof_release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual((completed[0].removed, completed[0].stale), (0, 1))
        self.assertEqual(containment.cleanup_blockers(), (original,))
        self.assertGreater(containment._CLEANUP_BLOCKER_GENERATIONS[key], original_generation)

    def test_new_publication_during_proof_remains(self):
        first = self.publish("runtime", "first", "a", "1")
        proof_started = threading.Event()
        proof_release = threading.Event()

        def prove(_blocker):
            proof_started.set()
            self.assertTrue(proof_release.wait(2))
            return proof(containment.AbsenceStatus.PROVEN_GONE)

        completed = []
        with mock.patch.object(containment, "prove_containment_absent", side_effect=prove):
            thread = threading.Thread(
                target=lambda: completed.append(containment.reconcile_cleanup_blockers())
            )
            thread.start()
            self.assertTrue(proof_started.wait(2))
            second = self.publish("probe", "second", "b", "2")
            proof_release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0].removed, 1)
        self.assertEqual([item.unit_name for item in containment.cleanup_blockers()], [second["unit_name"]])
        self.assertNotEqual(first["unit_name"], second["unit_name"])

    def test_saturation_remains_fail_closed_after_all_stored_records_resolve(self):
        self.publish("runtime", "stored", "a", "1")
        containment._CLEANUP_BLOCKERS_SATURATED = True
        with mock.patch.object(
            containment, "prove_containment_absent",
            return_value=proof(containment.AbsenceStatus.PROVEN_GONE),
        ):
            result = containment.reconcile_cleanup_blockers()
        self.assertEqual((result.removed, result.remaining), (1, 0))
        self.assertTrue(result.saturated)
        self.assertTrue(result.admission_blocked)
        self.assertIn("capacity exceeded", containment.containment_cleanup_blocker())

    def test_unexpected_proof_failure_keeps_that_blocker_but_commits_safe_progress(self):
        gone = self.publish("runtime", "a-gone", "a", "1")
        failed = self.publish("runtime", "z-failed", "b", "2")

        def prove_one(blocker):
            if blocker.unit_name == gone["unit_name"]:
                return proof(containment.AbsenceStatus.PROVEN_GONE)
            raise RuntimeError("unexpected proof bug")

        with mock.patch.object(containment, "prove_containment_absent", side_effect=prove_one), \
             self.assertLogs("debbuilder.command_containment", level="ERROR"):
            result = containment.reconcile_cleanup_blockers()
        self.assertEqual((result.removed, result.unavailable, result.remaining), (1, 1, 1))
        self.assertEqual(containment.cleanup_blockers()[0].unit_name, failed["unit_name"])

    def test_property_mismatch_remains_then_natural_disappearance_reopens(self):
        value = self.publish("runtime", "mismatch", "a", "1")
        snapshot = containment.UnitSnapshot(
            unit_name=value["unit_name"],
            description=containment.unit_description(value["run_id"], value["command_id"]),
            transient=True, invocation_id=value["invocation_id"],
            control_group=value["control_group"], active_state="active", sub_state="running",
            service_type="exec", exit_type="cgroup", kill_mode="process", result="success",
            main_pid=123, exec_main_code=0, exec_main_status=0,
        )

        class Connection:
            def __init__(self, current):
                self.current = current
            def snapshot(self, _name):
                return self.current
            def close(self):
                pass

        connection = Connection(snapshot)
        canonical_proof = containment.prove_containment_absent
        with mock.patch.object(
            containment, "prove_containment_absent",
            side_effect=lambda blocker: canonical_proof(
                blocker, connection_factory=lambda: connection,
            ),
        ), mock.patch.object(containment, "_cgroup_is_absent", return_value=True):
            first = containment.reconcile_cleanup_blockers()
            self.assertEqual((first.ambiguous, first.removed), (1, 0))
            connection.current = None
            second = containment.reconcile_cleanup_blockers()
        self.assertEqual((second.proven_gone, second.removed, second.remaining), (1, 1, 0))

    def test_new_same_name_incarnation_appearing_during_proof_is_ambiguous(self):
        value = identity("aba", "a", "1")
        replacement = containment.UnitSnapshot(
            unit_name=value["unit_name"],
            description=containment.unit_description(value["run_id"], value["command_id"]),
            transient=True, invocation_id="2" * 32,
            control_group=value["control_group"], active_state="active", sub_state="running",
            service_type="exec", exit_type="cgroup", kill_mode="control-group", result="success",
            main_pid=456, exec_main_code=0, exec_main_status=0,
        )

        class Connection:
            def __init__(self):
                self.observations = iter((None, replacement))
                self.closed = False
            def snapshot(self, _name):
                return next(self.observations)
            def close(self):
                self.closed = True

        connection = Connection()
        with mock.patch.object(containment, "_cgroup_is_absent", return_value=True):
            result = containment.prove_containment_absent(
                value, connection_factory=lambda: connection,
            )
        self.assertEqual(result.status, containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS)
        self.assertTrue(connection.closed)

    def test_resolved_probe_failure_reproves_cached_unresolved_capability(self):
        available = containment.ContainmentCapability("systemd_cgroup", True, "verified")
        with mock.patch.object(
            containment, "_CAPABILITY",
            containment.ContainmentCapability("unresolved", False, "old probe cleanup"),
        ), mock.patch.object(containment, "_probe_capability", return_value=available) as probe:
            result = containment.containment_capability()
        self.assertEqual(result, available)
        probe.assert_called_once_with()


class AdmissionReconciliationTests(unittest.TestCase):
    def setUp(self):
        blockers = mock.patch.object(containment, "_CLEANUP_BLOCKERS", {})
        blockers.start()
        self.addCleanup(blockers.stop)
        generations = mock.patch.object(containment, "_CLEANUP_BLOCKER_GENERATIONS", {})
        generations.start()
        self.addCleanup(generations.stop)
        revision = mock.patch.object(containment, "_CLEANUP_BLOCKER_REVISION", 0)
        revision.start()
        self.addCleanup(revision.stop)
        saturation = mock.patch.object(containment, "_CLEANUP_BLOCKERS_SATURATED", False)
        saturation.start()
        self.addCleanup(saturation.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / "data"
        self.data.mkdir()
        self.old_data = app.DATA
        app.DATA = self.data
        self.addCleanup(setattr, app, "DATA", self.old_data)
        self.managers = []

    def manager(self, execute=None):
        if execute is None:
            def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
                store.transition_status(run_id, expected=expected_initial_status, status="running")
                with store.locked_run(run_id):
                    run = store.load(run_id)
                    run["status"] = "failed"
                    run["error"] = {
                        "code": "command_containment_termination_failed",
                        "message": "historical containment failure",
                    }
                    store.save(run)
                return {"run_id": run_id, "status": "failed"}
        manager = ExecutionManager(BuildStore(self.data / "builds"), execute=execute)
        manager.start()
        self.managers.append(manager)
        return manager

    def tearDown(self):
        for manager in self.managers:
            manager.stop(timeout=5)

    @staticmethod
    def capability(_policy, *, workspace, refresh=False):
        return {
            "backend": "systemd_cgroup", "available": True,
            "requested_controls": [], "reason": "verified",
        }

    def publish(self, kind="runtime", run_id="blocked", command="a", invocation="1"):
        value = identity(run_id, command, invocation)
        containment._publish_cleanup_blocker(kind, "cleanup unresolved", value)
        return value

    def test_unrelated_build_and_test_resume_without_restart_and_keep_history(self):
        historical_done = threading.Event()

        def fail_with_history(run_id, *, store, expected_initial_status, cancellation_control=None):
            store.transition_status(run_id, expected=expected_initial_status, status="running")
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "failed"
                run["error"] = {
                    "code": "command_containment_termination_failed",
                    "message": "KillMode expected=control-group observed=process",
                }
                store.save(run)
            historical_done.set()
            return {"run_id": run_id, "status": "failed"}

        manager = self.manager(execute=fail_with_history)
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability):
            historical_response = app.enqueue_recipe_run(
                manager, recipe("seerr-like"), dry_run=False,
            )
        self.assertTrue(historical_done.wait(2))
        historical_before = manager.store.load(historical_response["run_id"])
        self.publish(run_id="seerr-like")
        statuses = [
            containment.AbsenceStatus.PRESENT_BUT_IDENTITY_AMBIGUOUS,
            containment.AbsenceStatus.PROVEN_GONE,
        ]
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(
                 containment, "prove_containment_absent",
                 side_effect=lambda _blocker: proof(statuses.pop(0)),
             ):
            with self.assertRaises(app.RunAdmissionError) as blocked:
                app.enqueue_recipe_run(manager, recipe("zoraxy-like"), dry_run=False)
            self.assertEqual(blocked.exception.code, "execution_recovery_unresolved")
            build = app.enqueue_recipe_run(manager, recipe("zoraxy-like"), dry_run=False)

        self.assertEqual(build["status"], "queued")
        historical_after = manager.store.load(historical_response["run_id"])
        self.assertEqual(historical_after, historical_before)
        self.assertEqual(historical_after["status"], "failed")
        self.assertIn("KillMode expected=control-group observed=process",
                      historical_after["error"]["message"])
        self.assertFalse(containment.containment_cleanup_blocker())

        self.publish(run_id="test-blocked", command="b", invocation="2")
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(
                 containment, "prove_containment_absent",
                 return_value=proof(containment.AbsenceStatus.PROVEN_GONE),
             ):
            test = app.enqueue_recipe_run(manager, recipe("same-test-path"), dry_run=True)
        self.assertEqual(test["status"], "queued")

    def test_transient_proof_failure_stays_canonical_and_later_request_retries(self):
        manager = self.manager()
        self.publish()
        statuses = [containment.AbsenceStatus.PROOF_UNAVAILABLE, containment.AbsenceStatus.PROVEN_GONE]
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(
                 containment, "prove_containment_absent",
                 side_effect=lambda _blocker: proof(statuses.pop(0)),
             ):
            with self.assertRaises(app.RunAdmissionError) as blocked:
                app.enqueue_recipe_run(manager, recipe(), dry_run=True)
            resumed = app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual((blocked.exception.status, blocked.exception.code),
                         (503, "execution_recovery_unresolved"))
        self.assertEqual(resumed["status"], "queued")

    def test_runtime_and_probe_converge_independently(self):
        manager = self.manager()
        runtime = self.publish("runtime", "runtime", "a", "1")
        probe_blocker = self.publish("probe", "probe", "b", "2")
        states = {
            runtime["unit_name"]: containment.AbsenceStatus.PROVEN_GONE,
            probe_blocker["unit_name"]: containment.AbsenceStatus.PROOF_UNAVAILABLE,
        }
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(
                 containment, "prove_containment_absent",
                 side_effect=lambda blocker: proof(states[blocker.unit_name]),
             ):
            with self.assertRaises(app.RunAdmissionError):
                app.enqueue_recipe_run(manager, recipe(), dry_run=True)
            self.assertEqual([(item.kind, item.unit_name) for item in containment.cleanup_blockers()],
                             [("probe", probe_blocker["unit_name"])])
            states[probe_blocker["unit_name"]] = containment.AbsenceStatus.PROVEN_GONE
            resumed = app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(resumed["status"], "queued")

    def test_new_blocker_during_admission_is_observed_after_one_pass(self):
        manager = self.manager()
        self.publish("runtime", "old", "a", "1")
        new_identity = identity("new", "b", "2")

        def capability(*args, **kwargs):
            containment._publish_cleanup_blocker("probe", "new failure", new_identity)
            return self.capability(*args, **kwargs)

        with mock.patch.object(
            containment, "prove_containment_absent",
            return_value=proof(containment.AbsenceStatus.PROVEN_GONE),
        ) as prove_absent, mock.patch.object(
            app.command_containment, "resource_limit_capability", side_effect=capability,
        ), self.assertRaises(app.RunAdmissionError) as blocked:
            app.enqueue_recipe_run(manager, recipe(), dry_run=False)
        self.assertEqual(blocked.exception.code, "execution_recovery_unresolved")
        self.assertEqual(prove_absent.call_count, 1)
        self.assertEqual(containment.cleanup_blockers()[0].unit_name, new_identity["unit_name"])
        self.assertFalse(manager.store.root.exists())

    def test_final_admission_and_publication_are_serialized(self):
        manager = self.manager()
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability):
            canonical, contract = app._prepare_run_admission(manager, recipe())
        create_entered = threading.Event()
        create_release = threading.Event()
        publication_done = threading.Event()
        original_create = app.build_pipeline.create_pipeline_run

        def blocking_create(*args, **kwargs):
            create_entered.set()
            self.assertTrue(create_release.wait(2))
            return original_create(*args, **kwargs)

        admitted = []
        with mock.patch.object(app.build_pipeline, "create_pipeline_run", side_effect=blocking_create):
            admission = threading.Thread(target=lambda: admitted.append(
                app._enqueue_prepared_recipe_run(
                    manager, canonical, contract, dry_run=True,
                )
            ))
            admission.start()
            self.assertTrue(create_entered.wait(2))
            publisher = threading.Thread(target=lambda: (
                self.publish("runtime", "racing", "c", "3"), publication_done.set(),
            ))
            publisher.start()
            self.assertFalse(publication_done.wait(0.1))
            create_release.set()
            admission.join(2)
            publisher.join(2)
        self.assertFalse(admission.is_alive())
        self.assertFalse(publisher.is_alive())
        self.assertEqual(admitted[0]["status"], "queued")
        self.assertTrue(publication_done.is_set())
        self.assertTrue(containment.containment_cleanup_blocker())

    def test_shutdown_during_reconciliation_does_not_start_a_run(self):
        executed = threading.Event()
        manager = self.manager(execute=lambda *args, **kwargs: executed.set())
        self.publish()
        proof_started = threading.Event()
        proof_release = threading.Event()

        def prove_absent(_blocker):
            proof_started.set()
            self.assertTrue(proof_release.wait(2))
            return proof(containment.AbsenceStatus.PROVEN_GONE)

        failures = []
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(containment, "prove_containment_absent", side_effect=prove_absent):
            admission = threading.Thread(target=lambda: self._capture_failure(
                failures, lambda: app.enqueue_recipe_run(manager, recipe(), dry_run=True),
            ))
            admission.start()
            self.assertTrue(proof_started.wait(2))
            manager.begin_shutdown()
            proof_release.set()
            admission.join(2)
        self.assertFalse(admission.is_alive())
        self.assertEqual(failures[0].code, "execution_manager_unavailable")
        self.assertFalse(executed.is_set())
        self.assertFalse(manager.store.root.exists())

    def test_preallocated_automation_retries_exact_run_without_duplication(self):
        manager = self.manager()
        self.publish()
        run_id = "preallocated-cp2-run"
        workflow = {
            "schema_version": 5, "name": "automated-cp2", "active": True,
            "automation": {"enabled": True, "policy": "build"},
            "package": {"name": "automated-cp2", "architecture": "amd64"},
            "source": {"repository": "owner/automated-cp2", "tracking": "latest_release"},
        }
        origin = {"kind": "automation", "trigger": "upstream_change", "reason": "new_release"}
        upstream_identity = normalize_upstream_identity({
            "provider": "github", "repository": "owner/automated-cp2",
            "tracking": "latest_release", "source_type": "release_asset",
            "payload_kind": "deb", "release_id": "10", "asset_id": "20",
            "asset_name": "automated-cp2_1.0_amd64.deb", "expected_size": 7,
            "resolved_ref": "v1.0", "resolved_version": "1.0",
            "expected_package": "automated-cp2", "expected_architecture": "amd64",
        })
        automation = {
            "attempt_key": automation_attempt_key(
                workflow["name"], upstream_identity, canonical_recipe_sha256(workflow),
            ),
            "generation": 1,
            "policy": "build",
            "expected_upstream_identity": upstream_identity,
        }
        state = [containment.AbsenceStatus.STILL_PRESENT_AND_OWNED,
                 containment.AbsenceStatus.PROVEN_GONE]
        original_create = app.build_pipeline.create_pipeline_run
        with mock.patch.object(app.command_containment, "resource_limit_capability", side_effect=self.capability), \
             mock.patch.object(
                 containment, "prove_containment_absent",
                 side_effect=lambda _blocker: proof(state.pop(0)),
             ), mock.patch.object(
                 app.build_pipeline, "create_pipeline_run", wraps=original_create,
             ) as create:
            with self.assertRaises(app.RunAdmissionError) as blocked:
                app.enqueue_preallocated_recipe_run(
                    manager, workflow, run_id=run_id, dry_run=False,
                    origin=origin, automation=automation,
                )
            resumed = app.enqueue_preallocated_recipe_run(
                manager, workflow, run_id=run_id, dry_run=False,
                origin=origin, automation=automation,
            )
            replay = app.enqueue_preallocated_recipe_run(
                manager, workflow, run_id=run_id, dry_run=False,
                origin=origin, automation=automation,
            )
        self.assertEqual(blocked.exception.code, "execution_recovery_unresolved")
        self.assertEqual(resumed["run_id"], run_id)
        self.assertTrue(replay["duplicate"])
        create.assert_called_once()
        self.assertEqual([path.name for path in manager.store.root.iterdir()], [run_id])

    def test_manual_and_automation_concurrently_share_one_reconciliation_boundary(self):
        manager = self.manager()
        self.publish()
        workflow = {
            "schema_version": 5, "name": "automated-concurrent", "active": True,
            "automation": {"enabled": True, "policy": "build"},
            "package": {"name": "automated-concurrent", "architecture": "amd64"},
            "source": {"repository": "owner/automated-concurrent", "tracking": "latest_release"},
        }
        origin = {"kind": "automation", "trigger": "upstream_change", "reason": "new_release"}
        upstream_identity = normalize_upstream_identity({
            "provider": "github", "repository": "owner/automated-concurrent",
            "tracking": "latest_release", "source_type": "release_asset",
            "payload_kind": "deb", "release_id": "11", "asset_id": "21",
            "asset_name": "automated-concurrent_1.0_amd64.deb", "expected_size": 7,
            "resolved_ref": "v1.0", "resolved_version": "1.0",
            "expected_package": "automated-concurrent", "expected_architecture": "amd64",
        })
        automation = {
            "attempt_key": automation_attempt_key(
                workflow["name"], upstream_identity, canonical_recipe_sha256(workflow),
            ),
            "generation": 1,
            "policy": "build",
            "expected_upstream_identity": upstream_identity,
        }
        both_proving = threading.Barrier(2)

        def prove_absent(_blocker):
            both_proving.wait(timeout=2)
            return proof(containment.AbsenceStatus.PROVEN_GONE)

        results = []
        failures = []
        operations = (
            lambda: app.enqueue_recipe_run(manager, recipe("manual-concurrent"), dry_run=True),
            lambda: app.enqueue_preallocated_recipe_run(
                manager, workflow, run_id="automation-concurrent-run", dry_run=False,
                origin=origin, automation=automation,
            ),
        )
        with mock.patch.object(
            app.command_containment, "resource_limit_capability", side_effect=self.capability,
        ), mock.patch.object(
            containment, "prove_containment_absent", side_effect=prove_absent,
        ):
            threads = [threading.Thread(
                target=lambda operation=operation: self._capture_result(
                    results, failures, operation,
                ),
            ) for operation in operations]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({result["status"] for result in results}, {"queued"})
        self.assertIn("automation-concurrent-run", {result["run_id"] for result in results})
        self.assertFalse(containment.containment_cleanup_blocker())

    def test_saturation_cannot_be_reopened_by_admission(self):
        manager = self.manager()
        self.publish()
        containment._CLEANUP_BLOCKERS_SATURATED = True
        with mock.patch.object(
            containment, "prove_containment_absent",
            return_value=proof(containment.AbsenceStatus.PROVEN_GONE),
        ), mock.patch.object(
            app.command_containment, "resource_limit_capability", side_effect=self.capability,
        ) as capability, self.assertRaises(app.RunAdmissionError) as blocked:
            app.enqueue_recipe_run(manager, recipe(), dry_run=True)
        self.assertEqual(blocked.exception.code, "execution_recovery_unresolved")
        self.assertIn("capacity exceeded", blocked.exception.details["reason"])
        capability.assert_not_called()

    @staticmethod
    def _capture_failure(target, function):
        try:
            function()
        except Exception as exc:
            target.append(exc)

    @staticmethod
    def _capture_result(results, failures, function):
        try:
            results.append(function())
        except Exception as exc:
            failures.append(exc)


if __name__ == "__main__":
    unittest.main()
