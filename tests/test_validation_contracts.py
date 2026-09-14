import copy
import json
import unittest

from debbuilder.validation_contracts import (
    PREPARED_DEPENDENCIES_CONTRACT_VERSION,
    VALIDATION_ATTEMPT_CONTRACT_VERSION,
    ValidationContractError,
    normalize_prepared_runtime_dependencies,
    normalize_validation_attempt,
)


def artifact(*, package="demo", version="1.0-1", architecture="amd64", sha256="a" * 64):
    return {
        "package": package,
        "version": version,
        "architecture": architecture,
        "size": 1234,
        "sha256": sha256,
    }


def prepared(*, packages=None, repositories=None, diagnostics=None):
    return {
        "contract_version": PREPARED_DEPENDENCIES_CONTRACT_VERSION,
        "profile_name": "bookworm",
        "image": {
            "name": "debbuilder-validation:bookworm",
            "id": "sha256:" + "1" * 64,
            "digest": "localhost/debbuilder-validation@sha256:" + "2" * 64,
        },
        "native_architecture": "amd64",
        "artifacts": {"current": artifact(), "previous": None},
        "repositories": repositories or [],
        "base_packages": [],
        "packages": packages or [],
        "started_at": "2026-09-12T10:00:01+00:00",
        "finished_at": "2026-09-12T10:00:02+00:00",
        "diagnostics": diagnostics or [],
        "enforcement": [{"control": "memory", "status": "enforced", "observed": "limit verified"}],
    }


def attempt(status="queued"):
    value = {
        "contract_version": VALIDATION_ATTEMPT_CONTRACT_VERSION,
        "id": "validation-1",
        "build_run_id": "run-1",
        "inputs": {"profile": "bookworm", "artifact": artifact(), "previous_artifact": None},
        "selected_profile": {
            "name": "bookworm",
            "image": {"name": "debbuilder-validation:bookworm", "id": None, "digest": None},
        },
        "created_at": "2026-09-12T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "status": status,
        "prepared_dependencies": None,
        "result": None,
        "error": None,
    }
    if status in {"running", "cancelling", "success", "failed"}:
        value["started_at"] = "2026-09-12T10:00:01+00:00"
    if status in {"cancelled", "success", "failed"}:
        value["finished_at"] = "2026-09-12T10:00:02+00:00"
    if status in {"success", "failed"}:
        value["result"] = {"status": status, "reference": "validation/validation-1/result.json"}
    if status in {"cancelled", "failed"}:
        value["error"] = {"code": "validation_cancelled" if status == "cancelled" else "validation_failed", "message": "Validation did not complete"}
    if status == "success":
        value["prepared_dependencies"] = prepared()
        value["selected_profile"]["image"] = copy.deepcopy(value["prepared_dependencies"]["image"])
    return value


class ValidationAttemptContractTests(unittest.TestCase):
    def test_current_contract_round_trips_for_every_status(self):
        for status in {"queued", "running", "cancelling", "cancelled", "success", "failed"}:
            with self.subTest(status=status):
                value = attempt(status)
                self.assertEqual(normalize_validation_attempt(value), value)

    def test_historical_unversioned_record_is_tagged_on_a_copy(self):
        historical = {
            "id": "legacy-validation",
            "status": "success",
            "artifact": "/historical/workspace/artifacts/demo.deb",
            "checks": [{"name": "package_install", "status": "success"}],
        }
        before = copy.deepcopy(historical)
        normalized = normalize_validation_attempt(historical)
        self.assertEqual(normalized["contract_version"], 0)
        self.assertEqual(normalized["artifact"], historical["artifact"])
        self.assertEqual(historical, before)

    def test_future_and_invalid_attempt_versions_fail_closed(self):
        for version, code in ((2, "future_contract_version"), (-1, "unsupported_contract_version"), (True, "invalid_contract_version")):
            value = attempt()
            value["contract_version"] = version
            with self.subTest(version=version), self.assertRaises(ValidationContractError) as raised:
                normalize_validation_attempt(value)
            self.assertEqual(raised.exception.code, code)

    def test_missing_identity_and_invalid_status_are_rejected(self):
        missing = attempt()
        missing.pop("build_run_id")
        with self.assertRaises(ValidationContractError):
            normalize_validation_attempt(missing)
        invalid = attempt()
        invalid["status"] = "pending"
        with self.assertRaises(ValidationContractError):
            normalize_validation_attempt(invalid)

    def test_status_timestamps_result_and_error_must_be_consistent(self):
        cases = []
        queued_started = attempt()
        queued_started["started_at"] = "2026-09-12T10:00:01+00:00"
        cases.append(queued_started)
        running_finished = attempt("running")
        running_finished["finished_at"] = "2026-09-12T10:00:02+00:00"
        cases.append(running_finished)
        success_with_error = attempt("success")
        success_with_error["error"] = {"code": "unexpected", "message": "Unexpected error"}
        cases.append(success_with_error)
        failed_without_error = attempt("failed")
        failed_without_error["error"] = None
        cases.append(failed_without_error)
        queued_prepared = attempt()
        queued_prepared["prepared_dependencies"] = prepared()
        cases.append(queued_prepared)
        success_without_prepared = attempt("success")
        success_without_prepared["prepared_dependencies"] = None
        cases.append(success_without_prepared)
        for value in cases:
            with self.subTest(status=value["status"]), self.assertRaises(ValidationContractError):
                normalize_validation_attempt(value)

    def test_profile_and_prepared_summary_must_match_immutable_input(self):
        value = attempt("success")
        self.assertEqual(normalize_validation_attempt(value), value)
        value["selected_profile"]["name"] = "bookworm-node22"
        with self.assertRaises(ValidationContractError):
            normalize_validation_attempt(value)

    def test_prepared_summary_is_bound_to_artifacts_image_and_attempt_timestamps(self):
        mutations = []
        wrong_artifact = attempt("success")
        wrong_artifact["prepared_dependencies"]["artifacts"]["current"]["sha256"] = "b" * 64
        mutations.append(wrong_artifact)
        wrong_image = attempt("success")
        wrong_image["prepared_dependencies"]["image"]["id"] = "sha256:" + "3" * 64
        mutations.append(wrong_image)
        early_preparation = attempt("success")
        early_preparation["prepared_dependencies"]["started_at"] = "2026-09-12T09:59:59+00:00"
        mutations.append(early_preparation)
        for value in mutations:
            with self.assertRaises(ValidationContractError):
                normalize_validation_attempt(value)


