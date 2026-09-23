from debbuilder import execution_projection
"""APT repository parsing and canonical Build Run-derived package state tests."""

import os
import gzip
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import apt_repo, build_pipeline, deb_inspector, package_store
from debbuilder.package_service import PackageService


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

    def test_parse_packages_index_accepts_space_and_tab_continuations(self):
        rows = apt_repo.parse_packages_index(
            "Package: app\nVersion: 1.0\nArchitecture: amd64\n"
            "Description: first line\n second line\n\tthird: line\n\n",
        )
        self.assertEqual(rows[0]["Description"], "first line\nsecond line\nthird: line")

    def test_local_packages_projection_reads_stable_bounded_canonical_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "dists" / "stable" / "main" / "binary-amd64"
            binary.mkdir(parents=True)
            (binary / "Packages.gz").write_bytes(gzip.compress(
                b"Package: demo\nVersion: 1.2.3-1\nArchitecture: amd64\n\n",
            ))
            rows = apt_repo.local_packages_index(root, "stable", "main", "amd64")
        self.assertEqual(rows[0]["Package"], "demo")
        self.assertEqual(rows[0]["Version"], "1.2.3-1")

    def test_local_packages_projection_fails_soft_for_missing_malformed_and_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "dists" / "stable" / "main" / "binary-amd64"
            binary.mkdir(parents=True)
            self.assertIsNone(apt_repo.local_packages_index(root, "stable", "main", "amd64"))
            (binary / "Packages.gz").write_bytes(b"not-gzip")
            self.assertIsNone(apt_repo.local_packages_index(root, "stable", "main", "amd64"))
            (binary / "Packages.gz").write_bytes(gzip.compress(b"x" * 100))
            with mock.patch.object(apt_repo, "MAX_PACKAGES_INDEX_BYTES", 20):
                self.assertIsNone(apt_repo.local_packages_index(root, "stable", "main", "amd64"))

    def test_local_packages_projection_rejects_malformed_non_field_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "dists" / "stable" / "main" / "binary-amd64"
            binary.mkdir(parents=True)
            (binary / "Packages.gz").write_bytes(gzip.compress(
                b"Package: demo\nBROKEN-LINE\nVersion: 1.2.3-1\nArchitecture: amd64\n\n",
            ))
            self.assertIsNone(apt_repo.local_packages_index(root, "stable", "main", "amd64"))

    def test_opened_index_disappearance_is_a_transition_not_initial_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "dists" / "stable" / "main" / "binary-amd64"
            binary.mkdir(parents=True)
            (binary / "Packages.gz").write_bytes(gzip.compress(
                b"Package: demo\nVersion: 1.2.3-1\nArchitecture: amd64\n\n",
            ))
            with apt_repo.pinned_directory(root) as (_root, root_fd), mock.patch.object(
                apt_repo.os, "stat", side_effect=FileNotFoundError,
            ):
                result = apt_repo._read_pinned_index_file(
                    root_fd, "dists/stable/main/binary-amd64/Packages.gz", compressed=True,
                )
            self.assertIsNone(result)

    def test_local_packages_projection_rejects_symlinked_root_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real"
            binary = real / "dists" / "stable" / "main" / "binary-amd64"
            binary.mkdir(parents=True)
            outside = base / "outside"
            outside.write_bytes(gzip.compress(b"Package: escaped\nVersion: 1\n\n"))
            (binary / "Packages.gz").symlink_to(outside)
            self.assertIsNone(apt_repo.local_packages_index(real, "stable", "main", "amd64"))
            linked_root = base / "linked-root"
            linked_root.symlink_to(real, target_is_directory=True)
            self.assertIsNone(apt_repo.local_packages_index(linked_root, "stable", "main", "amd64"))

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
        parsed = apt_repo.select_reprepro_distribution(text, "bookworm")
        self.assertEqual(parsed["codename"], "bookworm")
        self.assertEqual(parsed["suite"], "stable")
        self.assertEqual(parsed["architectures"], ["amd64"])
        self.assertEqual(parsed["components"], ["main"])
        self.assertEqual(parsed["sign_with"], "yes")

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
    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_inspect_deb_uses_dpkg_metadata_and_lists_maintainer_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            control = staging / "DEBIAN"
            payload = staging / "usr/share/deb-inspector-fixture/payload.txt"
            control.mkdir(parents=True)
            payload.parent.mkdir(parents=True)
            for directory in (staging, control, staging / "usr", staging / "usr/share", payload.parent):
                directory.chmod(0o755)
            (control / "control").write_text(
                "Package: deb-inspector-fixture\n"
                "Version: 1.2.3-1\n"
                "Architecture: all\n"
                "Maintainer: DebBuilder tests <tests@example.invalid>\n"
                "Description: deterministic deb inspector fixture\n",
                encoding="utf-8",
            )
            (control / "control").chmod(0o644)
            for script_name in ("preinst", "prerm"):
                script = control / script_name
                script.write_text("#!/bin/sh\nset -e\n", encoding="utf-8")
                script.chmod(0o755)
            payload.write_text("fixture payload\n", encoding="utf-8")
            payload.chmod(0o644)
            deb = root / "deb-inspector-fixture_1.2.3-1_all.deb"
            environment = os.environ.copy()
            environment.update({"LC_ALL": "C", "SOURCE_DATE_EPOCH": "946684800"})
            subprocess.run(
                ["dpkg-deb", "--build", "--root-owner-group", str(staging), str(deb)],
                check=True,
                capture_output=True,
                env=environment,
            )

            info = deb_inspector.inspect_deb(deb, workspace=root)

        self.assertTrue(info["ok"])
        self.assertEqual(info["package"], "deb-inspector-fixture")
        self.assertEqual(info["version"], "1.2.3-1")
        self.assertEqual(info["architecture"], "all")
        self.assertIn("preinst", info["maintainer_scripts"])
        self.assertIn("prerm", info["maintainer_scripts"])
        self.assertGreater(info["size"], 0)
        self.assertTrue(any(
            row["path"].endswith("/usr/share/deb-inspector-fixture/payload.txt")
            for row in info["files"]
        ))


