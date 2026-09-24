import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import release_validation_inputs


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleaseValidationInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "validation").mkdir()
        (self.root / "debbuilder").mkdir()
        for name in ("Dockerfile", "Dockerfile.node22", "validation_images.lock.json"):
            shutil.copy2(REPOSITORY_ROOT / "validation" / name, self.root / "validation" / name)
        shutil.copy2(
            REPOSITORY_ROOT / "debbuilder/validation_images.json",
            self.root / "debbuilder/validation_images.json",
        )
        self.lock = self.root / "validation/validation_images.lock.json"
        self.manifest = self.root / "debbuilder/validation_images.json"

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self):
        return release_validation_inputs.validate_release_inputs(self.root, self.lock, self.manifest)

    def test_unchanged_inputs_reuse_accepted_descriptors(self):
        result = self.validate()

        self.assertEqual([row["profile"] for row in result["profiles"]], ["bookworm", "bookworm-node22"])
        self.assertEqual(
            [row["reference"] for row in result["profiles"]],
            [
                "ghcr.io/probatou/debbuilder-validation-bookworm@sha256:871edf6fb764805c7e149a600080adb66e692952a1655ef1fb2ca883b9c6f1a7",
                "ghcr.io/probatou/debbuilder-validation-node22@sha256:e6e9e2381e3385c6bb17b9ea1859e47e1a02978ae207594eb206232e96f90bc7",
            ],
        )
        self.assertEqual({row["oci_architecture"] for row in result["profiles"]}, {"amd64"})

    def test_changed_bookworm_input_fails_closed(self):
        with (self.root / "validation/Dockerfile").open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")

        with self.assertRaises(release_validation_inputs.ValidationReleaseInputError) as raised:
            self.validate()

        self.assertEqual(raised.exception.code, "validation_image_inputs_changed")
        self.assertEqual(raised.exception.profile, "bookworm")

    def test_changed_node22_input_fails_closed(self):
        with (self.root / "validation/Dockerfile.node22").open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")

        with self.assertRaises(release_validation_inputs.ValidationReleaseInputError) as raised:
            self.validate()

        self.assertEqual(raised.exception.code, "validation_image_inputs_changed")
        self.assertEqual(raised.exception.profile, "bookworm-node22")

    def test_manifest_digest_mismatch_fails_closed(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["images"][0]["digest"] = "sha256:" + "a" * 64
        self.manifest.write_text(json.dumps(manifest))

        with self.assertRaises(release_validation_inputs.ValidationReleaseInputError) as raised:
            self.validate()

        self.assertEqual(raised.exception.code, "validation_image_descriptor_mismatch")
        self.assertEqual(raised.exception.profile, "bookworm")

    def test_lock_cannot_redirect_effective_inputs(self):
        lock = json.loads(self.lock.read_text())
        lock["profiles"]["bookworm"]["inputs"] = ["validation/README.md"]
        self.lock.write_text(json.dumps(lock))

        with self.assertRaises(release_validation_inputs.ValidationReleaseInputError) as raised:
            self.validate()

        self.assertEqual(raised.exception.code, "validation_input_lock_invalid")

    def test_normal_workflow_has_no_image_build_push_or_package_write_path(self):
        workflow = (REPOSITORY_ROOT / ".github/workflows/release.yml").read_text()
        forbidden = ("podman build", "podman push", "podman login", "packages: write", "GHCR_TOKEN")
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, workflow)
        self.assertIn("python3 -m tests.release_validation_inputs", workflow)
        self.assertIn('$GITHUB_WORKSPACE/debbuilder/validation_images.json', workflow)
        self.assertLess(
            workflow.index("python3 -m tests.release_validation_inputs"),
            workflow.index("actions/upload-artifact"),
        )


if __name__ == "__main__":
    unittest.main()
