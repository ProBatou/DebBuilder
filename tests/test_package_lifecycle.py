import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import apt_repo, deb_inspector, package_store, operations


class AptRepoTests(unittest.TestCase):
    def test_parse_packages_index_keeps_multiple_versions_and_architectures(self):
        text = """Package: app
Version: 1.0
Architecture: amd64
Filename: pool/main/a/app/app_1.0_amd64.deb

Package: app
Version: 0.9
Architecture: amd64
Filename: pool/main/a/app/app_0.9_amd64.deb

Package: tool
Version: 2
Architecture: all
Filename: pool/main/t/tool/tool_2_all.deb
"""
        rows = apt_repo.parse_packages_index(text)
        versions = apt_repo.published_versions(rows, "app")
        self.assertEqual([v["version"] for v in versions], ["1.0", "0.9"])
        self.assertEqual(versions[0]["architecture"], "amd64")

    def test_repository_state_distinguishes_latest_source_from_published(self):
        state = package_store.compute_package_state(
            source_version="4.4.0",
            built_version="4.4.0",
            published_version="4.3.1",
            has_verified_build=True,
        )
        self.assertEqual(state, "publication_available")

    def test_package_state_uses_user_facing_lifecycle_names(self):
        self.assertEqual(package_store.compute_package_state(source_version="2.0", published_version="1.0"), "update_available")
        self.assertEqual(package_store.compute_package_state(source_version="2.0", published_version=""), "build_required")
        self.assertEqual(package_store.compute_package_state(published_version="1.0", last_error="boom"), "failed")
        self.assertEqual(package_store.compute_package_state(published_version="1.0", is_building=True), "building")
        self.assertEqual(package_store.compute_package_state(published_version="1.0"), "unknown")
        self.assertEqual(package_store.compute_package_state(source_version="", built_version="1.0-1", published_version="1.0-1", has_verified_build=True), "unknown")
        self.assertEqual(package_store.compute_package_state(source_version="1.0-1", built_version="1.0-1", published_version="1.0-1", has_verified_build=True), "up_to_date")

    def test_debian_revisions_do_not_look_like_upstream_updates(self):
        for published, upstream in (
            ("1.0.0-1", "1.0.0"),
            ("4.16.1-0", "4.16.1"),
            ("6.0.0-1", "6.0.0"),
            ("3.4.1-2", "3.4.1"),
        ):
            with self.subTest(published=published, upstream=upstream):
                self.assertEqual(
                    package_store.compute_package_state(source_version=upstream, published_version=published),
                    "up_to_date",
                )
        self.assertEqual(
            package_store.compute_package_state(source_version="3.4.2", published_version="3.4.1-2"),
            "update_available",
        )

    def test_reprepro_distribution_parser_detects_codename_and_signing(self):
        text = """Origin: Example\nSuite: stable\nCodename: bookworm\nArchitectures: amd64\nComponents: main\nSignWith: yes\n"""
        parsed = apt_repo.parse_reprepro_distributions(text)
        self.assertEqual(parsed["codename"], "bookworm")
        self.assertEqual(parsed["suite"], "stable")
        self.assertEqual(parsed["architectures"], ["amd64"])
        self.assertEqual(parsed["components"], ["main"])
        self.assertEqual(parsed["sign_with"], "yes")

    def test_detect_repo_backend_prefers_reprepro_when_config_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "conf").mkdir()
            (root / "conf" / "distributions").write_text("Codename: bookworm\n")
            self.assertEqual(apt_repo.detect_repo_backend(root), "reprepro")

    def test_debian_version_comparison_is_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(apt_repo.debian_version_relation("1.10-1", "1.9-1", workspace=Path(td))["relation"], "newer")
            self.assertEqual(apt_repo.debian_version_relation("1.9-1", "1.10-1", workspace=Path(td))["relation"], "older")
            self.assertEqual(apt_repo.debian_version_relation("1.0-1", "1.0-1", workspace=Path(td))["relation"], "equal")

    def test_published_debian_version_is_split_by_debian_grammar(self):
        self.assertEqual(apt_repo.debian_upstream_version("3.4.1-2"), "3.4.1")
        self.assertEqual(apt_repo.debian_upstream_version("1:3.4.1~rc1-2+b1"), "1:3.4.1~rc1")
        self.assertEqual(apt_repo.debian_upstream_version("1.0-rc1-2"), "1.0-rc1")
        self.assertEqual(apt_repo.debian_upstream_version("3.4.1"), "3.4.1")
        with self.assertRaises(ValueError):
            apt_repo.debian_upstream_version("not-a-version")

    def test_upstream_relation_uses_dpkg_after_removing_only_debian_revision(self):
        with tempfile.TemporaryDirectory() as td:
            equal = apt_repo.upstream_version_relation("3.4.1", "3.4.1-2", workspace=Path(td))
            newer = apt_repo.upstream_version_relation("3.4.2", "3.4.1-2", workspace=Path(td))
        self.assertEqual(equal["relation"], "equal")
        self.assertEqual(equal["published_upstream"], "3.4.1")
        self.assertEqual(newer["relation"], "newer")