class PackageStoreTests(unittest.TestCase):
    def test_repo_current_prefix_is_not_reserved_from_recipe_projection(self):
        recipe = {"active": True, "package": {"name": "demo"}}
        service = PackageService(
            data_dir=Path("/tmp/not-used"), workspace_root=Path("/tmp/not-used"),
            list_workflows=lambda: [{"id": "repo-current-demo"}],
            workflow_path=lambda _recipe_id: Path("/tmp/not-used/recipe.json"),
            read_workflow=lambda _path: recipe,
            repo_settings=lambda: {"architecture": "amd64"},
        )

        records = service.recipe_records_by_package()

        self.assertEqual(records["demo"]["id"], "repo-current-demo")

    def test_noncanonical_run_validation_projection_is_ignored(self):
        run = {
            "id": "noncanonical-row", "recipe_id": "demo", "mode": "build",
            "status": "success", "artifact": {"path": "demo.deb"},
            "validations": [{"id": "untrusted-validation", "status": "success"}],
            "publications": [],
            "publication_insertion_eligibility": {
                "eligible": False,
                "reasons": ["current_validation_required"],
                "validation_id": "",
            },
        }

        summary = execution_projection.public_summary(run)

        self.assertEqual(summary["validation_status"], "not_run")
        self.assertEqual(summary["lifecycle_status"], "validation_needed")
        self.assertTrue(summary["allowed_actions"]["validate"])
        self.assertFalse(summary["allowed_actions"]["publish"])

    def test_allowed_actions_come_only_from_latest_build_run_facts(self):
        run = {
            "mode": "build", "status": "success", "artifact": {"path": "app.deb"},
            "_validation_attempts": [{"status": "success"}], "publications": [],
            "publication_insertion_eligibility": {"eligible": True, "reasons": [], "validation_id": "canonical"},
        }
        actions = package_store.allowed_actions("update_available", "app-recipe", run)
        self.assertEqual(actions, {"test": True, "build": True, "validate": True, "publish": True})
        failed = package_store.allowed_actions("build_failed", "app-recipe", {"mode": "build", "status": "failed"})
        self.assertEqual(failed, {"test": True, "build": True, "validate": False, "publish": False})

    def test_pruned_local_artifact_disables_validate_and_publish_actions(self):
        run = {
            "mode": "build", "status": "success",
            "artifact": {"path": "/data/builds/run/artifacts/demo.deb", "pruning": {"status": "pruned"}},
            "validations": [{"status": "success"}], "publications": [],
        }

        actions = package_store.allowed_actions("publication_available", "demo", run)

        self.assertFalse(actions["validate"])
        self.assertFalse(actions["publish"])

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
        self.assertEqual(status("queued"), "queued")
        self.assertEqual(status("cancelling"), "cancelling")

    def test_run_summary_never_borrows_lifecycle_events_from_an_older_run(self):
        def summary(run):
            validation = (run.get("_validation_attempts") or [{}])[-1].get("status", "not_run")
            publication = (run.get("publications") or [{}])[-1].get("status", "not_run")
            return {
                "id": run["id"], "status": run["status"], "updated": run.get("updated", 0),
                "lifecycle_status": package_store.derive_lifecycle_status(run["status"], validation, publication),
            }

        runs = [
            {"id": "new", "mode": "build", "status": "success", "artifact": {"path": "new.deb"}, "_validation_attempts": [], "publications": []},
            {"id": "old", "mode": "build", "status": "success", "artifact": {"path": "old.deb"}, "_validation_attempts": [{"status": "success"}], "publications": [{"status": "success"}]},
        ]
        state = package_store.summarize_runs(runs, summary)
        self.assertEqual(state["last_real"]["lifecycle_status"], "validation_needed")
        self.assertEqual(state["successful"]["id"], "new")
        self.assertIsNone(state["latest_validation"])
        self.assertIsNone(state["latest_publication"])

    def test_publication_history_derives_version_and_fails_closed_without_proof(self):
        run = {
            "id": "proofless", "mode": "build", "status": "success", "artifact": {"path": "demo.deb"},
            "publications": [{
                "id": "publication-one", "status": "success", "version": "2.0-1", "proof": None,
                "finished_at": "2026-09-21T10:00:00+00:00",
            }],
        }

        state = package_store.summarize_runs(
            [run], lambda value: {"id": value["id"], "status": value["status"], "updated": 0},
        )
        publication = next(row for row in state["history"] if row["action"] == "publication")

        self.assertEqual(publication["status"], "failed")
        self.assertEqual(publication["version"], "2.0-1")

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
        self.assertNotIn("url", enriched["source"])
        self.assertIn(enriched["lifecycle_state"], {"up_to_date", "publication_available", "update_available"})
        self.assertIn("build", enriched)
        self.assertIn("repository", enriched)

    def test_enrich_package_preserves_safe_release_asset_payload_identity(self):
        source = {
            "type": "github_release_asset", "repository": "owner/demo",
            "release_id": 123, "asset_id": 456, "asset_name": "demo-linux-amd64",
            "asset_url": "https://github.com/owner/demo/releases/download/v1/demo-linux-amd64",
            "asset_api_url": "https://api.github.com/repos/owner/demo/releases/assets/456",
            "content_type": "application/octet-stream", "declared_size": 42, "download_size": 42,
            "payload_kind": "raw_file", "file_count": 1, "archive_format": "", "sha256": "a" * 64,
            "expected_sha256": "a" * 64, "checksum_verified": True,
        }
        enriched = package_store.enrich_package({"name": "demo", "source": source})
        for field, value in source.items():
            if field in {"asset_url", "asset_api_url"}:
                self.assertNotIn(field, enriched["source"])
                continue
            self.assertEqual(enriched["source"][field], value)

    def test_package_service_projects_resolved_raw_release_asset_identity(self):
        service = PackageService(
            data_dir=Path("/tmp/not-used"), workspace_root=Path("/tmp/not-used"),
            list_workflows=lambda: [], workflow_path=lambda _recipe_id: None,
            read_workflow=lambda _path: {}, repo_settings=lambda: {"architecture": "amd64"},
            observation_lookup=lambda _recipe_id, _recipe: None,
        )
        run = {
            "id": "run-1", "recipe_id": "demo", "mode": "dry_run", "status": "prepared",
            "version": {"upstream": "1.2.3", "debian": "1.2.3-1"},
            "steps": [{"name": "source", "details": {
                "repository": "owner/demo", "ref": "v1.2.3", "tag": "v1.2.3",
                "release_id": 123, "release_url": "https://github.com/owner/demo/releases/tag/v1.2.3",
                "payload_kind": "raw_file", "file_count": 1,
                "asset": {
                    "source": "release_asset", "asset_id": 456, "name": "demo-linux-amd64",
                    "url": "https://github.com/owner/demo/releases/download/v1.2.3/demo-linux-amd64",
                    "api_url": "https://api.github.com/repos/owner/demo/releases/assets/456",
                    "content_type": "application/octet-stream", "declared_size": 42, "download_size": 42,
                    "payload_kind": "raw_file", "file_count": 1, "sha256": "a" * 64,
                    "expected_sha256": "a" * 64, "checksum_verified": True,
                },
            }}],
        }
        projected = service._enrich_package(
            {"name": "demo", "source": {}}, {}, [run],
            {"repository": "https://apt.example.test", "distribution": "stable", "component": "main"}, False,
        )
        expected = {
            "type": "github_release_asset", "repository": "owner/demo",
            "release": "v1.2.3", "tag": "v1.2.3", "release_id": 123,
            "asset_id": 456, "asset_name": "demo-linux-amd64",
            "content_type": "application/octet-stream", "declared_size": 42, "download_size": 42,
            "payload_kind": "raw_file", "file_count": 1, "archive_format": "", "sha256": "a" * 64,
            "expected_sha256": "a" * 64, "checksum_verified": True,
        }
        for field, value in expected.items():
            self.assertEqual(projected["source"][field], value)
        for field in ("path", "url", "release_url", "asset_url", "asset_api_url"):
            self.assertNotIn(field, projected["source"])

    def test_package_service_projects_generated_archive_payload(self):
        service = PackageService(
            data_dir=Path("/tmp/not-used"), workspace_root=Path("/tmp/not-used"),
            list_workflows=lambda: [], workflow_path=lambda _recipe_id: None,
            read_workflow=lambda _path: {}, repo_settings=lambda: {"architecture": "amd64"},
            observation_lookup=lambda _recipe_id, _recipe: None,
        )
        run = {
            "id": "run-1", "recipe_id": "demo", "mode": "dry_run", "status": "prepared",
            "version": {"upstream": "1.2.3", "debian": "1.2.3-1"},
            "steps": [{"name": "source", "details": {
                "repository": "owner/demo", "ref": "v1.2.3", "tag": "v1.2.3",
                "payload_kind": "archive", "file_count": 4, "extraction": {"files": 4},
                "asset": {"source": "github_source", "name": "source-v1.2.3.tar.gz", "archive_format": "tar.gz"},
            }}],
        }
        projected = service._enrich_package(
            {"name": "demo", "source": {}}, {}, [run],
            {"repository": "https://apt.example.test", "distribution": "stable", "component": "main"}, False,
        )
        self.assertEqual(projected["source"]["type"], "github")
        self.assertEqual(projected["source"]["payload_kind"], "archive")
        self.assertEqual(projected["source"]["file_count"], 4)
        self.assertEqual(projected["source"]["extracted_file_count"], 4)
        self.assertEqual(projected["source"]["archive_format"], "tar.gz")




if __name__ == "__main__":
    unittest.main()
