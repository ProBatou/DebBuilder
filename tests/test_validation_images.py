import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from debbuilder.validation_images import (
    ValidationImageError, admitted_image, descriptor, host_architecture,
    provision_admitted_image, provision_image, validate_manifest,
)
from debbuilder.execution_cancellation import ExecutionCancelled


REFERENCE = "ghcr.io/probatou/debbuilder-validation-bookworm@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


def manifest():
    return {"schema_version": 1, "images": [{
        "profile": "bookworm", "architecture": "amd64", "oci_architecture": "amd64",
        "repository": "ghcr.io/probatou/debbuilder-validation-bookworm", "digest": "sha256:" + "a" * 64,
    }]}


class FakeRuntime:
    def __init__(self, *, missing=True, digest=REFERENCE, architecture="amd64", pull_success=True):
        self.missing = missing
        self.digest = digest
        self.architecture = architecture
        self.pull_success = pull_success
        self.mutable_tag = {}
        self.calls = []
        self.cancellation_event = threading.Event()
        self.lock = threading.Lock()

    def run(self, argv, *, timeout, workload=False, cancellable_control=False, output_limit=None):
        with self.lock:
            self.calls.append((list(argv), workload or cancellable_control))
        if argv[:3] == ["podman", "image", "exists"]:
            return {"status": "failed", "exit_code": 1} if self.missing else {"status": "success"}
        if argv[:3] == ["podman", "image", "inspect"]:
            if argv[-1] in self.mutable_tag:
                return {"status": "success", "stdout": json.dumps([{"Id": self.mutable_tag[argv[-1]], "RepoDigests": [], "Architecture": "amd64"}])}
            return {"status": "success", "stdout": json.dumps([{
                "Id": IMAGE_ID, "RepoDigests": [self.digest], "Architecture": self.architecture,
            }])}
        if argv[:2] == ["podman", "pull"]:
            if self.pull_success:
                self.missing = False
                return {"status": "success"}
            return {"status": "failed", "exit_code": 125}
        raise AssertionError(argv)


class ValidationImageTests(unittest.TestCase):
    def test_contract_and_host_architecture(self):
        self.assertEqual(validate_manifest(manifest()), manifest())
        self.assertEqual(host_architecture("x86_64"), "amd64")
        self.assertEqual(host_architecture("aarch64"), "arm64")
        self.assertEqual(descriptor("bookworm", "amd64", manifest())["digest"], "sha256:" + "a" * 64)
        for field, value in (("digest", "sha256:short"), ("digest", "latest"), ("repository", "debbuilder:latest"), ("profile", "other"), ("oci_architecture", "arm64")):
            with self.subTest(field=field, value=value):
                invalid = manifest()
                invalid["images"][0][field] = value
                with self.assertRaises(ValidationImageError):
                    validate_manifest(invalid)
        with self.assertRaises(ValidationImageError) as error:
            descriptor("bookworm", "arm64", manifest())
        self.assertEqual(error.exception.code, "validation_profile_architecture_unsupported")

    def test_exact_local_reuse_and_wrong_tag_is_ignored(self):
        runtime = FakeRuntime(missing=False)
        runtime.mutable_tag = {"debbuilder-validation:bookworm": "sha256:" + "c" * 64}
        self.assertEqual(provision_image(runtime, "bookworm", manifest=manifest(), architecture="amd64")["id"], IMAGE_ID)
        self.assertFalse(any(call[:2] == ["podman", "pull"] for call, _ in runtime.calls))
        self.assertTrue(all(REFERENCE in call for call, _ in runtime.calls))

    def test_pull_inspects_digest_architecture_and_id(self):
        runtime = FakeRuntime()
        image = provision_image(runtime, "bookworm", manifest=manifest(), architecture="amd64")
        self.assertEqual(image, {"name": REFERENCE, "id": IMAGE_ID, "digest": REFERENCE})
        self.assertIn((["podman", "pull", "--quiet", REFERENCE], True), runtime.calls)
        self.assertEqual(sum(call[:3] == ["podman", "image", "inspect"] for call, _ in runtime.calls), 1)

    def test_bad_post_pull_evidence_fails_closed(self):
        for options, code in (({"digest": "ghcr.io/probatou/debbuilder-validation-bookworm@sha256:" + "c" * 64}, "validation_image_digest_mismatch"), ({"architecture": "arm64"}, "validation_image_architecture_mismatch")):
            with self.subTest(options=options):
                with self.assertRaises(ValidationImageError) as error:
                    provision_image(FakeRuntime(**options), "bookworm", manifest=manifest(), architecture="amd64")
                self.assertEqual(error.exception.code, code)

    def test_registry_outage_retries_without_ready_state(self):
        runtime = FakeRuntime(pull_success=False)
        for _ in range(2):
            with self.assertRaises(ValidationImageError) as error:
                provision_image(runtime, "bookworm", manifest=manifest(), architecture="amd64")
            self.assertEqual(error.exception.code, "validation_image_provisioning_retryable")
        self.assertEqual(sum(call[:2] == ["podman", "pull"] for call, _ in runtime.calls), 2)

    def test_concurrent_requests_share_one_pull(self):
        runtime = FakeRuntime()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(provision_image, runtime, "bookworm", manifest=manifest(), architecture="amd64") for _ in range(2)]
            self.assertEqual(futures[0].result(), futures[1].result())
        self.assertEqual(sum(call[:2] == ["podman", "pull"] for call, _ in runtime.calls), 1)

    def test_admitted_digest_does_not_follow_later_manifest(self):
        pinned = admitted_image("bookworm", manifest=manifest(), architecture="amd64")
        later = manifest()
        later["images"][0]["digest"] = "sha256:" + "c" * 64
        runtime = FakeRuntime()
        self.assertEqual(provision_admitted_image(runtime, "bookworm", pinned, production=False, architecture="amd64")["name"], REFERENCE)
        self.assertEqual(admitted_image("bookworm", manifest=later, architecture="amd64")["name"].split("@")[-1], "sha256:" + "c" * 64)

    def test_cancelled_pull_never_admits_image(self):
        class CancelledRuntime(FakeRuntime):
            def run(self, argv, **kwargs):
                if argv[:2] == ["podman", "pull"]:
                    self.cancellation_event.set()
                    return {"status": "cancelled", "cancellation_requested": True}
                return super().run(argv, **kwargs)
        runtime = CancelledRuntime()
        with self.assertRaises(ExecutionCancelled):
            provision_admitted_image(runtime, "bookworm", admitted_image("bookworm", manifest=manifest(), architecture="amd64"), production=False, architecture="amd64")
        self.assertTrue(runtime.missing)


if __name__ == "__main__":
    unittest.main()