class PreparedRuntimeDependenciesContractTests(unittest.TestCase):
    def test_empty_dependency_set_is_valid_and_round_trips(self):
        value = prepared()
        self.assertEqual(normalize_prepared_runtime_dependencies(value), value)

    def test_exact_base_satisfiers_are_phase_scoped(self):
        value = prepared()
        value["base_packages"] = [{
            "package": "ca-certificates", "version": "20250419~deb12u1",
            "architecture": "amd64", "role": "current",
        }]
        self.assertEqual(normalize_prepared_runtime_dependencies(value)["base_packages"], value["base_packages"])
        value["base_packages"][0]["role"] = "unknown"
        with self.assertRaises(ValidationContractError):
            normalize_prepared_runtime_dependencies(value)

    def test_cp1a_v1_draft_without_base_satisfiers_remains_readable(self):
        value = prepared()
        value.pop("base_packages")
        self.assertEqual(normalize_prepared_runtime_dependencies(value)["base_packages"], [])

    def test_package_and_repository_provenance_round_trip_without_raw_key(self):
        repository = {
            "id": "vendor-runtime",
            "uri": "https://apt.example.invalid/runtime",
            "suite": "nodistro",
            "components": ["main"],
            "signing_key_sha256": "b" * 64,
            "signing_key_fingerprints": ["C" * 40, "D" * 40],
        }
        package = {
            "package": "runtime-lib",
            "version": "2:3.4.5-1",
            "architecture": "amd64",
            "role": "current",
            "size": 9876,
            "sha256": "e" * 64,
            "origin": {"repository_id": "vendor-runtime", "suite": "nodistro", "component": "main"},
        }
        normalized = normalize_prepared_runtime_dependencies(prepared(packages=[package], repositories=[repository]))
        self.assertEqual(normalized["packages"], [package])
        self.assertEqual(normalized["repositories"], [repository])
        serialized = json.dumps(normalized)
        self.assertNotIn("PGP PUBLIC KEY", serialized)
        self.assertNotIn("armored", serialized)
        self.assertNotIn("local_path", serialized)

    def test_repository_provenance_requires_hash_and_verified_fingerprint(self):
        repository = {
            "id": "vendor-runtime", "uri": "https://apt.example.invalid/runtime",
            "suite": "nodistro", "components": ["main"],
            "signing_key_sha256": "b" * 64, "signing_key_fingerprints": ["C" * 40],
        }
        for field, replacement in (("signing_key_sha256", None), ("signing_key_fingerprints", [])):
            invalid = copy.deepcopy(repository)
            invalid[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(prepared(repositories=[invalid]))

    def test_package_origin_must_match_recorded_repository(self):
        repository = {
            "id": "vendor-runtime", "uri": "https://apt.example.invalid/runtime",
            "suite": "nodistro", "components": ["main"],
            "signing_key_sha256": "b" * 64, "signing_key_fingerprints": ["C" * 40],
        }
        package = {
            "package": "runtime-lib", "version": "1.0-1", "architecture": "amd64",
            "role": "current", "size": 10, "sha256": "f" * 64,
            "origin": {"repository_id": "vendor-runtime", "suite": "nodistro", "component": "main"},
        }
        for origin in (
            {**package["origin"], "repository_id": "missing"},
            {**package["origin"], "suite": "other"},
            {**package["origin"], "component": "contrib"},
        ):
            invalid = {**package, "origin": origin}
            with self.subTest(origin=origin), self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(prepared(packages=[invalid], repositories=[repository]))

    def test_prepared_summary_requires_exact_oci_image_id(self):
        for image_id in (None, "debbuilder-validation:bookworm", "sha256:short"):
            value = prepared()
            value["image"]["id"] = image_id
            with self.subTest(image_id=image_id), self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(value)

    def test_invalid_hash_architecture_and_future_version_are_rejected(self):
        bad_hash = prepared(packages=[{
            "package": "runtime-lib", "version": "1.0-1", "architecture": "amd64",
            "role": "current", "size": 10, "sha256": "not-a-hash", "origin": None,
        }])
        wrong_arch = prepared(packages=[{
            "package": "runtime-lib", "version": "1.0-1", "architecture": "arm64",
            "role": "current", "size": 10, "sha256": "f" * 64, "origin": None,
        }])
        future = prepared()
        future["contract_version"] = PREPARED_DEPENDENCIES_CONTRACT_VERSION + 1
        for value in (bad_hash, wrong_arch, future):
            with self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(value)

        with self.assertRaises(ValidationContractError) as raised:
            normalize_prepared_runtime_dependencies({"contract_version": 2})
        self.assertEqual(raised.exception.code, "future_contract_version")

    def test_duplicate_or_conflicting_phase_package_identity_is_rejected(self):
        first = {
            "package": "runtime-lib", "version": "1.0-1", "architecture": "amd64",
            "role": "current", "size": 10, "sha256": "f" * 64, "origin": None,
        }
        conflict = {**first, "version": "2.0-1", "sha256": "1" * 64}
        with self.assertRaisesRegex(ValidationContractError, "duplicate or conflicting"):
            normalize_prepared_runtime_dependencies(prepared(packages=[first, conflict]))
        transition = [{**first, "role": "previous"}, conflict]
        self.assertEqual(len(normalize_prepared_runtime_dependencies(prepared(packages=transition))["packages"]), 2)

    def test_ephemeral_paths_and_raw_keys_are_not_contract_fields(self):
        for field, value in (
            ("local_path", "/tmp/runtime-lib.deb"),
            ("signing_key", {"armored": "-----BEGIN PGP PUBLIC KEY BLOCK-----"}),
            ("apt_cache", b"not-json"),
        ):
            contract = prepared()
            contract[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(contract)

    def test_diagnostics_are_bounded_and_reject_ephemeral_or_key_material(self):
        invalid_diagnostics = [
            [{"code": "apt_failed", "message": "x" * 1001}],
            [{"code": "apt_failed", "message": "See /tmp/apt.log"}],
            [{"code": "apt_failed", "message": "See path:/tmp/apt.log"}],
            [{"code": "apt_failed", "message": "See file:///tmp/apt.log"}],
            [{"code": "apt_failed", "message": "-----BEGIN PGP PUBLIC KEY BLOCK-----"}],
            [{"code": "INVALID CODE", "message": "APT failed"}],
        ]
        for diagnostics in invalid_diagnostics:
            with self.subTest(diagnostics=diagnostics[0]["code"]), self.assertRaises(ValidationContractError):
                normalize_prepared_runtime_dependencies(prepared(diagnostics=diagnostics))

    def test_image_identity_and_result_reference_reject_local_paths(self):
        value = prepared()
        value["image"]["name"] = "/tmp/image"
        with self.assertRaises(ValidationContractError) as raised:
            normalize_prepared_runtime_dependencies(value)
        self.assertEqual(raised.exception.code, "ephemeral_contract_data")

        value = attempt("success")
        for reference in ("/tmp/result.json", "validation/../result.json", "validation\\result.json"):
            value["result"]["reference"] = reference
            with self.subTest(reference=reference), self.assertRaises(ValidationContractError):
                normalize_validation_attempt(value)

    def test_previous_artifact_must_match_package_and_architecture(self):
        value = prepared()
        value["artifacts"]["previous"] = artifact(package="other", version="0.9-1")
        with self.assertRaises(ValidationContractError):
            normalize_prepared_runtime_dependencies(value)


if __name__ == "__main__":
    unittest.main()
