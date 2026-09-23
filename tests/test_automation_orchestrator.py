import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app, build_pipeline, execution_projection, recipe_store, storage, validation_service
from debbuilder.automation_identity import normalize_upstream_identity
from debbuilder.automation_ledger import AutomationLedger
from debbuilder.automation_orchestrator import AutomationOrchestrator
from debbuilder.build_models import utc_now
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.execution_manager import ExecutionManager
from debbuilder.recipe_schema import recipe_for_storage
from tests.validation_helpers import record_canonical_validation


VALIDATION_ARTIFACT = {
    "package": "demo", "version": "1.2.3", "architecture": "amd64",
    "size": 7, "sha256": "ab" * 32,
}


def publication_attempt(run_id, attempt_id, *, status="success", error=None):
    artifact = f"/builds/{run_id}/artifacts/demo_1.2.3_amd64.deb"
    result = {
        "id": attempt_id, "build_run_id": run_id, "status": status, "error": error,
        "artifact": artifact, "package": "demo", "version": "1.2.3", "architecture": "amd64",
        "repository": {"root": "/repository", "distribution": "bookworm", "component": "main"},
        "proof": None,
    }
    if status == "success":
        result["proof"] = {
            "schema": "debbuilder.repository-publication-proof.v1", "proof_version": 1,
            "verified_at": "2026-09-21T10:00:00+00:00",
            "repository": {"root": "/repository", "device": 1, "inode": 2},
            "distribution": {"requested": "bookworm", "codename": "bookworm", "suite": "stable"},
            "component": "main", "package": "demo", "version": "1.2.3", "architecture": "amd64",
            "source": {"path": artifact, "size": 7, "sha256": "ab" * 32, "device": 3, "inode": 4},
            "targets": [{
                "database_architecture": "amd64",
                "index": {
                    "path": "dists/bookworm/main/binary-amd64/Packages.gz",
                    "device": 5, "inode": 6, "filename": "pool/main/d/demo/demo_1.2.3_amd64.deb",
                    "size": 7, "sha256": "ab" * 32,
                },
                "pool": {
                    "path": "pool/main/d/demo/demo_1.2.3_amd64.deb", "size": 7,
                    "sha256": "ab" * 32, "device": 7, "inode": 8,
                },
            }],
        }
    return result


