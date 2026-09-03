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
        status = package_store.lifecycle_display_status
        self.assertEqual(status("success"), "build_success")
        self.assertEqual(status("success", "success"), "ready_to_publish")
        self.assertEqual(status("success", "success", "failed"), "publication_failed")
        self.assertEqual(status("success", "success", "success"), "published")
        self.assertEqual(status("failed"), "build_failed")
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
