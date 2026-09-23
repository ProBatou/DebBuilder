import copy
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import app, build_pipeline, source_acquisition, upstream_archive, upstream_artifact
from debbuilder.automation_identity import (
    UpstreamIdentityError,
    automation_attempt_key,
    release_asset_identity,
    source_archive_identity,
    verify_expected_upstream_identity,
)
from debbuilder.build_store import BuildStore, canonical_recipe_sha256
from debbuilder.recipe_schema import runtime_recipe_for_storage, validate_recipe_metadata
from debbuilder.build_models import RunDocumentError


COMMIT_A = "a1" * 20
COMMIT_B = "b2" * 20


def source_recipe(*, tracking="manual", ref="v1.2.3", policy="build"):
    return validate_recipe_metadata({
        "schema_version": 5, "name": "demo", "active": True,
        "automation": {"enabled": True, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64"},
        "source": {"repository": "example/demo", "tracking": tracking, "ref": ref, "version": {"source": "tag"}},
    })


def archive_recipe(*, asset_name="demo.tar.gz", policy="build", **artifact):
    artifact_config = {
        "mode": "upstream_archive", "type": "archive", "architecture": "amd64",
        "archive_source": "release_asset", "asset_selection": "exact", "asset_name": asset_name,
        "payload": {"mode": "entire_archive"},
    }
    artifact_config.update(artifact)
    return validate_recipe_metadata({
        "schema_version": 5, "name": "demo", "active": True,
        "automation": {"enabled": True, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64"},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
        "artifact": artifact_config,
    })


def deb_recipe(policy="build"):
    return validate_recipe_metadata({
        "schema_version": 5, "name": "demo", "active": True,
        "automation": {"enabled": True, "policy": policy},
        "package": {"name": "demo", "architecture": "amd64"},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
        "artifact": {"mode": "upstream_deb", "architecture": "amd64", "name_pattern": "demo_*_amd64.deb"},
    })


def release(*, release_id=10, asset_id=20, name="demo.tar.gz", size=7, digest=""):
    return {
        "repository": "example/demo", "release_id": release_id, "tag": "v1.2.3", "ref": "v1.2.3",
        "name": "v1.2.3", "upstream_version": "1.2.3",
        "assets": [{
            "asset_id": asset_id, "name": name, "size": size, "digest": digest,
            "url": f"https://github.com/example/demo/releases/download/v1.2.3/{name}",
        }],
    }


def tar_payload():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        info = tarfile.TarInfo("demo-v1.2.3/app.txt")
        payload = b"content"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def ref_resolution(configured, commit=COMMIT_A):
    return {
        "repository": "example/demo", "strategy": configured["source"]["tracking"],
        "ref": configured["source"]["ref"], "tag": configured["source"]["ref"],
        "release_name": configured["source"]["ref"], "commit": commit, "ref_object_sha": COMMIT_B,
        "release_id": None, "archive_url": f"https://api.github.com/repos/example/demo/tarball/{commit}",
        "upstream_version": "1.2.3", "debian_version": "1.2.3-1",
    }


class IdentityComparisonTests(unittest.TestCase):
    def test_every_immutable_or_mode_difference_fails_closed(self):
        configured = deb_recipe()
        rel = release(name="demo_1.2.3_amd64.deb")
        expected = release_asset_identity(configured, rel, rel["assets"][0], "deb")
        mutations = (
            {"release_id": "11"}, {"asset_id": "21"}, {"asset_name": "other.deb"},
            {"payload_kind": "archive", "expected_package": "", "expected_architecture": ""},
            {"source_type": "github_source_archive", "payload_kind": "source_archive", "asset_id": "", "asset_name": "",
             "expected_size": None, "expected_package": "", "expected_architecture": "",
             "content_sha256": "", "commit_sha": COMMIT_A, "source_archive_format": "tar.gz"},
        )
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(UpstreamIdentityError) as caught:
                verify_expected_upstream_identity(expected, {**expected, **changes})
            self.assertEqual(caught.exception.code, "upstream_identity_changed")

    def test_moved_tag_and_branch_commit_are_different_identities(self):
        for tracking in ("tag", "manual"):
            configured = source_recipe(tracking=tracking, ref="v1.2.3")
            first = source_archive_identity(configured, ref_resolution(configured, COMMIT_A), generated_release=False)
            second = source_archive_identity(configured, ref_resolution(configured, COMMIT_B), generated_release=False)
            with self.subTest(tracking=tracking), self.assertRaises(UpstreamIdentityError):
                verify_expected_upstream_identity(first, second)

    def test_retargeted_tag_object_over_same_commit_is_a_new_identity(self):
        configured = source_recipe(tracking="tag", ref="v1.2.3")
        first_resolution = ref_resolution(configured, COMMIT_A)
        second_resolution = {**first_resolution, "ref_object_sha": "c3" * 20}
        first = source_archive_identity(configured, first_resolution, generated_release=False)
        second = source_archive_identity(configured, second_resolution, generated_release=False)
        with self.assertRaises(UpstreamIdentityError) as caught:
            verify_expected_upstream_identity(first, second)
        self.assertEqual(caught.exception.code, "upstream_identity_changed")


class AcquisitionPinningTests(unittest.TestCase):
    def test_generated_source_and_explicit_ref_mismatch_before_download(self):
        for tracking in ("latest_release", "tag", "manual"):
            configured = source_recipe(tracking=tracking, ref="v1.2.3")
            generated = tracking == "latest_release"
            expected_resolution = ref_resolution(configured, COMMIT_A)
            if generated:
                expected_resolution.update({"release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3"})
            expected = source_archive_identity(configured, expected_resolution, generated_release=generated)
            actual_resolution = ref_resolution(configured, COMMIT_B)
            if generated:
                actual_resolution.update({"release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3"})
            actual_resolution["upstream_identity"] = source_archive_identity(
                configured, actual_resolution, generated_release=generated,
            )
            downloader = mock.Mock()
            with self.subTest(tracking=tracking), tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.object(source_acquisition, "resolve_source", return_value=actual_resolution):
                with self.assertRaises(source_acquisition.SourceError) as caught:
                    source_acquisition.acquire_source(
                        configured, temporary, expected_identity=expected,
                    )
            self.assertEqual(caught.exception.code, "upstream_identity_changed")
            downloader.assert_not_called()

    def test_release_archive_raw_and_replaced_pattern_asset_fail_before_download(self):
        cases = (("demo.tar.gz", "archive"), ("install.sh", "raw_file"))
        for name, kind in cases:
            configured = archive_recipe(asset_name=name)
            original = release(asset_id=20, name=name)
            expected = release_asset_identity(configured, original, original["assets"][0], kind)
            replacement = release(asset_id=21, name=name)
            downloader = mock.Mock()
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                    upstream_archive.resolve_and_extract(
                        configured, temporary, expected_identity=expected,
                        release_resolver=lambda *_args, **_kwargs: replacement, downloader=downloader,
                    )
            self.assertEqual(caught.exception.code, "upstream_identity_changed")
            downloader.assert_not_called()

        configured = archive_recipe(asset_name="", asset_selection="pattern", name_pattern="demo-*.tar.gz")
        original = release(asset_id=20, name="demo-old.tar.gz")
        expected = release_asset_identity(configured, original, original["assets"][0], "archive")
        replacement = release(asset_id=21, name="demo-new.tar.gz")
        downloader = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.resolve_and_extract(
                configured, temporary, expected_identity=expected,
                release_resolver=lambda *_args, **_kwargs: replacement, downloader=downloader,
            )
        self.assertEqual(caught.exception.code, "upstream_identity_changed")
        downloader.assert_not_called()

    def test_matching_generated_archive_release_archive_and_raw_asset_proceed(self):
        archive_bytes = tar_payload()
        cases = []

        generated_recipe = archive_recipe(
            asset_name="", archive_source="github_source", asset_selection="pattern",
            name_pattern="", archive_format="tar.gz",
        )
        generated_release = {
            **release(), "assets": [], "commit": COMMIT_A, "ref_object_sha": COMMIT_B,
            "tarball_url": "https://api.github.com/repos/example/demo/tarball/v1.2.3",
        }
        generated_expected = source_archive_identity(
            generated_recipe, generated_release, generated_release=True,
        )
        cases.append((generated_recipe, generated_release, generated_expected, archive_bytes, "source_archive"))

        asset_recipe = archive_recipe()
        asset_release = release(size=len(archive_bytes))
        asset_expected = release_asset_identity(asset_recipe, asset_release, asset_release["assets"][0], "archive")
        cases.append((asset_recipe, asset_release, asset_expected, archive_bytes, "archive"))

        raw_recipe = archive_recipe(asset_name="install.sh")
        raw_bytes = b"#!/bin/sh\n"
        raw_release = release(name="install.sh", size=len(raw_bytes))
        raw_expected = release_asset_identity(raw_recipe, raw_release, raw_release["assets"][0], "raw_file")
        cases.append((raw_recipe, raw_release, raw_expected, raw_bytes, "raw_file"))

        for configured, rel, expected, payload, kind in cases:
            seen_urls = []

            def downloader(url, destination, token=""):
                seen_urls.append(url)
                Path(destination).write_bytes(payload)
                return {
                    "path": str(destination), "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }

            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                result = upstream_archive.resolve_and_extract(
                    configured, temporary, expected_identity=expected,
                    release_resolver=lambda *_args, value=rel, **_kwargs: value,
                    downloader=downloader,
                )
            self.assertEqual(result["upstream_identity"], expected)
            if kind == "source_archive":
                self.assertTrue(seen_urls[0].endswith("/" + COMMIT_A))
            else:
                self.assertEqual(
                    seen_urls[0],
                    f"https://api.github.com/repos/example/demo/releases/assets/{rel['assets'][0]['asset_id']}",
                )

    def test_upstream_deb_release_or_asset_drift_fails_before_download(self):
        configured = deb_recipe()
        original = release(name="demo_1.2.3_amd64.deb")
        expected = release_asset_identity(configured, original, original["assets"][0], "deb")
        for changed in (
            release(release_id=11, name="demo_1.2.3_amd64.deb"),
            release(asset_id=21, name="demo_1.2.3_amd64.deb"),
        ):
            downloader = mock.Mock()
            with self.subTest(changed=changed["release_id"]), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(upstream_artifact.UpstreamArtifactError) as caught:
                    upstream_artifact.acquire(
                        configured, temporary, expected_identity=expected,
                        release_resolver=lambda *_args, value=changed, **_kwargs: value,
                        downloader=downloader,
                    )
            self.assertEqual(caught.exception.code, "upstream_identity_changed")
            downloader.assert_not_called()

    def test_explicit_release_asset_does_not_fall_back_to_generated_source(self):
        configured = archive_recipe()
        admitted = release()
        expected = release_asset_identity(
            configured, admitted, admitted["assets"][0], "archive",
        )
        moved = {
            **release(), "assets": [],
            "commit": COMMIT_A, "ref_object_sha": COMMIT_B,
            "tarball_url": "https://api.github.com/repos/example/demo/tarball/v1.2.3",
        }
        downloader = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            upstream_archive.UpstreamArchiveError,
        ) as caught:
            upstream_archive.resolve_and_extract(
                configured, temporary, expected_identity=expected,
                release_resolver=lambda *_args, **_kwargs: moved,
                downloader=downloader,
            )
        self.assertEqual(caught.exception.code, "release_asset_not_found")
        downloader.assert_not_called()

    def test_matching_deb_identity_verifies_bytes_and_manual_unpinned_remains_supported(self):
        payload = b"debdata"
        digest = hashlib.sha256(payload).hexdigest()
        configured = deb_recipe()
        rel = release(name="demo_1.2.3_amd64.deb", digest="sha256:" + digest)
        expected = release_asset_identity(configured, rel, rel["assets"][0], "deb")

        seen_urls = []

        def downloader(url, destination, token=""):
            seen_urls.append(url)
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": digest}

        inspection = {"package": "demo", "version": "1.2.3-1", "architecture": "amd64"}
        for pin in (expected, None):
            with self.subTest(pinned=pin is not None), tempfile.TemporaryDirectory() as temporary:
                result = upstream_artifact.acquire(
                    configured, temporary, expected_identity=pin,
                    release_resolver=lambda *_args, **_kwargs: rel, downloader=downloader,
                    inspector=lambda *_args, **_kwargs: inspection,
                )
            self.assertEqual(result["upstream_identity"]["asset_id"], "20")
        self.assertEqual(seen_urls[0], "https://api.github.com/repos/example/demo/releases/assets/20")
        self.assertEqual(seen_urls[1], rel["assets"][0]["url"])

    def test_manual_latest_release_admission_persists_exact_provenance(self):
        configured = source_recipe(tracking="latest_release", ref="")
        resolved = {
            **ref_resolution(configured, COMMIT_A),
            "release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3",
        }
        expected = source_archive_identity(configured, resolved, generated_release=True)
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = build_pipeline.create_pipeline_run(
                runtime_recipe_for_storage(configured), store=store, dry_run=True,
                manual_source_provenance=expected,
            )
            persisted = store.load(run["id"])
            self.assertEqual(persisted["manual_source_provenance"], expected)
            persisted["manual_source_provenance"] = {
                **expected, "commit_sha": COMMIT_B,
            }
            with self.assertRaisesRegex(ValueError, "admission metadata seal"):
                store.save(persisted)

    def test_manual_latest_release_admission_uses_fresh_exact_detection(self):
        configured = source_recipe(tracking="latest_release", ref="")
        resolved = {
            **ref_resolution(configured, COMMIT_A),
            "release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3",
        }
        expected = source_archive_identity(configured, resolved, generated_release=True)
        with mock.patch.object(
            app.upstream_detection, "detect_upstream",
            return_value={"identity": expected, "display_version": "1.2.3", "display_ref": "v1.2.3"},
        ) as detect, mock.patch.object(app, "github_token", return_value="secret-token"):
            captured = app._resolve_manual_source_provenance(configured)
        self.assertEqual(captured, expected)
        detect.assert_called_once_with(configured, token="secret-token")

    def test_manual_enqueue_forwards_detected_identity_into_durable_creation(self):
        configured = runtime_recipe_for_storage(
            source_recipe(tracking="latest_release", ref=""),
        )
        resolved = {
            **ref_resolution(source_recipe(tracking="latest_release", ref=""), COMMIT_A),
            "release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3",
        }
        expected = source_archive_identity(configured, resolved, generated_release=True)
        manager = object()
        with mock.patch.object(
            app, "_prepare_run_admission", return_value=(configured, {"contract": "test"}),
        ), mock.patch.object(
            app, "_resolve_manual_source_provenance", return_value=expected,
        ), mock.patch.object(
            app, "_enqueue_prepared_recipe_run", return_value={"run_id": "run", "status": "queued"},
        ) as enqueue:
            result = app.enqueue_recipe_run(manager, configured, dry_run=True)
        self.assertEqual(result, {"run_id": "run", "status": "queued"})
        enqueue.assert_called_once_with(
            manager, configured, {"contract": "test"}, dry_run=True,
            manual_source_provenance=expected,
        )

    def test_manual_exact_ref_admission_does_not_add_latest_release_resolution(self):
        configured = source_recipe(tracking="manual", ref="v1.2.3")
        with mock.patch.object(app.upstream_detection, "detect_upstream") as detect:
            self.assertIsNone(app._resolve_manual_source_provenance(configured))
        detect.assert_not_called()

    def test_manual_latest_release_matching_identity_succeeds_but_movement_fails_closed(self):
        configured = source_recipe(tracking="latest_release", ref="")
        admitted_resolution = {
            **ref_resolution(configured, COMMIT_A),
            "release_id": 10, "ref": "v1.2.3", "tag": "v1.2.3",
        }
        expected = source_archive_identity(
            configured, admitted_resolution, generated_release=True,
        )
        moved_resolution = {**admitted_resolution, "commit": COMMIT_B}
        moved = source_archive_identity(
            configured, moved_resolution, generated_release=True,
        )

        for actual, source_status, error_code in (
            (expected, "success", "project_not_detected"),
            (moved, "failed", "upstream_identity_changed"),
        ):
            with self.subTest(moved=actual == moved), tempfile.TemporaryDirectory() as temporary:
                store = BuildStore(Path(temporary) / "builds")
                run = build_pipeline.create_pipeline_run(
                    runtime_recipe_for_storage(configured), store=store, dry_run=True,
                    manual_source_provenance=expected,
                )

                def acquire(_recipe, workspace, token="", expected_identity=None):
                    self.assertEqual(expected_identity, expected)
                    return {
                        **admitted_resolution,
                        "upstream_identity": actual,
                        "source_directory": str(Path(workspace) / "source"),
                    }

                result = build_pipeline.execute_pipeline_run(
                    run["id"], store=store, acquire=acquire,
                )
            self.assertEqual(result["steps"][0]["status"], source_status)
            self.assertEqual(result["error"]["code"], error_code)

    def test_historical_v3_latest_release_run_is_rejected_before_acquisition(self):
        configured = source_recipe(tracking="latest_release", ref="")
        acquire = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = store.create(
                runtime_recipe_for_storage(configured), mode="dry_run",
                run_id="historical-unpinned",
            )
            path = store.run_dir(run["id"]) / "run.json"
            historical = json.loads(path.read_text())
            historical["schema_version"] = 3
            path.write_text(json.dumps(historical))
            with self.assertRaises(RunDocumentError) as raised:
                build_pipeline.execute_pipeline_run(
                    run["id"], store=store, acquire=acquire,
                )
        self.assertEqual(raised.exception.code, "unsupported_run_schema_version")
        acquire.assert_not_called()

    def test_manual_exact_ref_keeps_existing_unpinned_behavior(self):
        configured = source_recipe(tracking="manual", ref="v1.2.3")
        actual = source_archive_identity(
            configured, ref_resolution(configured), generated_release=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            run = build_pipeline.create_pipeline_run(
                runtime_recipe_for_storage(configured), store=store, dry_run=True,
            )

            def acquire(_recipe, workspace, token="", expected_identity=None):
                return {
                    **ref_resolution(configured),
                    "upstream_identity": actual,
                    "source_directory": str(Path(workspace) / "source"),
                }

            result = build_pipeline.execute_pipeline_run(
                run["id"], store=store, acquire=acquire,
            )
        self.assertEqual(result["steps"][0]["status"], "success")
        self.assertEqual(result["error"]["code"], "project_not_detected")

    def test_automated_run_rejects_a_recipe_snapshot_hash_mismatch(self):
        configured = source_recipe(policy="build")
        expected = source_archive_identity(configured, ref_resolution(configured), generated_release=False)
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            recipe_sha = canonical_recipe_sha256(configured)
            run = store.create(
                runtime_recipe_for_storage(configured), mode="build", run_id="tampered", recipe_id="demo",
                origin={"kind": "automation", "trigger": "upstream_change", "reason": "ref_advanced"},
                automation={
                    "attempt_key": automation_attempt_key("demo", expected, recipe_sha),
                    "generation": 0, "policy": "build", "expected_upstream_identity": expected,
                },
            )
            changed = copy.deepcopy(configured)
            changed["package"]["description"] = "changed after admission"
            (store.run_dir(run["id"]) / "recipe.json").write_text(
                json.dumps(runtime_recipe_for_storage(changed), indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
            )
            with self.assertRaises(build_pipeline.PipelineRunError) as caught:
                build_pipeline.execute_pipeline_run(run["id"], store=store)
        self.assertEqual(caught.exception.code, "build_run_recipe_mismatch")

    def test_controlled_automated_run_uses_its_expected_identity(self):
        configured = source_recipe(policy="build")
        expected_resolution = ref_resolution(configured, COMMIT_A)
        expected = source_archive_identity(configured, expected_resolution, generated_release=False)
        actual = source_archive_identity(configured, ref_resolution(configured, COMMIT_B), generated_release=False)
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            recipe_sha = canonical_recipe_sha256(configured)
            run = store.create(
                runtime_recipe_for_storage(configured), mode="build", run_id="pinned", recipe_id="demo",
                origin={"kind": "automation", "trigger": "upstream_change", "reason": "ref_advanced"},
                automation={
                    "attempt_key": automation_attempt_key("demo", expected, recipe_sha),
                    "generation": 0, "policy": "build", "expected_upstream_identity": expected,
                },
            )

            def wrong_source(_recipe, _workspace, token="", expected_identity=None):
                return {"upstream_identity": actual}

            result = build_pipeline.execute_pipeline_run(run["id"], store=store, acquire=wrong_source)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["stage"], "source")
        self.assertEqual(result["error"]["code"], "upstream_identity_changed")


if __name__ == "__main__":
    unittest.main()
