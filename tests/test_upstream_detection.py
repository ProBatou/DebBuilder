import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import github_client, recipe_store, source_acquisition, upstream_archive, upstream_artifact
from debbuilder.automation_identity import release_asset_identity
from debbuilder.automation_ledger import AutomationLedger
from debbuilder.recipe_schema import recipe_for_storage
from debbuilder.upstream_detection import AutomationDetectionService, UpstreamDetectionError, detect_upstream
from debbuilder.upstream_observation import UpstreamObservationService, UpstreamObservationStore


COMMIT_A = "a1" * 20
COMMIT_B = "b2" * 20


def recipe(*, policy="build", mode="source_build", tracking="latest_release", ref="", version_source="tag", version_expression="", **artifact):
    artifact_config = {"mode": mode, "architecture": "amd64"}
    if mode == "upstream_archive":
        artifact_config.update({
            "type": "archive", "archive_source": "release_asset", "asset_selection": "exact",
            "asset_name": "demo-linux.tar.gz", "payload": {"mode": "entire_archive"},
        })
    elif mode == "upstream_deb":
        artifact_config.update({"name_pattern": "demo_*_amd64.deb"})
    artifact_config.update(artifact)
    return recipe_for_storage({
        "schema_version": 5, "name": "demo", "active": True,
        "automation": {"enabled": True, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64"},
        "source": {
            "repository": "Example/Demo", "tracking": tracking, "ref": ref,
            "version": {"source": version_source, "expression": version_expression},
        },
        "artifact": artifact_config,
    })


def release(*, release_id=100, asset_id=200, asset_name="demo-linux.tar.gz", digest=""):
    return {
        "repository": "example/demo", "release_id": release_id, "tag": "v1.2.3", "ref": "v1.2.3",
        "name": "Release 1.2.3", "upstream_version": "1.2.3",
        "tarball_url": "https://api.github.com/repos/example/demo/tarball/v1.2.3",
        "zipball_url": "https://api.github.com/repos/example/demo/zipball/v1.2.3",
        "assets": [{
            "asset_id": asset_id, "name": asset_name, "size": 42, "digest": digest,
            "url": f"https://github.com/example/demo/releases/download/v1.2.3/{asset_name}",
        }],
    }


def detected_identity(asset_id=200):
    configured = recipe(mode="upstream_deb")
    rel = release(asset_id=asset_id, asset_name="demo_1.2.3_amd64.deb")
    return release_asset_identity(configured, rel, rel["assets"][0], "deb")


class ExactDetectionTests(unittest.TestCase):
    def test_latest_release_generated_source_resolves_release_and_commit(self):
        resolved_release = release()
        resolved_release["archive_url"] = resolved_release["tarball_url"]
        with mock.patch.object(github_client, "repo_info", return_value={"repository": "Example/Demo"}), \
                mock.patch.object(github_client, "latest_release_exact", return_value=resolved_release), \
                mock.patch.object(github_client, "resolve_ref", return_value={
                    "tag": "v1.2.3", "ref": "v1.2.3", "name": "v1.2.3", "commit": COMMIT_A,
                    "ref_object_sha": COMMIT_B,
                    "archive_url": f"https://api.github.com/repos/Example/Demo/tarball/{COMMIT_A}",
                }):
            result = detect_upstream(recipe())
        identity = result["identity"]
        self.assertEqual(identity["source_type"], "github_source_archive")
        self.assertEqual(identity["release_id"], "100")
        self.assertEqual(identity["commit_sha"], COMMIT_A)
        self.assertEqual(identity["ref_object_sha"], COMMIT_B)
        self.assertEqual(identity["source_archive_format"], "tar.gz")
        self.assertNotIn("url", identity)

    def test_explicit_tag_branch_and_full_commit_all_resolve_exact_commit(self):
        for tracking, selector, commit in (
            ("tag", "v1.2.3", COMMIT_A),
            ("manual", "main-1.2.3", COMMIT_B),
            ("manual", COMMIT_A, COMMIT_A),
        ):
            resolved = {
                "tag": selector if tracking == "tag" else "", "ref": selector, "name": selector,
                "commit": commit, "ref_object_sha": COMMIT_B if tracking == "tag" else "",
                "archive_url": f"https://api.github.com/repos/example/demo/tarball/{commit}",
            }
            with self.subTest(tracking=tracking, selector=selector), \
                    mock.patch.object(github_client, "repo_info", return_value={"repository": "example/demo"}), \
                    mock.patch.object(github_client, "resolve_ref", return_value=resolved):
                identity = detect_upstream(recipe(
                    tracking=tracking, ref=selector, version_source="regex", version_expression=r"([0-9]+(?:\.[0-9]+)*)",
                ))["identity"]
            self.assertEqual(identity["source_type"], "git_ref")
            self.assertEqual(identity["requested_ref"], selector)
            self.assertEqual(identity["commit_sha"], commit)
            self.assertEqual(identity["ref_object_sha"], COMMIT_B if tracking == "tag" else "")

    def test_explicit_ref_upstream_archive_uses_the_same_exact_ref_identity(self):
        configured = recipe(
            mode="upstream_archive", tracking="manual", ref="branch-1.2.3",
            version_source="regex", version_expression=r"([0-9]+(?:\.[0-9]+)*)",
            asset_name="", archive_source="github_source", asset_selection="pattern",
            name_pattern="", archive_format="zip",
        )
        resolved = {
            "repository": "example/demo", "tag": "", "ref": "branch-1.2.3",
            "release_name": "branch-1.2.3", "release_url": "", "commit": COMMIT_A,
            "ref_object_sha": "", "upstream_version": "1.2.3", "debian_version": "1.2.3-1",
            "archive_url": f"https://api.github.com/repos/example/demo/tarball/{COMMIT_A}",
        }
        with mock.patch.object(source_acquisition, "resolve_source", return_value=resolved):
            identity = detect_upstream(configured)["identity"]
        self.assertEqual(identity["source_type"], "git_ref")
        self.assertEqual(identity["commit_sha"], COMMIT_A)
        self.assertEqual(identity["source_archive_format"], "zip")

    def test_release_archive_raw_and_deb_assets_have_exact_distinct_shapes(self):
        cases = (
            (recipe(mode="upstream_archive"), release(), "archive"),
            (recipe(mode="upstream_archive", asset_name="install.sh"), release(asset_name="install.sh"), "raw_file"),
            (recipe(mode="upstream_deb"), release(asset_name="demo_1.2.3_amd64.deb"), "deb"),
        )
        for configured, rel, kind in cases:
            target = "debbuilder.upstream_detection.upstream_artifact.resolve_release" if kind == "deb" else "debbuilder.upstream_detection.upstream_archive.resolve_release"
            with self.subTest(kind=kind), mock.patch(target, return_value=rel):
                identity = detect_upstream(configured)["identity"]
            self.assertEqual(identity["payload_kind"], kind)
            self.assertEqual(identity["release_id"], "100")
            self.assertEqual(identity["asset_id"], "200")
            self.assertEqual(identity["asset_name"], rel["assets"][0]["name"])
            self.assertEqual(identity["expected_size"], 42)
            if kind == "deb":
                self.assertEqual(identity["expected_package"], "demo")

    def test_selector_before_classification_and_no_generated_fallback(self):
        configured = recipe(mode="upstream_archive", asset_selection="pattern", asset_name="", name_pattern="demo*")
        rel = release()
        rel["assets"].append({**rel["assets"][0], "asset_id": 201, "name": "demo-install.sh"})
        with mock.patch.object(upstream_archive, "resolve_release", return_value=rel):
            with self.assertRaises(UpstreamDetectionError) as caught:
                detect_upstream(configured)
        self.assertEqual(caught.exception.code, "ambiguous_release_asset")
        configured["artifact"].update({"asset_selection": "exact", "asset_name": "missing", "name_pattern": ""})
        with mock.patch.object(upstream_archive, "resolve_release", return_value=rel):
            with self.assertRaises(UpstreamDetectionError) as missing:
                detect_upstream(configured)
        self.assertEqual(missing.exception.code, "release_asset_not_found")

    def test_stable_network_taxonomy(self):
        for code, classification in (
            ("release_not_found", "source_not_found"),
            ("github_rate_limited", "rate_limited"),
            ("github_unavailable", "upstream_unavailable"),
            ("github_api_error", "upstream_unavailable"),
        ):
            error = source_acquisition.SourceError(code, "private upstream response omitted")
            with self.subTest(code=code), mock.patch.object(source_acquisition, "resolve_source", side_effect=error):
                with self.assertRaises(UpstreamDetectionError) as caught:
                    detect_upstream(recipe())
            self.assertEqual(caught.exception.classification, classification)
            self.assertNotIn("private", str(caught.exception))

    def test_rate_limit_hints_survive_bounded_error_wrapping(self):
        def fail_with_wrapped_rate_limit(*_args, **_kwargs):
            try:
                raise github_client.GitHubError(
                    "github_rate_limited", "private response",
                    retry_after_seconds=120, rate_limit_reset=2_000_000_180,
                )
            except github_client.GitHubError as cause:
                raise source_acquisition.SourceError("github_rate_limited", "bounded source failure") from cause

        with mock.patch.object(source_acquisition, "resolve_source", side_effect=fail_with_wrapped_rate_limit):
            with self.assertRaises(UpstreamDetectionError) as caught:
                detect_upstream(recipe())
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertEqual(caught.exception.rate_limit_reset, 2_000_000_180)

    def test_malformed_github_objects_stay_inside_the_bounded_taxonomy(self):
        malformed = (
            [[]],
            [{"full_name": 123}],
            [{"full_name": {"owner": "example", "name": "demo"}}],
            [{"full_name": "example/demo"}, {"id": 100, "tag_name": "v1.2.3", "assets": ["not-an-object"]}],
        )
        for responses in malformed:
            with self.subTest(responses=responses), \
                    mock.patch.object(github_client, "request_json", side_effect=responses):
                with self.assertRaises(UpstreamDetectionError) as caught:
                    detect_upstream(recipe())
            self.assertEqual(caught.exception.code, "github_api_error")
            self.assertEqual(caught.exception.classification, "upstream_unavailable")


class DetectionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recipes = self.root / "recipes"
        self.recipes.mkdir()
        self.ledger = AutomationLedger(self.root / "data")
        self.service = self.detection_service(self.ledger)

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, configured):
        return recipe_store.save_recipe(self.recipes / "demo.json", configured)

    def detection_service(self, ledger):
        return AutomationDetectionService(
            self.recipes, ledger,
            observation_service=UpstreamObservationService(
                UpstreamObservationStore(ledger.data_dir), resolver=detect_upstream,
            ),
        )

    @staticmethod
    def detector(identity=None, barrier=None):
        def run(_snapshot, token=""):
            if barrier:
                barrier.wait()
            return {"identity": identity or detected_identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}
        return run

    def test_detect_only_is_terminally_handled_and_creates_no_run(self):
        self.save(recipe(policy="detect"))
        first = self.service.check("demo", detector=self.detector())
        second = self.service.check("demo", detector=self.detector())
        self.assertEqual(first["classification"], "detected")
        self.assertEqual(first["attempt_state"], "terminal")
        self.assertFalse(first["claim_eligible"])
        self.assertEqual(second["classification"], "suppressed_terminal")
        self.assertEqual(len(self.ledger.read()["attempts"]), 1)
        self.assertFalse((self.root / "data/builds").exists())

    def test_execution_policy_claims_once_and_survives_restart(self):
        self.save(recipe(policy="build"))
        first = self.service.check("demo", detector=self.detector())
        second = self.detection_service(AutomationLedger(self.root / "data")).check(
            "demo", detector=self.detector(),
        )
        self.assertEqual(first["classification"], "detected")
        self.assertTrue(first["claim_eligible"])
        self.assertEqual(second["classification"], "no_change")
        self.assertEqual(first["attempt_key"], second["attempt_key"])

    def test_same_upstream_changed_recipe_and_same_version_changed_identity_are_new(self):
        configured = recipe(policy="build")
        self.save(configured)
        first = self.service.check("demo", detector=self.detector())
        changed = copy.deepcopy(configured)
        changed["package"]["description"] = "changed Recipe"
        self.save(changed)
        second = self.service.check("demo", detector=self.detector())
        third = self.service.check("demo", detector=self.detector(detected_identity(999)))
        self.assertEqual(len({first["attempt_key"], second["attempt_key"], third["attempt_key"]}), 3)
        self.assertEqual(third["display_version"], first["display_version"])

    def test_concurrent_identical_checks_converge_on_one_claim(self):
        self.save(recipe(policy="build"))
        entered = threading.Event()
        release = threading.Event()
        def detector(_snapshot, token=""):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"identity": detected_identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.service.check("demo", detector=detector))) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(5))
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(row["change"] == "new" for row in results), 1)
        self.assertEqual(len(self.ledger.read()["attempts"]), 1)

    def test_recipe_edit_disable_and_policy_change_during_network_never_claim_stale_snapshot(self):
        for mutation in ("edit", "disable", "policy"):
            with self.subTest(mutation=mutation):
                data = self.root / f"data-{mutation}"
                service = self.detection_service(AutomationLedger(data))
                configured = recipe(policy="build")
                self.save(configured)

                def detector(_snapshot, token=""):
                    changed = copy.deepcopy(configured)
                    if mutation == "edit":
                        changed["package"]["description"] = "changed"
                    elif mutation == "disable":
                        changed["automation"]["enabled"] = False
                    else:
                        changed["automation"]["policy"] = "test"
                    self.save(changed)
                    return {"identity": detected_identity(), "display_version": "1.2.3", "display_ref": "v1.2.3"}

                result = service.check("demo", detector=detector)
                self.assertEqual(result["classification"], "recipe_changed")
                self.assertEqual(AutomationLedger(data).read()["attempts"], {})

    def test_recipe_change_in_the_former_recheck_to_claim_gap_is_cas_rejected(self):
        configured = recipe(policy="build")
        self.save(configured)
        path = self.recipes / "demo.json"

        class RacingLedger(AutomationLedger):
            def claim_current_recipe(inner_self, *args, **kwargs):
                changed = copy.deepcopy(configured)
                changed["automation"]["enabled"] = False
                recipe_store.save_recipe(path, changed)
                return super().claim_current_recipe(*args, **kwargs)

        ledger = RacingLedger(self.root / "race-data")
        result = self.detection_service(ledger).check("demo", detector=self.detector())
        self.assertEqual(result["classification"], "recipe_changed")
        self.assertEqual(ledger.read()["attempts"], {})

    def test_disabled_manual_and_builtin_recipes_fail_before_network(self):
        for configured in (
            recipe(policy="manual"),
            {**recipe(policy="build"), "active": False},
        ):
            self.save(configured)
            detector = mock.Mock()
            result = self.service.check("demo", detector=detector)
            self.assertEqual(result["classification"], "invalid_configuration")
            detector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