def recipe(policy="build", *, enabled=True, active=True):
    return recipe_for_storage({
        "schema_version": 5, "name": "demo", "active": active,
        "automation": {"enabled": enabled, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64"},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
    })


def identity(asset_id="20"):
    return normalize_upstream_identity({
        "provider": "github", "repository": "example/demo", "tracking": "latest_release",
        "source_type": "release_asset", "payload_kind": "deb", "release_id": "10",
        "asset_id": asset_id, "asset_name": "demo_1.2.3_amd64.deb", "expected_size": 7,
        "resolved_ref": "v1.2.3", "resolved_version": "1.2.3",
        "expected_package": "demo", "expected_architecture": "amd64",
    })


class FakeExecutionAdmission:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def __call__(self, _manager, workflow, **kwargs):
        run = self.store.load(kwargs["run_id"])
        if run is None:
            run = build_pipeline.create_pipeline_run(
                workflow, store=self.store, dry_run=kwargs["dry_run"],
                recipe_id=workflow["name"], run_id=kwargs["run_id"],
                origin=kwargs["origin"], automation=kwargs["automation"],
            )
            self.calls.append(run["id"])
        kwargs["created_callback"](run)
        if run["status"] == "pending":
            run["status"] = "queued"
            self.store.save(run)
        return {"run_id": run["id"], "status": run["status"], "duplicate": len(self.calls) != 1}


class FakeValidationManager:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def admit(self, run_id, _payload, **kwargs):
        attempt_id = f"validation-{len(self.calls) + 1}"
        root = validation_service.attempt_root(self.store, run_id, attempt_id)
        root.mkdir(parents=True, exist_ok=False)
        attempt = {
            "contract_version": 1, "id": attempt_id, "build_run_id": run_id,
            "inputs": {"profile": "bookworm", "artifact": VALIDATION_ARTIFACT, "previous_artifact": None},
            "selected_profile": {"name": "bookworm", "image": {
                "name": "debbuilder-validation:bookworm", "id": "sha256:" + "a" * 64, "digest": None,
            }},
            "status": "queued", "created_at": utc_now(), "started_at": None,
            "finished_at": None, "result": None, "error": None,
        }
        storage.save_json(root / "attempt.json", attempt)
        metadata = {
            "automatic": kwargs["automatic"],
            "publish_after_success": kwargs["publish_after_success"],
            "publication_state": "pending" if kwargs["publish_after_success"] else "not_requested",
            **kwargs["automation_context"],
        }
        storage.save_json(root / validation_service.AUTOMATION_FILE, metadata)
        self.calls.append((run_id, attempt_id, copy.deepcopy(metadata)))
        return {"attempt_id": attempt_id, "status": "queued", "duplicate": False}


class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recipes = self.root / "recipes"
        self.recipes.mkdir()
        self.data = self.root / "data"
        self.store = BuildStore(self.data / "builds")
        self.ledger = AutomationLedger(self.data)
        self.enqueue = FakeExecutionAdmission(self.store)
        self.validation = FakeValidationManager(self.store)
        self.publications = []
        self.notifications = []
        self.clock = [1_800_000_000.0]
        self.open = True

    def tearDown(self):
        self.temporary.cleanup()

    def save_recipe(self, configured):
        recipe_store.save_recipe(self.recipes / "demo.json", configured)

    def publish(self, run_id, _payload):
        result = publication_attempt(run_id, f"publication-{len(self.publications) + 1}")
        self.publications.append(result)
        with self.store.locked_run(run_id):
            run = self.store.load(run_id)
            run.setdefault("publications", []).append(copy.deepcopy(result))
            self.store.save(run)
        return result

    def orchestrator(self, publish=None):
        return AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=self.enqueue,
            validation_manager=lambda: self.validation,
            publish=publish or self.publish,
            notify_completion=lambda result: self.notifications.append(copy.deepcopy(result)),
            admission_open=lambda: self.open, wall_clock=lambda: self.clock[0],
        )

    def claim(self, policy):
        configured = recipe(policy)
        self.save_recipe(configured)
        return self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity(), canonical_recipe_sha256(configured), policy, detect_only=policy == "detect")

    def terminal_run(self, claim, status=None):
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][claim.generation]
        run_id = row["run_id"]
        with self.store.locked_run(run_id):
            run = self.store.load(run_id)
            run["status"] = status or ("prepared" if row["desired_policy"] == "test" else "success")
            if run["status"] in {"prepared", "success", "failed", "cancelled"}:
                run["finished_at"] = utc_now()
            if run["status"] == "success" and run["mode"] == "build":
                artifact = f"/builds/{run_id}/artifacts/demo_1.2.3_amd64.deb"
                run["artifact"] = {
                    "path": artifact, "size": 7, "sha256": "ab" * 32,
                    "inspection": {
                        "package": "demo", "version": "1.2.3", "architecture": "amd64",
                    },
                }
            self.store.save(run)
        return run_id

    def terminal_validation(self, run_id, attempt_id, status="success", code="validation_failed"):
        root = validation_service.attempt_root(self.store, run_id, attempt_id)
        path = root / "attempt.json"
        current = storage.load_json(path, {})
        if status == "failed" and code == "validation_preparation_failed":
            current.update({
                "status": "failed", "started_at": current["created_at"],
                "finished_at": utc_now(), "result": None,
                "error": {"code": code, "message": "Validation failed"},
            })
            storage.save_json(path, current)
            return
        metadata = storage.load_json(root / validation_service.AUTOMATION_FILE, {})
        current = record_canonical_validation(
            self.store, self.store.load(run_id), attempt_id=attempt_id,
            status=status, artifact_identity=VALIDATION_ARTIFACT,
            created_at=current["created_at"],
        )
        storage.save_json(root / validation_service.AUTOMATION_FILE, metadata)
        if status == "failed":
            current["error"]["code"] = code
            result = storage.load_json(root / "result.json", {})
            result["error"]["code"] = code
            storage.save_json(root / "result.json", result)
            storage.save_json(path, current)

    def test_policy_matrix_uses_one_canonical_stage_chain(self):
        for policy in ("test", "build", "build_validate", "full"):
            with self.subTest(policy=policy):
                self.tearDown()
                self.setUp()
                claim = self.claim(policy)
                owner = self.orchestrator()
                owner.advance(claim.attempt_key, claim.generation)
                run_id = self.terminal_run(claim)
                owner.on_run_terminal(run_id)
                if policy in {"build_validate", "full"}:
                    self.assertEqual(len(self.validation.calls), 1)
                    attempt_id = self.validation.calls[0][1]
                    self.terminal_validation(run_id, attempt_id)
                    owner.on_validation_terminal(run_id, attempt_id)
                row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
                self.assertEqual(row["terminal_classification"], "success")
                self.assertEqual(len(self.enqueue.calls), 1)
                self.assertEqual(self.store.load(run_id)["mode"], "dry_run" if policy == "test" else "build")
                self.assertEqual(len(self.validation.calls), 1 if policy in {"build_validate", "full"} else 0)
                self.assertEqual(len(self.publications), 1 if policy == "full" else 0)
                self.assertEqual(len(self.notifications), 1)

    def test_proofless_publication_success_does_not_complete_automation(self):
        def proofless_publish(run_id, _payload):
            result = publication_attempt(run_id, "publication-proofless")
            result["proof"] = None
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run.setdefault("publications", []).append(copy.deepcopy(result))
                self.store.save(run)
            return result

        claim = self.claim("full")
        owner = self.orchestrator(proofless_publish)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        validation_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, validation_id)

        owner.on_validation_terminal(run_id, validation_id)

        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "blocked_manual")
        self.assertEqual(row["last_error_code"], "publication_proof_invalid")
        self.assertEqual(
            validation_service.load_automation(self.store, run_id, validation_id)["publication_state"],
            "cancelled",
        )

    def test_public_projected_success_is_verified_from_the_durable_attempt(self):
        def projected_publish(run_id, _payload):
            result = publication_attempt(run_id, "publication-public-dto")
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run.setdefault("publications", []).append(copy.deepcopy(result))
                self.store.save(run)
            return execution_projection.public_publication(result)

        claim = self.claim("full")
        owner = self.orchestrator(projected_publish)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        validation_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, validation_id)

        owner.on_validation_terminal(run_id, validation_id)

        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "success")
        self.assertEqual(row["publication_attempt_id"], "publication-public-dto")
        self.assertEqual(
            validation_service.load_automation(self.store, run_id, validation_id)["publication_state"],
            "complete",
        )

    def test_detect_policy_creates_no_run(self):
        configured = recipe("detect")
        self.save_recipe(configured)
        claim = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity(), canonical_recipe_sha256(configured), "detect", detect_only=True)
        self.orchestrator().advance(claim.attempt_key, 0)
        self.assertEqual(self.enqueue.calls, [])
        self.assertFalse(self.store.root.exists())

    def test_duplicate_callbacks_do_not_duplicate_any_stage(self):
        claim = self.claim("full")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        owner.on_run_terminal(run_id)
        attempt_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, attempt_id)
        owner.on_validation_terminal(run_id, attempt_id)
        owner.on_validation_terminal(run_id, attempt_id)
        self.assertEqual(len(self.enqueue.calls), 1)
        self.assertEqual(len(self.validation.calls), 1)
        self.assertEqual(len(self.publications), 1)
        self.assertEqual(len(self.notifications), 1)

    def test_execution_queue_full_reuses_preallocated_run_on_retry(self):
        claim = self.claim("build")
        admitted = self.enqueue

        class QueueFull(RuntimeError):
            code = "execution_queue_full"

        calls = []

        def enqueue(manager, workflow, **kwargs):
            calls.append(kwargs["run_id"])
            if len(calls) == 1:
                run = self.store.load(kwargs["run_id"])
                if run is None:
                    run = build_pipeline.create_pipeline_run(
                        workflow, store=self.store, dry_run=kwargs["dry_run"],
                        recipe_id=workflow["name"], run_id=kwargs["run_id"],
                        origin=kwargs["origin"], automation=kwargs["automation"],
                    )
                kwargs["created_callback"](run)
                raise QueueFull("full")
            return admitted(manager, workflow, **kwargs)

        owner = AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=enqueue,
            validation_manager=lambda: self.validation, publish=self.publish,
            notify_completion=self.notifications.append,
            admission_open=lambda: self.open, wall_clock=lambda: self.clock[0],
        )
        owner.advance(claim.attempt_key, 0)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["state"], "retry_delayed")
        self.assertEqual(self.store.load(row["run_id"])["status"], "pending")
        self.clock[0] += 61
        owner.advance(claim.attempt_key, 0)
        self.assertEqual(calls, [row["run_id"], row["run_id"]])
        self.assertEqual(len(list(self.store.root.glob("*/run.json"))), 1)

    def test_execution_recovery_blocker_delays_same_preallocation_until_restart_recovery(self):
        claim = self.claim("build")

        class RecoveryBlocked(RuntimeError):
            code = "execution_recovery_unresolved"

        calls = []

        def blocked(_manager, _workflow, **kwargs):
            calls.append(kwargs["run_id"])
            raise RecoveryBlocked("canonical containment recovery is unresolved")

        owner = AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=blocked,
            validation_manager=lambda: self.validation, publish=self.publish,
            notify_completion=self.notifications.append,
            admission_open=lambda: self.open, wall_clock=lambda: self.clock[0],
        )
        owner.advance(claim.attempt_key, 0)
        delayed = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(delayed["state"], "retry_delayed")
        self.assertEqual(delayed["last_error_code"], "execution_recovery_unresolved")
        self.assertIsNotNone(delayed["preallocated_run_id"])
        self.assertIsNone(delayed["run_id"])
        self.assertEqual(list(self.store.root.glob("*/run.json")), [])

        owner.advance(claim.attempt_key, 0)
        self.assertEqual(calls, [delayed["preallocated_run_id"]])

        self.clock[0] += 61
        restarted = self.orchestrator()
        restarted.advance_all()
        admitted = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(admitted["run_id"], delayed["preallocated_run_id"])
        self.assertEqual(self.enqueue.calls, [delayed["preallocated_run_id"]])
        self.assertEqual(len(list(self.store.root.glob("*/run.json"))), 1)

    def test_disable_during_queue_backoff_blocks_pending_run_submission(self):
        claim = self.claim("build")

        class QueueFull(RuntimeError):
            code = "execution_queue_full"

        calls = []

        def enqueue(_manager, workflow, **kwargs):
            calls.append(kwargs["run_id"])
            run = self.store.load(kwargs["run_id"])
            if run is None:
                run = build_pipeline.create_pipeline_run(
                    workflow, store=self.store, dry_run=False,
                    recipe_id=workflow["name"], run_id=kwargs["run_id"],
                    origin=kwargs["origin"], automation=kwargs["automation"],
                )
            kwargs["created_callback"](run)
            raise QueueFull("full")

        owner = AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=enqueue,
            validation_manager=lambda: self.validation, publish=self.publish,
            notify_completion=self.notifications.append,
            admission_open=lambda: self.open, wall_clock=lambda: self.clock[0],
        )
        owner.advance(claim.attempt_key, 0)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        run_id = row["run_id"]

        case = self

        class DisableOnEnter:
            def __enter__(self):
                case.save_recipe(recipe("manual", enabled=False))

            def __exit__(self, _type, _value, _traceback):
                return False

        owner = AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=enqueue,
            validation_manager=lambda: self.validation, publish=self.publish,
            notify_completion=self.notifications.append,
            admission_open=lambda: self.open, wall_clock=lambda: self.clock[0],
            mutation_lease=DisableOnEnter,
        )
        self.clock[0] += 61
        owner.advance_all()
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(calls, [run_id])
        self.assertEqual(row["terminal_classification"], "disabled")
        self.assertEqual(self.store.load(run_id)["status"], "pending")

    def test_validation_transient_retry_reuses_the_successful_build(self):
        claim = self.claim("build_validate")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        first = self.validation.calls[0][1]
        self.terminal_validation(run_id, first, "failed", "validation_preparation_failed")
        owner.on_validation_terminal(run_id, first)
        owner.advance(claim.attempt_key, 0)
        self.assertEqual(len(self.validation.calls), 1)
        self.clock[0] += 61
        owner.advance(claim.attempt_key, 0)
        self.assertEqual(len(self.enqueue.calls), 1)
        self.assertEqual([row[0] for row in self.validation.calls], [run_id, run_id])

    def test_restart_links_completed_retry_admitted_before_ledger_link(self):
        claim = self.claim("build_validate")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        first = self.validation.calls[0][1]
        self.terminal_validation(run_id, first, "failed", "validation_preparation_failed")
        owner.on_validation_terminal(run_id, first)
        self.clock[0] += 61
        original = self.ledger.advance_lifecycle

        def interrupted(key, generation, stage, **kwargs):
            if kwargs.get("validation_attempt_id") == "validation-2":
                raise KeyboardInterrupt("crash before durable linkage")
            return original(key, generation, stage, **kwargs)

        with mock.patch.object(self.ledger, "advance_lifecycle", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                owner.advance(claim.attempt_key, 0)
        self.assertEqual(len(self.validation.calls), 2)
        self.terminal_validation(run_id, "validation-2")
        self.orchestrator().advance_all()
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["validation_attempt_id"], "validation-2")
        self.assertEqual(row["terminal_classification"], "success")
        self.assertEqual(len(self.validation.calls), 2)
        self.assertEqual(len(self.enqueue.calls), 1)

    def test_restart_replays_retry_interrupted_before_validation_admission(self):
        claim = self.claim("build_validate")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        first = self.validation.calls[0][1]
        self.terminal_validation(run_id, first, "failed", "validation_preparation_failed")
        owner.on_validation_terminal(run_id, first)
        self.clock[0] += 61
        with mock.patch.object(self.validation, "admit", side_effect=KeyboardInterrupt("crash before admission")):
            with self.assertRaises(KeyboardInterrupt):
                owner.advance(claim.attempt_key, 0)
        interrupted = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(interrupted["stage"], "validation_admission")
        self.assertEqual(interrupted["validation_attempt_id"], first)
        self.assertEqual(interrupted["validation_retry_count"], 1)
        self.orchestrator().advance_all()
        recovered = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(recovered["validation_attempt_id"], "validation-2")
        self.assertEqual(recovered["validation_retry_count"], 1)
        self.assertEqual(len(self.validation.calls), 2)

    def test_publication_failure_stays_pending_and_retries_exact_chain(self):
        results = ["failed", "success"]

        def publish(run_id, _payload):
            status = results.pop(0)
            result = publication_attempt(
                run_id,
                f"publication-{2 - len(results)}",
                status=status,
                error=None if status == "success" else {"code": "repository_mutation_busy"},
            )
            self.publications.append(result)
            with self.store.locked_run(run_id):
                run = self.store.load(run_id)
                run.setdefault("publications", []).append(copy.deepcopy(result))
                self.store.save(run)
            return result

        claim = self.claim("full")
        owner = self.orchestrator(publish)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        validation_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, validation_id)
        owner.on_validation_terminal(run_id, validation_id)
        metadata = validation_service.load_automation(self.store, run_id, validation_id)
        self.assertEqual(metadata["publication_state"], "pending")
        owner.advance(claim.attempt_key, 0)
        self.assertEqual(len(self.publications), 1)
        self.clock[0] += 61
        owner.advance(claim.attempt_key, 0)
        self.assertEqual(len(self.publications), 2)
        self.assertEqual(validation_service.load_automation(self.store, run_id, validation_id)["publication_state"], "complete")
        self.assertEqual(len(self.enqueue.calls), 1)
        self.assertEqual(len(self.validation.calls), 1)

    def test_restart_replays_canonical_publication_proof_instead_of_trusting_history(self):
        claim = self.claim("full")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        validation_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, validation_id)
        with self.store.locked_run(run_id):
            run = self.store.load(run_id)
            run["publications"] = [{
                "id": "older-publication", "build_run_id": run_id, "status": "success", "error": None,
            }]
            self.store.save(run)

        restarted = self.orchestrator()
        restarted.advance_all()
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "success")
        self.assertEqual(len(self.publications), 1)
        self.assertEqual(row["publication_attempt_id"], "publication-1")

    def test_disable_and_policy_changes_obey_stage_boundaries(self):
        claim = self.claim("full")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        self.save_recipe(recipe("manual", enabled=False))
        owner.on_run_terminal(run_id)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "disabled")
        self.assertEqual(self.validation.calls, [])

        self.tearDown()
        self.setUp()
        claim = self.claim("build")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        self.save_recipe(recipe("full"))
        owner.on_run_terminal(run_id)
        self.assertEqual(self.validation.calls, [])
        self.assertEqual(self.publications, [])

        self.tearDown()
        self.setUp()
        claim = self.claim("full")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        self.save_recipe(recipe("build"))
        owner.on_run_terminal(run_id)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "disabled")
        self.assertEqual(self.validation.calls, [])

    def test_disable_or_shutdown_before_publication_prevents_new_mutation(self):
        for boundary in ("disable", "shutdown"):
            with self.subTest(boundary=boundary):
                self.tearDown()
                self.setUp()
                claim = self.claim("full")
                owner = self.orchestrator()
                owner.advance(claim.attempt_key, 0)
                run_id = self.terminal_run(claim)
                owner.on_run_terminal(run_id)
                attempt_id = self.validation.calls[0][1]
                self.terminal_validation(run_id, attempt_id)
                if boundary == "disable":
                    self.save_recipe(recipe("manual", enabled=False))
                else:
                    self.open = False
                owner.on_validation_terminal(run_id, attempt_id)
                self.assertEqual(self.publications, [])
                if boundary == "disable":
                    row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
                    self.assertEqual(row["terminal_classification"], "disabled")
                    self.assertEqual(
                        validation_service.load_automation(self.store, run_id, attempt_id)["publication_state"],
                        "cancelled",
                    )
                else:
                    self.open = True
                    owner.advance_all()
                    self.assertEqual(len(self.publications), 1)

    def test_shutdown_blocks_new_stage_and_restart_advances_it(self):
        claim = self.claim("build_validate")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        self.open = False
        owner.on_run_terminal(run_id)
        self.assertEqual(self.validation.calls, [])
        self.open = True
        owner.advance_all()
        self.assertEqual(len(self.validation.calls), 1)

    def test_newer_identity_waits_for_the_recipe_active_lifecycle(self):
        first = self.claim("build")
        configured = recipe("build")
        second = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity("21"), canonical_recipe_sha256(configured), "build")
        owner = self.orchestrator()
        owner.advance_all()
        self.assertEqual(len(self.enqueue.calls), 1)
        first_run = self.terminal_run(first)
        owner.on_run_terminal(first_run)
        self.assertEqual(len(self.enqueue.calls), 2)
        second_row = self.ledger.read()["attempts"][second.attempt_key]["generations"][0]
        self.assertIsNotNone(second_row["run_id"])

    def test_stale_claim_and_closed_shutdown_create_no_run(self):
        claim = self.claim("build")
        self.save_recipe(recipe("full"))
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "disabled")
        self.assertEqual(self.enqueue.calls, [])

        self.tearDown()
        self.setUp()
        claim = self.claim("build")
        self.open = False
        self.orchestrator().advance(claim.attempt_key, 0)
        self.assertEqual(self.enqueue.calls, [])
        self.assertIsNone(self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]["preallocated_run_id"])

    def test_validation_lifecycle_failure_is_terminal_without_rebuild(self):
        claim = self.claim("build_validate")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        attempt_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, attempt_id, "failed", "validation_lifecycle_failed")
        owner.on_validation_terminal(run_id, attempt_id)
        self.clock[0] += 10_000
        owner.advance_all()
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "terminal_failure")
        self.assertEqual(len(self.enqueue.calls), 1)
        self.assertEqual(len(self.validation.calls), 1)

    def test_publication_conflict_blocks_and_cancels_pending_intent(self):
        def conflict(run_id, _payload):
            result = {
                "id": "publication-conflict", "build_run_id": run_id, "status": "failed",
                "error": {"code": "publication_identity_conflict"},
            }
            self.publications.append(result)
            return result

        claim = self.claim("full")
        owner = self.orchestrator(conflict)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        attempt_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, attempt_id)
        owner.on_validation_terminal(run_id, attempt_id)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(validation_service.load_automation(self.store, run_id, attempt_id)["publication_state"], "cancelled")
        self.clock[0] += 10_000
        owner.advance_all()
        self.assertEqual(len(self.publications), 1)

    def test_publication_downgrade_is_blocked_without_retry(self):
        def downgrade(run_id, _payload):
            result = {
                "id": "publication-downgrade", "build_run_id": run_id, "status": "failed",
                "error": {"code": "downgrade_refused"},
            }
            self.publications.append(result)
            return result

        claim = self.claim("full")
        owner = self.orchestrator(downgrade)
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        attempt_id = self.validation.calls[0][1]
        self.terminal_validation(run_id, attempt_id)
        owner.on_validation_terminal(run_id, attempt_id)
        self.clock[0] += 10_000
        owner.advance_all()
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "blocked_manual")
        self.assertEqual(len(self.publications), 1)

    def test_automated_run_does_not_use_manual_global_post_build_settings(self):
        claim = self.claim("build")
        owner = self.orchestrator()
        owner.advance(claim.attempt_key, 0)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        run_id = row["run_id"]

        def execute(submitted_run_id, **_kwargs):
            with self.store.locked_run(submitted_run_id):
                run = self.store.load(submitted_run_id)
                run["status"] = "success"
                run["finished_at"] = utc_now()
                self.store.save(run)
            return {"run_id": submitted_run_id, "status": "success"}

        old_owner = app.APPLICATION_AUTOMATION_ORCHESTRATOR
        app.APPLICATION_AUTOMATION_ORCHESTRATOR = owner
        self.addCleanup(setattr, app, "APPLICATION_AUTOMATION_ORCHESTRATOR", old_owner)
        with mock.patch.object(app.build_pipeline, "execute_pipeline_run", side_effect=execute), \
                mock.patch.object(app, "run_post_build_automation") as manual_automation, \
                mock.patch.object(app, "request_maintenance"):
            app.execute_queued_recipe_run(
                run_id, store=self.store, expected_initial_status="queued",
            )
        manual_automation.assert_not_called()
        self.assertEqual(self.validation.calls, [])
        self.assertEqual(
            self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]["terminal_classification"],
            "success",
        )

    def test_origin_reason_distinguishes_recipe_and_release_asset_changes(self):
        configured = recipe("build")
        self.save_recipe(configured)
        first = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity("20"), canonical_recipe_sha256(configured), "build")
        self.ledger.finish(first.attempt_key, 0, "success")

        revised = copy.deepcopy(configured)
        revised["package"]["description"] = "revised"
        self.save_recipe(revised)
        revision = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity("20"), canonical_recipe_sha256(revised), "build")
        owner = self.orchestrator()
        owner.advance(revision.attempt_key, 0)
        revision_row = self.ledger.read()["attempts"][revision.attempt_key]["generations"][0]
        self.assertEqual(self.store.load(revision_row["run_id"])["origin"]["reason"], "recipe_revision_changed")
        self.ledger.finish(revision.attempt_key, 0, "success")

        changed_asset = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity("21"), canonical_recipe_sha256(revised), "build")
        owner.advance(changed_asset.attempt_key, 0)
        asset_row = self.ledger.read()["attempts"][changed_asset.attempt_key]["generations"][0]
        self.assertEqual(self.store.load(asset_row["run_id"])["origin"]["reason"], "release_asset_changed")

    def test_notification_failure_never_restarts_lifecycle(self):
        claim = self.claim("build")
        owner = AutomationOrchestrator(
            self.recipes, self.ledger, self.store,
            execution_manager=lambda: object(), enqueue_run=self.enqueue,
            validation_manager=lambda: self.validation, publish=self.publish,
            notify_completion=lambda _result: (_ for _ in ()).throw(OSError("offline")),
            admission_open=lambda: True, wall_clock=lambda: self.clock[0],
        )
        owner.advance(claim.attempt_key, 0)
        run_id = self.terminal_run(claim)
        owner.on_run_terminal(run_id)
        row = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(row["terminal_classification"], "success")
        self.assertTrue(row["completion_notified"])
        owner.advance_all()
        self.assertEqual(len(self.enqueue.calls), 1)

    def test_preallocation_and_created_unlinked_restart_use_one_run(self):
        claim = self.claim("build")
        row = self.ledger.preallocate_run(claim.attempt_key, 0)
        configured = recipe("build")
        metadata = {
            "attempt_key": claim.attempt_key, "generation": 0, "policy": "build",
            "expected_upstream_identity": identity(),
        }
        self.store.create(
            configured, recipe_id="demo", mode="build", run_id=row["preallocated_run_id"],
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation=metadata,
        )
        owner = self.orchestrator()
        owner.advance_all()
        linked = self.ledger.read()["attempts"][claim.attempt_key]["generations"][0]
        self.assertEqual(linked["run_id"], row["preallocated_run_id"])
        self.assertEqual(len(list(self.store.root.glob("*/run.json"))), 1)

    def test_preallocated_admission_uses_exact_id_and_canonical_manager(self):
        configured = recipe("test")
        self.save_recipe(configured)
        claim = self.ledger.claim_current_recipe(self.recipes / "demo.json", "demo", identity(), canonical_recipe_sha256(configured), "test")
        row = self.ledger.preallocate_run(claim.attempt_key, 0)

        def execute(run_id, *, store, expected_initial_status, cancellation_control=None):
            with store.locked_run(run_id):
                run = store.load(run_id)
                run["status"] = "prepared"
                run["finished_at"] = utc_now()
                store.save(run)
            return {"run_id": run_id, "status": "prepared"}

        manager = ExecutionManager(self.store, execute=execute)
        manager.start()
        self.addCleanup(manager.stop, timeout=5)
        metadata = {
            "attempt_key": claim.attempt_key, "generation": 0, "policy": "test",
            "expected_upstream_identity": identity(),
        }
        old_data = app.DATA
        app.DATA = self.data
        self.addCleanup(setattr, app, "DATA", old_data)
        capability = {"backend": "process_group", "available": True, "requested_controls": [], "reason": "not requested"}
        with mock.patch.object(app.command_containment, "resource_limit_capability", return_value=capability):
            result = app.enqueue_preallocated_recipe_run(
                manager, configured, run_id=row["preallocated_run_id"], dry_run=True,
                origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
                automation=metadata,
                created_callback=lambda run: self.ledger.link_run(claim.attempt_key, 0, run["id"], store=self.store),
            )
        self.assertEqual(result["run_id"], row["preallocated_run_id"])
        manager.stop(timeout=5)
        run = self.store.load(result["run_id"])
        self.assertEqual(run["origin"]["kind"], "automation")
        self.assertEqual(run["automation"], metadata)
        self.assertEqual(run["status"], "prepared")


if __name__ == "__main__":
    unittest.main()
