import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import __version__, builtin_recipe, debian_packaging, release_build


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CURRENT_TAG = f"v{__version__}"
CURRENT_REVISION = builtin_recipe.load_builtin_definition()["package"]["version_revision"]
CURRENT_DEBIAN_VERSION = f"{__version__}-{CURRENT_REVISION}" if CURRENT_REVISION else __version__
CURRENT_FILENAME = f"debbuilder_{CURRENT_DEBIAN_VERSION}_all.deb"


def fixture_images(root: Path) -> Path:
    path = root / "images.json"
    path.write_text(json.dumps({"schema_version": 1, "images": [
        {"profile": profile, "architecture": "amd64", "oci_architecture": "amd64",
         "repository": f"ghcr.io/probatou/debbuilder-validation-{suffix}", "digest": "sha256:" + digit * 64}
        for profile, suffix, digit in (("bookworm", "bookworm", "a"), ("bookworm-node22", "node22", "b"))
    ]}))
    return path


class ReleasePlanTests(unittest.TestCase):
    def test_plan_uses_canonical_recipe_for_all_artifact_identity(self):
        plan = release_build.release_plan(CURRENT_TAG)

        self.assertEqual(plan["package"], "debbuilder")
        self.assertEqual(plan["upstream_version"], __version__)
        self.assertEqual(plan["debian_revision"], plan["definition"]["package"]["version_revision"])
        self.assertEqual(plan["debian_version"], CURRENT_DEBIAN_VERSION)
        self.assertEqual(plan["architecture"], plan["definition"]["package"]["architecture"])
        self.assertEqual(plan["filename"], CURRENT_FILENAME)
        self.assertEqual(plan["definition_version"], plan["definition"]["management"]["definition_version"])

    def test_wrong_or_unsafe_tag_is_rejected(self):
        for tag, code in (
            ("v9.9.9", "release_version_mismatch"),
            (__version__, "invalid_release_tag"),
            (f"{CURRENT_TAG};false", "invalid_release_tag"),
        ):
            with self.subTest(tag=tag), self.assertRaises(release_build.ReleaseBuildError) as raised:
                release_build.release_plan(tag)
            self.assertEqual(raised.exception.code, code)

    def test_production_runtime_output_paths_are_refused_before_writing(self):
        target = Path("/var/lib/debbuilder/release-assets-unit-test")
        self.assertFalse(target.exists())
        with self.assertRaises(release_build.ReleaseBuildError) as raised:
            release_build.build_release_artifacts(
                tag=CURRENT_TAG, source_root=REPOSITORY_ROOT, output_directory=target,
            )
        self.assertEqual(raised.exception.code, "unsafe_release_output")
        self.assertFalse(target.exists())

    def test_release_build_requires_immutable_image_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "release-assets"
            with self.assertRaises(release_build.ReleaseBuildError) as raised:
                release_build.build_release_artifacts(
                    tag=CURRENT_TAG, source_root=REPOSITORY_ROOT, output_directory=output,
                )
            self.assertEqual(raised.exception.code, "release_validation_images_missing")
            self.assertFalse(output.exists())

    def test_inaccessible_public_descriptor_fails_closed(self):
        images = json.loads((REPOSITORY_ROOT / "debbuilder/validation_images.json").read_text())
        failed = subprocess.CompletedProcess([], 1, "", "registry unavailable")
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "debbuilder.release_build.subprocess.run", return_value=failed,
        ):
            with self.assertRaises(release_build.ReleaseBuildError) as raised:
                release_build._prove_public_images(images, Path(temporary))
        self.assertEqual(raised.exception.code, "release_validation_image_unproven")


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
class RealReleaseBuildTests(unittest.TestCase):
    def test_checked_in_manifest_must_match_verified_release_descriptors(self):
        source_manifest = REPOSITORY_ROOT / "debbuilder/validation_images.json"
        with tempfile.TemporaryDirectory() as temporary, mock.patch("debbuilder.release_build._prove_public_images") as public_proof:
            root = Path(temporary)
            result = release_build.build_release_artifacts(
                tag=CURRENT_TAG, source_root=REPOSITORY_ROOT,
                output_directory=root / "matching-assets", validation_images=source_manifest,
            )
            self.assertTrue(Path(result["artifact"]["path"]).is_file())
            public_proof.assert_called_once()
            with self.assertRaises(release_build.ReleaseBuildError) as raised:
                release_build.build_release_artifacts(
                    tag=CURRENT_TAG, source_root=REPOSITORY_ROOT,
                    output_directory=root / "conflicting-assets",
                    validation_images=fixture_images(root),
                )
            self.assertEqual(raised.exception.code, "release_validation_images_conflict")
            self.assertFalse((root / "conflicting-assets").exists())

    def test_builds_checked_canonical_asset_and_cleans_temporary_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            temporary_parent = root / "temporary"
            temporary_parent.mkdir()
            output = root / "release-assets"

            result = release_build.build_release_artifacts(
                tag=CURRENT_TAG, source_root=REPOSITORY_ROOT,
                output_directory=output, temporary_parent=temporary_parent,
                validation_images=fixture_images(root), _allow_test_image_fixture=True,
            )

            artifact = output / CURRENT_FILENAME
            self.assertEqual(result["artifact"]["path"], str(artifact))
            self.assertTrue(artifact.is_file())
            self.assertGreater(result["artifact"]["size"], 0)
            self.assertEqual(result["checks"]["metadata"], {
                "package": "debbuilder", "version": CURRENT_DEBIAN_VERSION, "architecture": "all",
            })
            self.assertEqual(result["checks"]["depends"], "python3, python3-dbus, reprepro, gnupg, gpgv, podman, kmod, ca-certificates")
            control_dir = root / "control"
            subprocess.run(["dpkg-deb", "-e", str(artifact), str(control_dir)], check=True)
            postinst = (control_dir / "postinst").read_text()
            self.assertIn("PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 /opt/debbuilder/bootstrap_local.py", postinst)
            self.assertEqual(result["checks"]["runtime_data_directory"], "/var/lib/debbuilder")
            self.assertFalse(result["checks"]["mutable_application_data_present"])
            self.assertFalse(result["checks"]["generated_python_cache_present"])
            self.assertEqual(
                [line for line in result["checks"]["unit"].splitlines() if line.startswith("EnvironmentFile=")],
                ["EnvironmentFile=/etc/debbuilder/debbuilder.env"],
            )
            self.assertIn("ExecStartPre=/usr/bin/python3 /opt/debbuilder/bootstrap_local.py",
                          result["checks"]["unit"])
            self.assertTrue((output / CURRENT_FILENAME).is_file())
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
                "ok": True, "package": "other", "version": CURRENT_DEBIAN_VERSION, "architecture": "all",
                "depends": "python3, python3-dbus", "files": [], "file_count": 0,
                "maintainer_scripts": [], "conffiles": [], "warnings": [], "control": {},
            }
            with mock.patch("debbuilder.release_build.deb_inspector.inspect_deb", return_value=inconsistent):
                with self.assertRaises(debian_packaging.PackagingError) as raised:
                    release_build.build_release_artifacts(
                        tag=CURRENT_TAG, source_root=REPOSITORY_ROOT, output_directory=output,
                        validation_images=fixture_images(Path(temporary)),
                        _allow_test_image_fixture=True,
                    )
            self.assertEqual(raised.exception.code, "deb_inspection_failed")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