class DebInspectorTests(unittest.TestCase):
    def test_inspect_deb_uses_dpkg_metadata_and_lists_maintainer_scripts(self):
        deb = Path("/opt/debbuilder-repo-ui/data/package-audit/bashrc_5_all.deb")
        if not deb.exists():
            self.skipTest("local audit deb unavailable")
        info = deb_inspector.inspect_deb(deb)
        self.assertTrue(info["ok"])
        self.assertEqual(info["package"], "bashrc")
        self.assertEqual(info["version"], "5")
        self.assertEqual(info["architecture"], "all")
        self.assertIn("preinst", info["maintainer_scripts"])
        self.assertIn("prerm", info["maintainer_scripts"])
        self.assertGreater(info["size"], 0)
        self.assertTrue(any(f["path"].endswith("/root/.bashrc") for f in info["files"]))


class PackageStoreTests(unittest.TestCase):
    def test_lifecycle_display_statuses(self):
        status = package_store.derive_lifecycle_status
        self.assertEqual(status("success"), "validation_needed")
        self.assertEqual(status("success", "running"), "validating")
        self.assertEqual(status("success", "failed"), "validation_failed")
        self.assertEqual(status("success", "success"), "ready_to_publish")
        self.assertEqual(status("success", "success", "running"), "publishing")
        self.assertEqual(status("success", "success", "failed"), "publication_failed")
        self.assertEqual(status("success", "success", "success"), "published")
        self.assertEqual(status("failed"), "build_failed")

    def test_run_summary_never_borrows_lifecycle_events_from_an_older_run(self):
        def summary(run):
            validation = (run.get("validations") or [{}])[-1].get("status", "not_run")
            publication = (run.get("publications") or [{}])[-1].get("status", "not_run")
            return {
                "id": run["id"], "status": run["status"], "updated": run.get("updated", 0),
                "lifecycle_status": package_store.derive_lifecycle_status(run["status"], validation, publication),
            }

        runs = [
            {"id": "new", "mode": "build", "status": "success", "artifact": {"path": "new.deb"}, "validations": [], "publications": []},
            {"id": "old", "mode": "build", "status": "success", "artifact": {"path": "old.deb"}, "validations": [{"status": "success"}], "publications": [{"status": "success"}]},
        ]
        state = package_store.summarize_runs(runs, summary)
        self.assertEqual(state["last_real"]["lifecycle_status"], "validation_needed")
        self.assertEqual(state["successful"]["id"], "new")
        self.assertIsNone(state["latest_validation"])
        self.assertIsNone(state["latest_publication"])

    def test_failed_dry_run_does_not_replace_latest_real_run(self):
        summary = lambda run: {"id": run["id"], "status": run["status"]}
        state = package_store.summarize_runs([
            {"id": "dry", "mode": "dry_run", "status": "failed"},
            {"id": "real", "mode": "build", "status": "success", "artifact": {"path": "app.deb"}},
        ], summary)
        self.assertEqual(state["last_real"]["id"], "real")
        self.assertEqual(state["last_dry_run"]["id"], "dry")

    def test_enrich_package_preserves_existing_fields_and_adds_lifecycle_sections(self):
        pkg = {"name": "code-server", "apt_version": "4.133.0", "architecture": "amd64", "recipe": "code-server-recipe", "source": {"type": "github", "repository": "coder/code-server"}}
        enriched = package_store.enrich_package(pkg, published_version="4.134.0", source_version="4.134.0")
        self.assertEqual(enriched["name"], "code-server")
        self.assertEqual(enriched["version"]["published"], "4.134.0")
        self.assertEqual(enriched["version"]["source"], "4.134.0")
        self.assertEqual(enriched["source"]["repository"], "coder/code-server")
        self.assertIn(enriched["lifecycle_state"], {"up_to_date", "publication_available", "update_available"})
        self.assertIn("build", enriched)
        self.assertIn("repository", enriched)


class OperationsTests(unittest.TestCase):
    def test_publish_requires_explicit_confirmation_even_when_deb_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "app_1.0_all.deb"
            fake.write_bytes(b"not a real deb")
            with self.assertRaises(PermissionError):
                operations.publish_deb_operation(fake, repo_root=Path(tmp), package_name="app", version="1.0", dry_run=False, confirm="")

    def test_publish_dry_run_does_not_modify_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "app_1.0_all.deb"
            fake.write_bytes(b"not a real deb")
            result = operations.publish_deb_operation(fake, repo_root=Path(tmp), package_name="app", version="1.0", dry_run=True)
            self.assertEqual(result["status"], "dry_run")
            self.assertFalse((Path(tmp) / "pool").exists())


if __name__ == "__main__":
    unittest.main()
