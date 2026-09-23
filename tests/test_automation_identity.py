import copy
import json
import unittest

from debbuilder.automation_identity import (
    UpstreamIdentityError,
    automation_attempt_key,
    canonical_identity_bytes,
    normalize_upstream_identity,
)


RECIPE_SHA = "ab" * 32


def release_asset(**changes):
    value = {
        "schema_version": 1,
        "provider": "github",
        "repository": "Example/Project",
        "tracking": "latest_release",
        "source_type": "release_asset",
        "payload_kind": "deb",
        "requested_ref": "",
        "release_id": "1234",
        "asset_id": "5678",
        "commit_sha": "",
        "ref_object_sha": "",
        "resolved_ref": "v1.2.3",
        "resolved_version": "1.2.3",
        "content_sha256": "12" * 32,
        "asset_name": "project_1.2.3_amd64.deb",
        "expected_size": 42,
        "expected_package": "project",
        "expected_architecture": "amd64",
    }
    value.update(changes)
    return value


class AutomationIdentityTests(unittest.TestCase):
    def test_release_asset_identity_is_exact_and_canonical(self):
        normalized = normalize_upstream_identity(release_asset())
        self.assertEqual(normalized["repository"], "example/project")
        self.assertEqual(normalized["completeness"], "complete")
        self.assertEqual(normalized["missing_fields"], [])
        self.assertEqual(normalized["release_id"], "1234")
        self.assertEqual(normalized["asset_id"], "5678")

    def test_generated_source_shape_does_not_fake_completeness(self):
        source = release_asset(
            source_type="github_source_archive", payload_kind="source_archive",
            asset_id="", asset_name="", expected_size=None, expected_package="",
            expected_architecture="", content_sha256="", commit_sha="", source_archive_format="tar.gz",
        )
        normalized = normalize_upstream_identity(source)
        self.assertEqual(normalized["completeness"], "partial")
        self.assertEqual(normalized["missing_fields"], ["commit_sha", "ref_object_sha"])
        with self.assertRaises(UpstreamIdentityError) as raised:
            automation_attempt_key("recipe", source, RECIPE_SHA)
        self.assertEqual(raised.exception.code, "incomplete_upstream_identity")

    def test_mutable_ref_requires_resolved_commit(self):
        source = release_asset(
            tracking="manual", source_type="git_ref", payload_kind="source_archive",
            requested_ref="main", release_id="", asset_id="", commit_sha="",
            asset_name="", expected_size=None, expected_package="", expected_architecture="",
            content_sha256="", source_archive_format="tar.gz", resolved_ref="refs/heads/main",
        )
        self.assertEqual(normalize_upstream_identity(source)["missing_fields"], ["commit_sha"])
        source["commit_sha"] = "cd" * 20
        self.assertEqual(normalize_upstream_identity(source)["completeness"], "complete")

    def test_exact_requested_and_resolved_refs_survive(self):
        identity = normalize_upstream_identity(release_asset(
            tracking="tag", source_type="git_ref", payload_kind="source_archive",
            requested_ref="v1.2.3", release_id="", asset_id="", commit_sha="cd" * 20,
            asset_name="", expected_size=None, expected_package="", expected_architecture="",
            content_sha256="", source_archive_format="tar.gz", resolved_ref="refs/tags/v1.2.3",
        ))
        self.assertEqual(identity["requested_ref"], "v1.2.3")
        self.assertEqual(identity["resolved_ref"], "refs/tags/v1.2.3")

    def test_raw_and_archive_asset_payload_kinds_are_distinct(self):
        non_deb = {"expected_package": "", "expected_architecture": ""}
        raw = normalize_upstream_identity(release_asset(payload_kind="raw_file", **non_deb))
        archive = normalize_upstream_identity(release_asset(payload_kind="archive", **non_deb))
        self.assertNotEqual(canonical_identity_bytes(raw), canonical_identity_bytes(archive))

    def test_serialization_and_attempt_keys_are_stable_across_input_order(self):
        first = release_asset()
        second = dict(reversed(list(first.items())))
        self.assertEqual(canonical_identity_bytes(first), canonical_identity_bytes(second))
        self.assertEqual(
            automation_attempt_key("recipe", first, RECIPE_SHA),
            automation_attempt_key("recipe", second, RECIPE_SHA.upper()),
        )
        self.assertEqual(
            automation_attempt_key("recipe", first, RECIPE_SHA),
            "automation-v1-4e4151e3b6181e800ae1099277be33a6c971b46a1903b90150526ba2a3ce6261",
        )
        self.assertEqual(json.loads(canonical_identity_bytes(first))["schema_version"], 1)

    def test_attempt_key_binds_recipe_revision_and_exact_identity(self):
        key = automation_attempt_key("recipe", release_asset(), RECIPE_SHA)
        self.assertNotEqual(key, automation_attempt_key("recipe", release_asset(asset_id="999"), RECIPE_SHA))
        self.assertNotEqual(key, automation_attempt_key("recipe", release_asset(), "cd" * 32))
        self.assertNotEqual(key, automation_attempt_key("other", release_asset(), RECIPE_SHA))

    def test_package_version_alone_cannot_complete_identity(self):
        incomplete = release_asset(release_id="", asset_id="", resolved_ref="", resolved_version="9.9.9")
        self.assertEqual(normalize_upstream_identity(incomplete)["completeness"], "partial")

    def test_declared_completeness_cannot_override_derived_result(self):
        with self.assertRaises(UpstreamIdentityError) as raised:
            normalize_upstream_identity(release_asset(asset_id="", completeness="complete"))
        self.assertEqual(raised.exception.code, "upstream_identity_completeness_mismatch")

    def test_credentials_urls_paths_and_unknown_fields_are_rejected(self):
        for field in ("token", "authenticated_url", "download_url", "local_path", "signing_key", "lock_name"):
            with self.subTest(field=field), self.assertRaises(UpstreamIdentityError) as raised:
                normalize_upstream_identity({**release_asset(), field: "/tmp/secret"})
            self.assertEqual(raised.exception.code, "unsafe_upstream_identity_field")
        for field, value in (("requested_ref", "https://token@example.test/ref"), ("resolved_ref", "/tmp/source"), ("resolved_version", "/tmp/version")):
            with self.subTest(field=field), self.assertRaises(UpstreamIdentityError):
                normalize_upstream_identity(release_asset(**{field: value}))
        for value in ("git@github.com:owner/repository", "token=ghp_secret"):
            with self.subTest(value=value), self.assertRaises(UpstreamIdentityError):
                normalize_upstream_identity(release_asset(resolved_ref=value))

    def test_tracking_and_source_type_must_describe_one_coherent_identity(self):
        for changes in (
            {"tracking": "latest_release", "source_type": "git_ref", "payload_kind": "source_archive"},
            {"tracking": "tag", "source_type": "release_asset"},
            {"tracking": "manual", "source_type": "github_source_archive", "payload_kind": "source_archive"},
        ):
            with self.subTest(changes=changes), self.assertRaises(UpstreamIdentityError) as raised:
                normalize_upstream_identity(release_asset(**changes))
            self.assertEqual(raised.exception.code, "invalid_upstream_identity")

    def test_irrelevant_optional_fields_cannot_alias_one_source_identity(self):
        for changes in (
            {"requested_ref": "v1.2.3"}, {"commit_sha": "cd" * 20},
            {"source_archive_format": "tar.gz"}, {"source_tree_sha": "cd" * 20},
        ):
            with self.subTest(changes=changes), self.assertRaises(UpstreamIdentityError):
                normalize_upstream_identity(release_asset(**changes))
        source = release_asset(
            source_type="github_source_archive", payload_kind="source_archive",
            asset_id="", asset_name="", expected_size=None, expected_package="",
            expected_architecture="", content_sha256="", commit_sha="cd" * 20,
            source_archive_format="tar.gz",
        )
        for changes in ({"asset_id": "999"}, {"requested_ref": "v1.2.3"}):
            with self.subTest(changes=changes), self.assertRaises(UpstreamIdentityError):
                normalize_upstream_identity({**source, **changes})

    def test_normalization_does_not_mutate_input(self):
        identity = release_asset()
        original = copy.deepcopy(identity)
        normalize_upstream_identity(identity)
        self.assertEqual(identity, original)


if __name__ == "__main__":
    unittest.main()
