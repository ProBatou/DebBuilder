import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import debian_packaging, release_build


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleasePlanTests(unittest.TestCase):
    def test_plan_uses_canonical_recipe_for_all_artifact_identity(self):
        plan = release_build.release_plan("v0.3.0")

        self.assertEqual(plan["package"], "debbuilder")
        self.assertEqual(plan["upstream_version"], "0.3.0")
        self.assertEqual(plan["debian_revision"], plan["definition"]["package"]["version_revision"])
        self.assertEqual(plan["debian_version"], "0.3.0-2")
        self.assertEqual(plan["architecture"], plan["definition"]["package"]["architecture"])
        self.assertEqual(plan["filename"], "debbuilder_0.3.0-2_all.deb")
        self.assertEqual(plan["definition_version"], 4)

    def test_wrong_or_unsafe_tag_is_rejected(self):
        for tag, code in (
            ("v9.9.9", "release_version_mismatch"),
            ("0.3.0", "invalid_release_tag"),
            ("v0.3.0;false", "invalid_release_tag"),
        ):
            with self.subTest(tag=tag), self.assertRaises(release_build.ReleaseBuildError) as raised:
                release_build.release_plan(tag)
            self.assertEqual(raised.exception.code, code)

    def test_production_runtime_output_paths_are_refused_before_writing(self):
        target = Path("/var/lib/debbuilder/release-assets-unit-test")
        self.assertFalse(target.exists())
        with self.assertRaises(release_build.ReleaseBuildError) as raised:
            release_build.build_release_artifacts(
                tag="v0.3.0", source_root=REPOSITORY_ROOT, output_directory=target,
            )
        self.assertEqual(raised.exception.code, "unsafe_release_output")
        self.assertFalse(target.exists())


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
class RealReleaseBuildTests(unittest.TestCase):
    def test_builds_checked_canonical_asset_and_cleans_temporary_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            temporary_parent = root / "temporary"
            temporary_parent.mkdir()
            output = root / "release-assets"

            result = release_build.build_release_artifacts(
                tag="v0.3.0", source_root=REPOSITORY_ROOT,
                output_directory=output, temporary_parent=temporary_parent,
            )

            artifact = output / "debbuilder_0.3.0-2_all.deb"
            self.assertEqual(result["artifact"]["path"], str(artifact))
            self.assertTrue(artifact.is_file())
            self.assertGreater(result["artifact"]["size"], 0)
            self.assertEqual(result["checks"]["metadata"], {
                "package": "debbuilder", "version": "0.3.0-2", "architecture": "all",
            })
            self.assertEqual(result["checks"]["depends"], "python3, python3-dbus")
            self.assertEqual(result["checks"]["runtime_data_directory"], "/var/lib/debbuilder")
            self.assertFalse(result["checks"]["mutable_application_data_present"])
            self.assertFalse(result["checks"]["generated_python_cache_present"])
            self.assertEqual(
                [line for line in result["checks"]["unit"].splitlines() if line.startswith("EnvironmentFile=")],
                ["EnvironmentFile=/etc/debbuilder/debbuilder.env"],
            )
            for directive in (
                "KillMode=control-group", "Restart=on-failure", "RestartSec=3s",
                "TimeoutStopSec=20s", "KillSignal=SIGTERM",
            ):
                self.assertIn(directive, result["checks"]["unit"])
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(result["artifact"]["sha256"], digest)
            self.assertEqual((output / "SHA256SUMS").read_text(), f"{digest}  {artifact.name}\n")
            self.assertEqual(list(temporary_parent.iterdir()), [])

    def test_inconsistent_package_metadata_fails_without_publishing_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release-assets"
            inconsistent = {
                "ok": True, "package": "other", "version": "0.3.0-2", "architecture": "all",
                "depends": "python3, python3-dbus", "files": [], "file_count": 0,
                "maintainer_scripts": [], "conffiles": [], "warnings": [], "control": {},
            }
            with mock.patch("debbuilder.release_build.deb_inspector.inspect_deb", return_value=inconsistent):
                with self.assertRaises(debian_packaging.PackagingError) as raised:
                    release_build.build_release_artifacts(
                        tag="v0.3.0", source_root=REPOSITORY_ROOT, output_directory=output,
                    )
            self.assertEqual(raised.exception.code, "deb_inspection_failed")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
