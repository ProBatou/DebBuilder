import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import build_pipeline, upstream_artifact
from debbuilder.build_store import BuildStore
from debbuilder.recipe_schema import validate_recipe_metadata


def recipe(architecture="amd64", pattern="flood-linux-x64.deb"):
    return validate_recipe_metadata({
        "name": "flood", "package": {"name": "flood", "architecture": architecture},
        "source": {"repository": "jesec/flood", "tracking": "latest_release"},
        "artifact": {"mode": "upstream_deb", "architecture": architecture, "name_pattern": pattern},
    })


def release(assets=None):
    return {"repository": "jesec/flood", "tag": "v4.16.1", "ref": "v4.16.1", "name": "Release", "url": "https://github.com/jesec/flood/releases/tag/v4.16.1", "upstream_version": "4.16.1", "assets": assets or [{"name": "flood-linux-x64.deb", "url": "https://github.com/jesec/flood/releases/download/v4.16.1/flood-linux-x64.deb", "digest": ""}]}


class UpstreamArtifactTests(unittest.TestCase):
    def test_selection_is_deterministic(self):
        self.assertEqual(upstream_artifact.select_asset(release(), recipe()["artifact"])["name"], "flood-linux-x64.deb")
        with self.assertRaisesRegex(upstream_artifact.UpstreamArtifactError, "No deterministic") as missing:
            upstream_artifact.select_asset(release(), recipe(pattern="missing*.deb")["artifact"])
        self.assertEqual(missing.exception.code, "release_asset_not_found")
        duplicate = release(release()["assets"] * 2)
        with self.assertRaises(upstream_artifact.UpstreamArtifactError) as ambiguous:
            upstream_artifact.select_asset(duplicate, recipe()["artifact"])
        self.assertEqual(ambiguous.exception.code, "ambiguous_release_asset")

    def test_wrong_architecture_has_no_match(self):
        with self.assertRaises(upstream_artifact.UpstreamArtifactError) as caught:
            upstream_artifact.select_asset(release(), recipe(architecture="arm64", pattern="*.deb")["artifact"])
        self.assertEqual(caught.exception.code, "release_asset_not_found")

    def test_acquisition_checks_metadata_and_sha256(self):
        payload = b"upstream deb bytes"
        rel = release()
        rel["assets"][0]["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
        def resolver(_recipe, token=""):
            return rel
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        inspection = {"ok": True, "package": "flood", "version": "4.16.1-0", "architecture": "amd64", "depends": "", "maintainer_scripts": [], "files": []}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_artifact.acquire(recipe(), temporary, release_resolver=resolver, downloader=downloader, inspector=lambda *_a, **_k: inspection)
            self.assertEqual(Path(result["path"]).read_bytes(), payload)
            self.assertTrue(result["checksum_verified"])
            self.assertEqual(result["source"], "upstream_release")

    def test_metadata_mismatches_are_structured(self):
        payload = b"deb"
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": 3, "sha256": hashlib.sha256(payload).hexdigest()}
        base = {"ok": True, "package": "flood", "version": "4.16.1-0", "architecture": "amd64"}
        cases = [("package", "other", "artifact_package_mismatch"), ("version", "9.0-1", "artifact_version_mismatch"), ("architecture", "arm64", "artifact_architecture_mismatch")]
        for field, value, code in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(upstream_artifact.UpstreamArtifactError) as caught:
                    upstream_artifact.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release(), downloader=downloader, inspector=lambda *_a, **_k: {**base, field: value})
                self.assertEqual(caught.exception.code, code)

    def test_pipeline_skips_source_build_stages_and_registers_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(configured, workspace, token=""):
                target = Path(workspace) / "artifacts/flood-linux-x64.deb"
                target.write_bytes(b"deb")
                return {"path": str(target), "name": target.name, "size": 3, "sha256": "a" * 64, "source": "upstream_release", "release_asset": release()["assets"][0], "inspection": {"ok": True, "package": "flood", "version": "4.16.1-0", "architecture": "amd64", "depends": ""}}
            with mock.patch("debbuilder.upstream_artifact.resolve_release", return_value=release()):
                result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=False, upstream_acquirer=acquire)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["versions"], {"upstream": "4.16.1", "debian": "4.16.1-0"})
            statuses = {step["name"]: step["status"] for step in result["steps"]}
            for name in ("dependencies", "source_changes", "build", "staging", "debian_metadata", "systemd", "package"):
                self.assertEqual(statuses[name], "skipped")
                self.assertEqual(next(step for step in result["steps"] if step["name"] == name)["details"]["reason"], "upstream_artifact")
            self.assertEqual(statuses["artifact"], "success")


if __name__ == "__main__":
    unittest.main()
