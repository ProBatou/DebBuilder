import copy
import json
import tempfile
import unittest
from pathlib import Path

from debbuilder.automation_identity import automation_attempt_key
from debbuilder.build_models import validate_run
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.execution_service import get_execution, list_executions


def recipe(automation=None):
    return {
        "schema_version": 5,
        "name": "demo",
        "active": True,
        "automation": automation or {"enabled": False, "policy": "manual"},
        "package": {"name": "demo"},
        "source": {"repository": "owner/demo"},
    }


def identity():
    return {
        "schema_version": 1,
        "provider": "github",
        "repository": "owner/demo",
        "tracking": "latest_release",
        "source_type": "release_asset",
        "payload_kind": "deb",
        "release_id": "100",
        "asset_id": "200",
        "resolved_ref": "v1.0.0",
        "resolved_version": "1.0.0",
        "content_sha256": "12" * 32,
        "asset_name": "demo_1.0.0_amd64.deb",
        "expected_size": 42,
        "expected_package": "demo",
        "expected_architecture": "amd64",
    }


class AutomationRunMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = BuildStore(Path(self.temporary.name) / "builds")

    def tearDown(self):
        self.temporary.cleanup()

    def test_manual_run_defaults_and_execution_are_unchanged(self):
        run = self.store.create(recipe(), run_id="manual")
        self.assertEqual(run["origin"], {"kind": "manual", "trigger": "manual", "reason": None})
        self.assertIsNone(run["automation"])
        self.assertEqual(self.store.load("manual"), run)

    def test_automation_metadata_roundtrips_and_binds_snapshot(self):
        configured = recipe({"enabled": True, "policy": "build_validate"})
        recipe_sha = canonical_recipe_sha256(configured)
        key = automation_attempt_key("demo", identity(), recipe_sha)
        run = self.store.create(
            configured, mode="build",
            run_id="automated",
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation={
                "attempt_key": key,
                "generation": 0,
                "policy": "build_validate",
                "expected_upstream_identity": identity(),
            },
        )
        loaded = self.store.load("automated")
        self.assertEqual(loaded["automation"], run["automation"])
        self.assertEqual(loaded["automation"]["expected_upstream_identity"]["asset_id"], "200")
        self.assertEqual(loaded["automation"]["policy"], "build_validate")
        self.assertEqual(loaded["recipe_sha256"], recipe_sha)
        snapshot = json.loads((self.store.run_dir("automated") / "recipe.json").read_text())
        self.assertEqual(snapshot["automation"], {"enabled": True, "policy": "build_validate"})

    def test_historical_run_without_metadata_loads_in_memory_without_rewrite(self):
        run = self.store.create(recipe(), run_id="historical")
        path = self.store.run_dir(run["id"]) / "run.json"
        historical = copy.deepcopy(run)
        historical["schema_version"] = 2
        historical.pop("origin")
        historical.pop("automation")
        historical.pop("admission_sha256")
        path.write_text(json.dumps(historical))
        before = path.read_bytes()
        loaded = self.store.load("historical")
        self.assertEqual(loaded["origin"]["kind"], "manual")
        self.assertIsNone(loaded["automation"])
        self.assertEqual(path.read_bytes(), before)

    def test_current_run_cannot_drop_or_relabel_automation_provenance(self):
        configured = recipe({"enabled": True, "policy": "build"})
        recipe_sha = canonical_recipe_sha256(configured)
        run = self.store.create(
            configured, run_id="sealed", mode="build",
            origin={"kind": "automation", "trigger": "upstream_change", "reason": "new_release"},
            automation={
                "attempt_key": automation_attempt_key("demo", identity(), recipe_sha),
                "generation": 0, "policy": "build", "expected_upstream_identity": identity(),
            },
        )
        missing = copy.deepcopy(run)
        missing.pop("origin")
        missing.pop("automation")
        with self.assertRaises(ValueError):
            validate_run(missing)
        relabelled = copy.deepcopy(run)
        relabelled["origin"] = {"kind": "manual", "trigger": "manual", "reason": None}
        relabelled["automation"] = None
        with self.assertRaises(ValueError):
            validate_run(relabelled)

    def test_invalid_or_unbounded_automation_metadata_is_rejected(self):
        configured = recipe({"enabled": True, "policy": "build"})
        recipe_sha = canonical_recipe_sha256(configured)
        key = automation_attempt_key("demo", identity(), recipe_sha)
        base = self.store.create(
            configured, run_id="base", mode="build",
            origin={"kind": "automation", "trigger": "upstream_change", "reason": None},
            automation={"attempt_key": key, "generation": 0, "policy": "build", "expected_upstream_identity": identity()},
        )
        for field, value in (("attempt_key", "bad"), ("generation", 99), ("policy", "unknown")):
            candidate = copy.deepcopy(base)
            candidate["automation"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_run(candidate)
        candidate = copy.deepcopy(base)
        candidate["origin"]["reason"] = "private/path"
        with self.assertRaises(ValueError):
            validate_run(candidate)
        for origin in (
            {"kind": "manual", "trigger": "manual", "reason": "new_release"},
            {"kind": "automation", "trigger": "upstream_change", "reason": "operator_check_now"},
            {"kind": "automation_check_now", "trigger": "operator_check_now", "reason": "new_release"},
        ):
            candidate = copy.deepcopy(base)
            candidate["origin"] = origin
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_run(candidate)

    def test_attempt_key_mismatch_is_rejected(self):
        configured = recipe({"enabled": True, "policy": "build"})
        with self.assertRaises(ValueError):
            self.store.create(
                configured,
                run_id="mismatch", mode="build",
                origin={"kind": "automation", "trigger": "upstream_change", "reason": None},
                automation={
                    "attempt_key": "automation-v1-" + "00" * 32,
                    "generation": 0,
                    "policy": "build",
                    "expected_upstream_identity": identity(),
                },
            )
        self.assertFalse(self.store.run_dir("mismatch").exists())

    def test_run_policy_must_match_eligible_recipe_snapshot(self):
        configured = recipe({"enabled": True, "policy": "build"})
        recipe_sha = canonical_recipe_sha256(configured)
        metadata = {
            "attempt_key": automation_attempt_key("demo", identity(), recipe_sha),
            "generation": 0,
            "policy": "full",
            "expected_upstream_identity": identity(),
        }
        with self.assertRaisesRegex(ValueError, "policy does not match"):
            self.store.create(
                configured, run_id="wrong-policy",
                origin={"kind": "automation", "trigger": "upstream_change", "reason": None},
                automation=metadata,
            )
        disabled = recipe({"enabled": False, "policy": "build"})
        disabled_sha = canonical_recipe_sha256(disabled)
        metadata.update({
            "attempt_key": automation_attempt_key("demo", identity(), disabled_sha),
            "policy": "build",
        })
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.store.create(
                disabled, run_id="disabled-policy",
                origin={"kind": "automation", "trigger": "upstream_change", "reason": None},
                automation=metadata,
            )
        detect = recipe({"enabled": True, "policy": "detect"})
        detect_sha = canonical_recipe_sha256(detect)
        metadata.update({
            "attempt_key": automation_attempt_key("demo", identity(), detect_sha),
            "policy": "detect",
        })
        with self.assertRaisesRegex(ValueError, "Detect-only"):
            self.store.create(
                detect, run_id="detect-policy",
                origin={"kind": "automation_check_now", "trigger": "operator_check_now", "reason": None},
                automation=metadata,
            )

    def test_automation_policy_constrains_and_seals_run_mode(self):
        for policy, accepted_mode, rejected_mode in (
            ("test", "dry_run", "build"),
            ("build", "build", "dry_run"),
            ("build_validate", "build", "dry_run"),
            ("full", "build", "dry_run"),
        ):
            configured = recipe({"enabled": True, "policy": policy})
            recipe_sha = canonical_recipe_sha256(configured)
            metadata = {
                "attempt_key": automation_attempt_key("demo", identity(), recipe_sha),
                "generation": 0, "policy": policy, "expected_upstream_identity": identity(),
            }
            with self.subTest(policy=policy, case="rejected"), self.assertRaisesRegex(ValueError, "requires Run mode"):
                self.store.create(
                    configured, run_id=f"{policy}-rejected", mode=rejected_mode,
                    origin={"kind": "automation", "trigger": "upstream_change", "reason": None}, automation=metadata,
                )
            accepted = self.store.create(
                configured, run_id=f"{policy}-accepted", mode=accepted_mode,
                origin={"kind": "automation", "trigger": "upstream_change", "reason": None}, automation=metadata,
            )
            tampered = copy.deepcopy(accepted)
            tampered["mode"] = rejected_mode
            with self.subTest(policy=policy, case="tampered"), self.assertRaises(ValueError):
                validate_run(tampered)

    def test_public_execution_projection_omits_internal_automation_seal(self):
        configured = recipe({"enabled": True, "policy": "build"})
        recipe_sha = canonical_recipe_sha256(configured)
        run = self.store.create(
            configured, run_id="public", mode="build",
            origin={"kind": "automation_check_now", "trigger": "operator_check_now", "reason": None},
            automation={
                "attempt_key": automation_attempt_key("demo", identity(), recipe_sha),
                "generation": 0, "policy": "build", "expected_upstream_identity": identity(),
            },
        )
        projected = get_execution(self.store, run["id"])
        self.assertEqual(projected["origin"]["kind"], "automation_check_now")
        self.assertNotIn("automation", projected)
        self.assertNotIn("admission_sha256", projected)
        summaries = list_executions(self.store, lambda candidate: candidate["recipe_id"])
        self.assertEqual(summaries[0]["origin"]["kind"], "automation_check_now")
        self.assertNotIn("automation", summaries[0])
        self.assertNotIn("admission_sha256", summaries[0])


if __name__ == "__main__":
    unittest.main()
