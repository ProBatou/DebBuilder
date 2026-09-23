import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from debbuilder import build_pipeline, debian_packaging, upstream_archive, upstream_artifact
from debbuilder.automation_identity import release_asset_identity
from debbuilder.build_store import BuildStore
from debbuilder.execution_manager import ExecutionManager
from debbuilder.recipe_schema import normalize_recipe, runtime_recipe_for_storage, validate_recipe_metadata


def recipe(**artifact):
    artifact_config = {
        "mode": "upstream_archive", "type": "archive", "architecture": "amd64",
        "asset_name": "demo-linux.tar.gz",
        "archive_source": "release_asset", "asset_selection": "exact",
        "payload": {"mode": "paths", "include": ["demo"], "exclude": []},
    }
    artifact_config.update(artifact)
    if artifact_config.get("name_pattern") and not artifact_config.get("asset_name"):
        artifact_config["asset_selection"] = "pattern"
    return validate_recipe_metadata({
        "schema_version": 5,
        "name": "demo", "package": {"name": "demo", "architecture": "amd64", "maintainer": "Demo <demo@example.org>"},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
        "artifact": artifact_config,
        "build": {"commands": [], "source_changes": [], "output": {"mode": "source"}},
        "install": {"content": {"source": "configured_files"}, "owner": {"user": "root", "group": "root"}, "config_files": [{"source": "demo", "destination": "/usr/bin/demo", "policy": "replace", "mode": "0755", "owner": "root", "group": "root"}]},
    })


def release(assets):
    return {"repository": "example/demo", "release_id": 123, "tag": "v1.2.3", "ref": "v1.2.3", "name": "v1.2.3", "url": "https://github.com/example/demo/releases/tag/v1.2.3", "upstream_version": "1.2.3", "archive_url": "https://api.github.com/repos/example/demo/tarball/v1.2.3", "tarball_url": "https://api.github.com/repos/example/demo/tarball/v1.2.3", "zipball_url": "https://api.github.com/repos/example/demo/zipball/v1.2.3", "assets": assets}


def admitted_asset_identity(configured, *, name="demo-linux.tar.gz", payload_kind="archive", size=0):
    asset = {
        "asset_id": 456, "name": name, "size": size, "digest": "",
        "url": f"https://github.com/example/demo/releases/download/v1.2.3/{name}",
    }
    return release_asset_identity(configured, release([asset]), asset, payload_kind)


def tar_bytes(entries, mode="w:gz"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as bundle:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(payload))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload.decode()
                bundle.addfile(info)
    return output.getvalue()


class UpstreamArchiveTests(unittest.TestCase):
    def make_payload_tree(self, root):
        source = Path(root) / "source"
        for relative, content in {
            "app/main.py": "main\n", "app/lib/util.py": "util\n",
            "static/index.html": "index\n", "static/dev/debug.js": "debug\n",
            "foo/a.txt": "foo\n", "foobar/b.txt": "foobar\n",
            "server.py": "server\n", "tests/test_app.py": "test\n", ".github/workflow.yml": "ci\n",
        }.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return source

    def resolved_paths(self, source, payload):
        configured = recipe(payload=payload)
        return [row["relative_path"] for row in upstream_archive.resolve_payload(configured, source)["files"]]

    def test_payload_resolution_supports_files_directories_entire_archive_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_payload_tree(temporary)
            cases = [
                ({"mode": "paths", "include": ["app/main.py"], "exclude": []}, ["app/main.py"]),
                ({"mode": "paths", "include": ["app/"], "exclude": []}, ["app/lib/util.py", "app/main.py"]),
                ({"mode": "paths", "include": ["app/", "static/"], "exclude": []}, ["app/lib/util.py", "app/main.py", "static/dev/debug.js", "static/index.html"]),
                ({"mode": "paths", "include": ["server.py", "static/"], "exclude": []}, ["server.py", "static/dev/debug.js", "static/index.html"]),
                ({"mode": "entire_archive", "include": [], "exclude": []}, [".github/workflow.yml", "app/lib/util.py", "app/main.py", "foo/a.txt", "foobar/b.txt", "server.py", "static/dev/debug.js", "static/index.html", "tests/test_app.py"]),
                ({"mode": "entire_archive", "include": [], "exclude": ["server.py"]}, [".github/workflow.yml", "app/lib/util.py", "app/main.py", "foo/a.txt", "foobar/b.txt", "static/dev/debug.js", "static/index.html", "tests/test_app.py"]),
                ({"mode": "paths", "include": ["static/"], "exclude": ["static/dev/"]}, ["static/index.html"]),
                ({"mode": "entire_archive", "include": [], "exclude": [".github/", "tests/"]}, ["app/lib/util.py", "app/main.py", "foo/a.txt", "foobar/b.txt", "server.py", "static/dev/debug.js", "static/index.html"]),
                ({"mode": "paths", "include": ["foo/", "foobar/"], "exclude": ["foo/"]}, ["foobar/b.txt"]),
            ]
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    self.assertEqual(self.resolved_paths(source, payload), expected)

    def test_payload_resolution_is_deterministic_and_reports_compact_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_payload_tree(temporary)
            configured = recipe(payload={
                "mode": "paths", "include": ["static/", "server.py", "app/"],
                "exclude": ["static/dev/"],
            })
            plan = upstream_archive.resolve_payload(configured, source)
            self.assertEqual([row["relative_path"] for row in plan["files"]], ["app/lib/util.py", "app/main.py", "server.py", "static/index.html"])
            self.assertEqual(upstream_archive.payload_plan_summary(plan), {
                "mode": "paths", "include": ["app/", "server.py", "static/"], "exclude": ["static/dev/"],
                "explicit_files": 1, "selected_directories": 2, "selected_files": 4,
                "excluded_files": 0, "excluded_directories": 1, "excluded_resolved_files": 1,
            })

    def test_payload_selectors_are_validated_against_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_payload_tree(temporary)
            cases = [
                ({"mode": "paths", "include": ["missing.py"], "exclude": []}, "archive_selection_path_not_found", "include", "file"),
                ({"mode": "paths", "include": ["server.py"], "exclude": ["missing.py"]}, "archive_selection_path_not_found", "exclude", "file"),
                ({"mode": "paths", "include": ["app"], "exclude": []}, "archive_selection_type_mismatch", "include", "file"),
                ({"mode": "paths", "include": ["server.py"], "exclude": ["server.py/"]}, "archive_selection_type_mismatch", "exclude", "directory"),
            ]
            for payload, code, role, expected_kind in cases:
                with self.subTest(payload=payload), self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                    upstream_archive.resolve_payload(recipe(payload=payload), source)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details["role"], role)
                self.assertEqual(caught.exception.details["expected_kind"], expected_kind)

    def test_payload_exclusions_cannot_resolve_to_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_payload_tree(temporary)
            configured = recipe(payload={"mode": "paths", "include": ["app/"], "exclude": ["app/"]})
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.resolve_payload(configured, source)
            self.assertEqual(caught.exception.code, "archive_selection_empty")
            self.assertEqual(caught.exception.details["mode"], "paths")

    def test_payload_resolution_rejects_later_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self.make_payload_tree(temporary)
            (source / "link.py").symlink_to("server.py")
            configured = recipe(payload={"mode": "entire_archive", "include": [], "exclude": []})
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.resolve_payload(configured, source)
            self.assertEqual(caught.exception.code, "archive_selection_unsafe_path")

    def test_root_stripping_keeps_selectors_and_staged_paths_in_logical_namespace(self):
        archive = tar_bytes([
            ("Project-v1.0/app/main.py", b"main\n", "file"),
            ("Project-v1.0/static/index.html", b"index\n", "file"),
        ])
        configured = recipe(
            asset_name="", archive_source="github_source", archive_format="tar.gz",
            payload={"mode": "paths", "include": ["app/", "static/"], "exclude": []},
        )
        configured["install"].update({
            "destination": "/opt/demo", "content": {"source": "build_output", "path": ""},
            "config_files": [],
        })
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(archive)
            return {"path": str(destination), "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            acquired = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([]), downloader=downloader,
            )
            staged = debian_packaging.prepare_staging(
                configured, {"output": {"mode": "archive_payload", "payload": acquired["archive_payload"]}, "version": "1.2.3-1"}, temporary,
            )
            destination = Path(temporary) / "staging/opt/demo"
            self.assertEqual(acquired["archive_payload"]["include"], ["app/", "static/"])
            self.assertEqual(staged["content_files"], ["app/main.py", "static/index.html"])
            self.assertTrue((destination / "app/main.py").is_file())
            self.assertTrue((destination / "static/index.html").is_file())
            self.assertFalse((destination / "Project-v1.0").exists())

    def test_latest_release_artifact_error_is_converted_with_details(self):
        details = {"status": 403, "repository": "example/demo"}
        error = upstream_artifact.UpstreamArtifactError(
            "github_api_error", "GitHub API request failed with HTTP 403", details=details,
        )
        with mock.patch.object(upstream_archive.upstream_artifact, "resolve_release", side_effect=error):
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.resolve_release(recipe())
        self.assertEqual(caught.exception.code, error.code)
        self.assertEqual(str(caught.exception), str(error))
        self.assertEqual(caught.exception.details, details)
        self.assertIs(caught.exception.__cause__, error)

    def test_pipeline_records_latest_release_failure_as_source_error(self):
        details = {"status": 403, "repository": "example/demo"}
        error = upstream_artifact.UpstreamArtifactError(
            "github_api_error", "GitHub API request failed with HTTP 403", details=details,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            configured = runtime_recipe_for_storage(recipe())
            with mock.patch.object(upstream_archive.upstream_artifact, "resolve_release", side_effect=error):
                result = build_pipeline.execute_pipeline_run(
                    build_pipeline.create_pipeline_run(
                        configured,
                        store=store,
                        dry_run=True,
                        manual_source_provenance=admitted_asset_identity(configured),
                    )["id"],
                    store=store, acquire=upstream_archive.acquire,
                )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], {
            "stage": "source", "code": "github_api_error",
            "message": "GitHub API request failed with HTTP 403", "details": details,
        })
        self.assertNotEqual(result["error"]["code"], "execution_worker_error")

    def test_execution_manager_preserves_latest_release_failure(self):
        details = {"status": 403}
        error = upstream_artifact.UpstreamArtifactError(
            "github_api_error", "GitHub API request failed with HTTP 403", details=details,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            configured = runtime_recipe_for_storage(recipe())
            run = build_pipeline.create_pipeline_run(
                configured, store=store, dry_run=True,
                manual_source_provenance=admitted_asset_identity(configured),
            )
            manager = ExecutionManager(store)
            with mock.patch.object(upstream_archive.upstream_artifact, "resolve_release", side_effect=error):
                manager.start()
                manager.submit(run["id"])
                manager.stop(timeout=3)
            persisted = store.load(run["id"])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["error"]["stage"], "source")
        self.assertEqual(persisted["error"]["code"], "github_api_error")
        self.assertNotEqual(persisted["error"]["code"], "execution_worker_error")

    def test_exact_and_unique_pattern_selection_and_ambiguity(self):
        assets = [{"name": "demo-linux.tar.gz"}, {"name": "demo-arm64.tar.gz"}, {"name": "notes.txt"}]
        self.assertEqual(upstream_archive.select_asset(release(assets), recipe()["artifact"])["name"], "demo-linux.tar.gz")
        patterned = recipe(asset_name="", name_pattern="demo-linux*.tar.gz")
        self.assertEqual(upstream_archive.select_asset(release(assets), patterned["artifact"])["name"], "demo-linux.tar.gz")
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_asset(release(assets), recipe(asset_name="", name_pattern="demo-*.tar.gz")["artifact"])
        self.assertEqual(caught.exception.code, "ambiguous_release_asset")

    def test_raw_assets_are_selected_before_payload_classification(self):
        assets = [
            {"name": "demo-linux-amd64"},
            {"name": "demo-linux-arm64"},
            {"name": "demo-linux-amd64.tar.gz"},
        ]
        exact = recipe(asset_name="demo-linux-amd64", payload={"mode": "entire_archive", "include": [], "exclude": []})
        selected = upstream_archive.select_archive(release(assets), exact["artifact"])
        self.assertEqual(selected["name"], "demo-linux-amd64")
        self.assertEqual(selected["payload_kind"], "raw_file")
        self.assertEqual(selected["archive_format"], "")

        patterned = recipe(
            asset_name="", name_pattern="*-linux-amd64",
            payload={"mode": "entire_archive", "include": [], "exclude": []},
        )
        self.assertEqual(upstream_archive.select_archive(release(assets), patterned["artifact"])["name"], "demo-linux-amd64")

        missing = recipe(
            asset_name="", name_pattern="*-windows-amd64",
            payload={"mode": "entire_archive", "include": [], "exclude": []},
        )
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_archive(release(assets), missing["artifact"])
        self.assertEqual(caught.exception.code, "release_asset_not_found")
        self.assertIn("*-windows-amd64", str(caught.exception))

        case_mismatch = recipe(
            asset_name="", name_pattern="*-Linux-amd64",
            payload={"mode": "entire_archive", "include": [], "exclude": []},
        )
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_archive(release(assets), case_mismatch["artifact"])
        self.assertEqual(caught.exception.code, "release_asset_not_found")

        ambiguous = recipe(
            asset_name="", name_pattern="demo-linux-*",
            payload={"mode": "entire_archive", "include": [], "exclude": []},
        )
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_archive(release(assets), ambiguous["artifact"])
        self.assertEqual(caught.exception.code, "ambiguous_release_asset")
        self.assertEqual(caught.exception.details["assets"], sorted(row["name"] for row in assets))
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as reordered:
            upstream_archive.select_archive(release(list(reversed(assets))), ambiguous["artifact"])
        self.assertEqual(reordered.exception.details, caught.exception.details)

        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.inspect(ambiguous, release_resolver=lambda *_a, **_k: release(assets))
        self.assertEqual(caught.exception.code, "ambiguous_release_asset")
        self.assertEqual(
            caught.exception.details["sources"],
            [
                {"source": "release_asset", "name": "demo-linux-amd64", "size": 0, "payload_kind": "raw_file", "archive_format": ""},
                {"source": "release_asset", "name": "demo-linux-amd64.tar.gz", "size": 0, "payload_kind": "archive", "archive_format": "tar.gz"},
                {"source": "release_asset", "name": "demo-linux-arm64", "size": 0, "payload_kind": "raw_file", "archive_format": ""},
            ],
        )

    def test_archive_classification_remains_suffix_based_and_bounded(self):
        expected = {
            "demo.tar.gz": "tar.gz", "demo.tgz": "tgz",
            "demo.tar.xz": "tar.xz", "demo.zip": "zip",
        }
        for name, kind in expected.items():
            with self.subTest(name=name):
                self.assertEqual(upstream_archive.classify_payload(name), {"payload_kind": "archive", "archive_format": kind})
        for name in ("demo", "notes.txt", "demo.tar.zst"):
            with self.subTest(name=name):
                self.assertEqual(upstream_archive.classify_payload(name), {"payload_kind": "raw_file", "archive_format": ""})

    def test_auto_ignores_raw_assets_and_preserves_generated_source_contract(self):
        configured = recipe(asset_name="", archive_source="auto")
        selected = upstream_archive.select_archive(
            release([{"name": "demo-linux-amd64", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux-amd64"}]),
            configured["artifact"],
        )
        self.assertEqual(selected["source"], "github_source")
        self.assertEqual(selected["payload_kind"], "archive")

    def test_raw_acquisition_creates_one_verified_file_with_durable_identity(self):
        payload = b"\x7fELFraw-binary"
        digest = hashlib.sha256(payload).hexdigest()
        configured = recipe(asset_name="demo-linux-amd64", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {
            "asset_id": 456, "api_url": "https://api.github.com/repos/example/demo/releases/assets/456",
            "name": "demo-linux-amd64", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux-amd64",
            "size": len(payload), "content_type": "application/octet-stream", "digest": "sha256:" + digest,
        }

        def downloader(url, destination, token=""):
            self.assertEqual(url, asset["url"])
            self.assertEqual(Path(destination).parent.name, "source")
            Path(destination).write_bytes(payload)
            Path(destination).chmod(0o755)
            return {"path": str(destination), "size": len(payload), "sha256": digest}

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(upstream_archive, "extract_zip_archive", side_effect=AssertionError("raw extraction")), \
                mock.patch.object(upstream_archive.source_acquisition, "extract_tar_archive", side_effect=AssertionError("raw extraction")):
            result = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )
            source = Path(result["source_directory"])
            self.assertEqual([path.name for path in source.iterdir()], ["demo-linux-amd64"])
            self.assertEqual((source / "demo-linux-amd64").stat().st_mode & 0o777, 0o644)

        self.assertEqual(result["release_id"], 123)
        self.assertEqual(result["payload_kind"], "raw_file")
        self.assertEqual(result["file_count"], 1)
        self.assertIsNone(result["extraction"])
        self.assertEqual(result["asset"], {
            "asset_id": 456, "api_url": asset["api_url"], "name": asset["name"], "url": asset["url"],
            "source": "release_asset", "declared_size": len(payload), "download_size": len(payload),
            "content_type": "application/octet-stream", "sha256": digest, "expected_sha256": digest,
            "checksum_verified": True, "payload_kind": "raw_file", "file_count": 1,
        })
        self.assertEqual(result["archive_payload"]["mode"], "raw_file")
        self.assertEqual(result["archive_payload"]["selected_files"], 1)
        self.assertEqual(result["archive_payload"]["files"][0]["relative_path"], "demo-linux-amd64")

    def test_generic_non_archive_asset_is_a_raw_file(self):
        configured = recipe(asset_name="release-notes.txt", payload={"mode": "entire_archive", "include": [], "exclude": []})
        payload = b"release notes\n"
        asset = {"name": "release-notes.txt", "url": "https://github.com/example/demo/releases/download/v1.2.3/release-notes.txt", "size": len(payload), "digest": ""}
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader)
        self.assertEqual(result["payload_kind"], "raw_file")
        self.assertEqual(result["file_count"], 1)

    def test_release_zip_asset_is_selected_classified_and_acquired_as_an_archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("demo-1.2.3/demo", b"binary")
        payload = output.getvalue()
        configured = recipe(asset_name="demo.zip", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {
            "name": "demo.zip", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo.zip",
            "size": len(payload), "digest": "",
        }
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )
        self.assertEqual(result["payload_kind"], "archive")
        self.assertEqual(result["asset"]["archive_format"], "zip")
        self.assertEqual(result["extraction"]["files"], 1)
        self.assertEqual(result["archive_payload"]["files"][0]["relative_path"], "demo")

    def test_github_source_archive_tarball_is_usable_without_asset_fields(self):
        payload = tar_bytes([("demo-1.2.3/demo", b"binary", "file")])
        digest = hashlib.sha256(payload).hexdigest()
        configured = recipe(asset_name="", archive_source="github_source", archive_format="tar.gz")
        def downloader(url, destination, token=""):
            self.assertEqual(url, "https://api.github.com/repos/example/demo/tarball/v1.2.3")
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": digest}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([]), downloader=downloader)
        self.assertEqual(result["asset"]["source"], "github_source")
        self.assertEqual(result["asset"]["archive_format"], "tar.gz")
        self.assertEqual(result["archive_payload"]["files"][0]["relative_path"], "demo")

    def test_auto_uses_github_source_when_release_has_no_assets_and_reports_ambiguous_assets(self):
        configured = recipe(asset_name="", archive_source="auto")
        self.assertEqual(upstream_archive.select_archive(release([]), configured["artifact"])["source"], "github_source")
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_archive(release([{"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz"}]), configured["artifact"])
        self.assertEqual(caught.exception.code, "ambiguous_archive_source")

    def test_inspection_returns_complete_typed_inventory_without_file_contents(self):
        payload = tar_bytes([
            ("demo-1.2.3/bin/demo", b"binary", "file"),
            ("demo-1.2.3/bin/lib/helper", b"helper", "file"),
            ("demo-1.2.3/README.md", b"readme", "file"),
        ])
        configured = normalize_recipe({
            "schema_version": 5,
            "name": "demo", "package": {"name": "demo"},
            "source": {"repository": "example/demo"},
            "artifact": {"mode": "upstream_archive", "archive_source": "github_source", "archive_format": "tar.gz"},
        })
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        result = upstream_archive.inspect(configured, release_resolver=lambda *_a, **_k: release([]), downloader=downloader)
        inventory = result["inventory"]
        self.assertEqual(inventory, {
            "entries": [
                {"path": "README.md", "kind": "file", "size": 6, "mode": "0755"},
                {"path": "bin/", "kind": "directory", "descendant_files": 2},
                {"path": "bin/demo", "kind": "file", "size": 6, "mode": "0755"},
                {"path": "bin/lib/", "kind": "directory", "descendant_files": 1},
                {"path": "bin/lib/helper", "kind": "file", "size": 6, "mode": "0755"},
            ],
            "file_count": 3, "directory_count": 2, "entry_count": 5, "complete": True,
        })
        self.assertNotIn("files", result)
        self.assertNotIn("selected_files", result)
        self.assertTrue(all("content" not in row for row in inventory["entries"]))

    def test_raw_editor_inspection_uses_the_canonical_one_file_resolver(self):
        payload = b"raw"
        configured = recipe(asset_name="demo", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {"asset_id": 456, "name": "demo", "url": "https://github.com/example/demo/releases/download/v1/demo", "size": len(payload), "digest": ""}
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        result = upstream_archive.inspect(configured, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader)
        self.assertEqual(result["payload_kind"], "raw_file")
        self.assertEqual(result["file_count"], 1)
        self.assertIsNone(result["extraction"])
        self.assertEqual(result["inventory"], {
            "entries": [{"path": "demo", "kind": "file", "size": 3, "mode": "0644"}],
            "file_count": 1, "directory_count": 0, "entry_count": 1, "complete": True,
        })
        self.assertEqual(result["payload"]["mode"], "raw_file")
        self.assertEqual(result["release"]["release_id"], 123)

    def test_inventory_is_complete_beyond_old_two_thousand_file_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            for index in range(2005):
                path = source / f"data/{index:04d}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(index))
            inventory = upstream_archive.inspect_inventory(source)
            self.assertTrue(inventory["complete"])
            self.assertEqual(inventory["file_count"], 2005)
            self.assertEqual(inventory["directory_count"], 1)
            self.assertEqual(inventory["entry_count"], 2006)
            self.assertEqual(inventory["entries"][0], {"path": "data/", "kind": "directory", "descendant_files": 2005})
            self.assertEqual(inventory["entries"][-1]["path"], "data/2004.txt")

    def test_inventory_rejects_noncanonical_logical_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "bad\\name").write_text("unsafe\n")
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.inspect_inventory(source)
            self.assertEqual(caught.exception.code, "archive_inspection_failed")

    def test_inspection_reports_stale_selector_without_hiding_inventory(self):
        payload = tar_bytes([("demo-1.2.3/bin/demo", b"binary", "file")])
        configured = recipe(
            asset_name="", archive_source="github_source", archive_format="tar.gz",
            payload={"mode": "paths", "include": ["missing"], "exclude": []},
        )
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        result = upstream_archive.inspect(configured, release_resolver=lambda *_a, **_k: release([]), downloader=downloader)
        self.assertEqual(result["inventory"]["file_count"], 1)
        self.assertEqual(result["selection_error"]["code"], "archive_selection_path_not_found")
        self.assertEqual(result["selection_error"]["details"]["path"], "missing")

    def test_valid_archive_checksum_selection_and_provenance(self):
        payload = tar_bytes([("demo", b"binary", "file")])
        digest = hashlib.sha256(payload).hexdigest()
        asset = {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz", "size": len(payload), "digest": "sha256:" + digest}
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": digest}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_archive.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader)
        self.assertEqual(result["asset"]["archive_format"], "tar.gz")
        self.assertTrue(result["asset"]["checksum_verified"])
        self.assertEqual(result["asset"]["sha256"], digest)
        self.assertEqual(result["archive_payload"]["files"][0]["relative_path"], "demo")
        self.assertEqual(result["tag"], "v1.2.3")

    def test_checksum_and_missing_selected_file_fail(self):
        payload = tar_bytes([("other", b"binary", "file")])
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        base = {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1/demo-linux.tar.gz", "size": len(payload), "digest": ""}
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as missing:
            upstream_archive.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release([base]), downloader=downloader)
        self.assertEqual(missing.exception.code, "archive_selection_path_not_found")
        self.assertEqual(missing.exception.details, {"role": "include", "path": "demo", "expected_kind": "file"})
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as checksum:
            upstream_archive.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release([{**base, "digest": "sha256:" + "0" * 64}]), downloader=downloader)
        self.assertEqual(checksum.exception.code, "archive_checksum_mismatch")
        self.assertFalse((Path(temporary) / "downloads" / "demo-linux.tar.gz").exists())

    def test_download_size_failure_is_preserved(self):
        asset = {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1/demo-linux.tar.gz", "size": 999, "digest": ""}
        def downloader(_url, _destination, token=""):
            raise upstream_archive.github_client.GitHubError("archive_too_large", "Archive exceeds download size limit")
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader)
        self.assertEqual(caught.exception.code, "archive_too_large")

    def test_raw_download_size_digest_filename_and_regular_file_fail_closed(self):
        payload = b"raw payload"
        configured = recipe(
            asset_name="", name_pattern="*",
            payload={"mode": "entire_archive", "include": [], "exclude": []},
        )

        def write_payload(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        cases = [
            ({"name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload) + 1, "digest": ""}, write_payload, "asset_size_mismatch"),
            ({"name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload), "digest": "sha256:" + "0" * 64}, write_payload, "asset_checksum_mismatch"),
            ({"name": "../raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload), "digest": ""}, write_payload, "unsafe_release_asset_name"),
            ({"name": "dir\\raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload), "digest": ""}, write_payload, "unsafe_release_asset_name"),
        ]
        for asset, downloader, code in cases:
            with self.subTest(code=code, name=asset["name"]), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                    upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, row=asset, **_k: release([row]), downloader=downloader)
                self.assertEqual(caught.exception.code, code)

        def truncated_downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload[:-1])
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload[:-1]).hexdigest()}

        asset = {"name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload), "digest": ""}
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=truncated_downloader)
        self.assertEqual(caught.exception.code, "asset_size_mismatch")

        def directory_downloader(_url, destination, token=""):
            Path(destination).mkdir()
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=directory_downloader)
        self.assertEqual(caught.exception.code, "raw_file_verification_failed")

        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "source").write_bytes(b"replacement")
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=write_payload)
            self.assertEqual(caught.exception.code, "raw_file_verification_failed")

        def symlink_downloader(_url, destination, token=""):
            outside = Path(destination).parents[1] / "outside"
            outside.write_bytes(payload)
            Path(destination).symlink_to(outside)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        asset = {"name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw", "size": len(payload), "digest": ""}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=symlink_downloader)
            self.assertEqual(caught.exception.code, "raw_file_verification_failed")
            self.assertFalse((Path(temporary) / "source/raw").exists())

        def extra_file_downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            Path(destination).with_name("unexpected").write_bytes(b"extra")
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=extra_file_downloader)
        self.assertEqual(caught.exception.code, "raw_file_verification_failed")

        def swapped_source_downloader(_url, destination, token=""):
            destination = Path(destination)
            original_source = destination.parent.with_name("original-source")
            outside = destination.parents[1] / "outside"
            destination.parent.rename(original_source)
            outside.mkdir()
            destination.parent.symlink_to(outside, target_is_directory=True)
            destination.write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=swapped_source_downloader)
            self.assertEqual(caught.exception.code, "raw_file_verification_failed")
            self.assertEqual((Path(temporary) / "outside/raw").read_bytes(), payload)
            self.assertFalse((Path(temporary) / "original-source/raw").exists())

        downloaded_target = None
        def replaced_after_hash_downloader(_url, destination, token=""):
            nonlocal downloaded_target
            downloaded_target = Path(destination)
            downloaded_target.write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

        original_fd_sha256 = upstream_archive._fd_sha256
        def replace_after_hash(descriptor):
            digest = original_fd_sha256(descriptor)
            downloaded_target.unlink()
            downloaded_target.write_bytes(b"replacement")
            return digest

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(upstream_archive, "_fd_sha256", side_effect=replace_after_hash), \
                self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=replaced_after_hash_downloader)
        self.assertEqual(caught.exception.code, "raw_file_verification_failed")

    def test_raw_download_failure_is_preserved_and_transient_url_is_not_persisted(self):
        configured = recipe(asset_name="raw", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {"name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw?token=secret", "size": 3, "digest": ""}
        def failed(_url, destination, token=""):
            Path(destination).write_bytes(b"partial")
            raise upstream_archive.github_client.GitHubError("source_download_failed", "Source download failed")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
                upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=failed)
            self.assertEqual(caught.exception.code, "source_download_failed")
            self.assertFalse((Path(temporary) / "source/raw").exists())

        def downloaded(url, destination, token=""):
            self.assertIn("token=secret", url)
            Path(destination).write_bytes(b"raw")
            return {"path": str(destination), "size": 3, "sha256": hashlib.sha256(b"raw").hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            result = upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloaded)
        self.assertEqual(result["asset"]["url"], "")
        self.assertNotIn("secret", json.dumps(result))

    def test_raw_downloader_digest_disagreement_fails_closed(self):
        configured = recipe(asset_name="raw", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {
            "name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw",
            "size": 3, "digest": "",
        }

        def changed_after_download(_url, destination, token=""):
            Path(destination).write_bytes(b"one")
            reported = hashlib.sha256(b"one").hexdigest()
            Path(destination).write_bytes(b"two")
            return {"path": str(destination), "size": 3, "sha256": reported}

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]),
                downloader=changed_after_download,
            )
        self.assertEqual(caught.exception.code, "raw_file_verification_failed")

    def test_raw_identity_is_reverified_from_anchored_descriptors_during_staging(self):
        payload = b"raw"
        digest = hashlib.sha256(payload).hexdigest()
        configured = recipe(asset_name="raw", payload={"mode": "entire_archive", "include": [], "exclude": []})
        configured["install"]["config_files"][0]["source"] = "raw"
        asset = {
            "name": "raw", "url": "https://github.com/example/demo/releases/download/v1/raw",
            "size": len(payload), "digest": "sha256:" + digest,
        }

        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": digest}

        mutations = {
            "same_inode_content": lambda source: (source / "raw").write_bytes(b"bad"),
            "replacement_inode": lambda source: ((source / "raw").unlink(), (source / "raw").write_bytes(payload)),
            "extra_file": lambda source: (source / "unexpected").write_bytes(b"extra"),
            "replacement_directory": lambda source: (
                source.rename(source.with_name("original-source")),
                source.mkdir(),
                os.link(source.with_name("original-source") / "raw", source / "raw"),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                acquired = upstream_archive.acquire(
                    configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
                )
                mutate(Path(acquired["source_directory"]))
                with self.assertRaises(debian_packaging.PackagingError) as caught:
                    debian_packaging.prepare_staging(
                        configured,
                        {"output": {"mode": "archive_payload", "payload": acquired["archive_payload"]}, "version": "1.2.3-1"},
                        temporary,
                        preview=True,
                    )
                self.assertEqual(caught.exception.code, "invalid_install_content")
                self.assertFalse((Path(temporary) / "staging/usr/bin/demo").exists())

        with tempfile.TemporaryDirectory() as temporary:
            acquired = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )
            equivalent = json.loads(json.dumps(configured))
            equivalent["install"]["config_files"][0]["source"] = "./raw"
            staged = debian_packaging.prepare_staging(
                equivalent,
                {"output": {"mode": "archive_payload", "payload": acquired["archive_payload"]}, "version": "1.2.3-1"},
                temporary,
                preview=True,
            )
            self.assertEqual(Path(staged["staging_directory"], "usr/bin/demo").read_bytes(), payload)

        with tempfile.TemporaryDirectory() as temporary:
            acquired = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )
            source = Path(acquired["source_directory"])
            (source / "unexpected").write_bytes(b"attacker-controlled")
            injected = json.loads(json.dumps(configured))
            injected["install"]["config_files"][0]["source"] = "unexpected"
            with self.assertRaises(debian_packaging.PackagingError) as caught:
                debian_packaging.prepare_staging(
                    injected,
                    {"output": {"mode": "archive_payload", "payload": acquired["archive_payload"]}, "version": "1.2.3-1"},
                    temporary,
                    preview=True,
                )
            self.assertEqual(caught.exception.code, "invalid_install_content")

        with tempfile.TemporaryDirectory() as temporary:
            acquired = upstream_archive.acquire(
                configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )
            original_copy = debian_packaging._copy_verified_raw_record
            copy_calls = 0

            def remove_after_initial_verification(record, source_root, target=None):
                nonlocal copy_calls
                copy_calls += 1
                result = original_copy(record, source_root, target)
                if target is None:
                    (Path(source_root) / "raw").unlink()
                return result

            with mock.patch.object(debian_packaging, "_copy_verified_raw_record", side_effect=remove_after_initial_verification), \
                    self.assertRaises(debian_packaging.PackagingError) as caught:
                debian_packaging.prepare_staging(
                    configured,
                    {"output": {"mode": "archive_payload", "payload": acquired["archive_payload"]}, "version": "1.2.3-1"},
                    temporary,
                    preview=True,
                )
            self.assertEqual(caught.exception.code, "invalid_install_content")
            self.assertEqual(copy_calls, 2)
            self.assertFalse((Path(temporary) / "staging/usr/bin/demo").exists())

    def test_recognized_corrupt_archive_is_never_downgraded_to_raw(self):
        payload = b"not an archive"
        configured = recipe(asset_name="demo.tar.gz", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {"name": "demo.tar.gz", "url": "https://github.com/example/demo/releases/download/v1/demo.tar.gz", "size": len(payload), "digest": ""}
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader)
        self.assertEqual(caught.exception.code, "archive_extract_failed")

    def test_all_supported_release_tar_suffixes_still_extract(self):
        cases = [("demo.tar.gz", "w:gz"), ("demo.tgz", "w:gz"), ("demo.tar.xz", "w:xz")]
        for name, mode in cases:
            with self.subTest(name=name):
                payload = tar_bytes([("root/demo", b"binary", "file")], mode=mode)
                configured = recipe(asset_name=name, payload={"mode": "entire_archive", "include": [], "exclude": []})
                asset = {"name": name, "url": f"https://github.com/example/demo/releases/download/v1/{name}", "size": len(payload), "digest": ""}
                def downloader(_url, destination, token="", content=payload):
                    Path(destination).write_bytes(content)
                    return {"path": str(destination), "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                with tempfile.TemporaryDirectory() as temporary:
                    result = upstream_archive.acquire(configured, temporary, release_resolver=lambda *_a, row=asset, **_k: release([row]), downloader=downloader)
                self.assertEqual(result["payload_kind"], "archive")
                self.assertEqual(result["file_count"], 1)

    def test_tar_traversal_symlink_and_expanded_size_are_rejected(self):
        cases = [
            ([("../escape", b"bad", "file")], {}, "Unsafe path"),
            ([("link", b"/etc/passwd", "symlink")], {}, "Unsupported link"),
            ([("large", b"12345", "file")], {"max_uncompressed_bytes": 4}, "size limit"),
        ]
        for entries, options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "test.tar.gz"
                archive.write_bytes(tar_bytes(entries))
                with self.assertRaisesRegex(Exception, message):
                    upstream_archive.source_acquisition.extract_tar_archive(archive, Path(temporary) / "out", **options)

    def test_zip_is_extracted_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "demo.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("root/demo", b"binary")
            result = upstream_archive.extract_zip_archive(archive, Path(temporary) / "out")
            self.assertEqual(result["files"], 1)
            self.assertEqual((Path(temporary) / "out/demo").read_bytes(), b"binary")

    def test_zip_traversal_and_symlink_are_rejected(self):
        for kind in ("traversal", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "demo.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    if kind == "traversal":
                        bundle.writestr("../escape", b"bad")
                    else:
                        info = zipfile.ZipInfo("link")
                        info.create_system = 3
                        info.external_attr = (0o120777 << 16)
                        bundle.writestr(info, "/etc/passwd")
                with self.assertRaises(upstream_archive.UpstreamArchiveError):
                    upstream_archive.extract_zip_archive(archive, Path(temporary) / "out")

    def test_pipeline_records_provenance_and_has_no_build_commands(self):
        acquired = {
            "repository": "example/demo", "strategy": "latest_release", "ref": "v1.2.3", "tag": "v1.2.3", "release_name": "v1.2.3", "release_url": "https://github.com/example/demo/releases/tag/v1.2.3", "upstream_version": "1.2.3", "debian_version": "1.2.3-1", "artifact_mode": "upstream_archive",
            "payload_kind": "archive", "file_count": 100,
            "asset": {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz", "download_size": 6, "sha256": "a" * 64, "archive_format": "tar.gz"}, "extraction": {"files": 100},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            configured = runtime_recipe_for_storage(recipe())
            expected = admitted_asset_identity(configured)
            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                source.mkdir(exist_ok=True)
                binary = source / "demo"
                binary.write_bytes(b"binary")
                return {**acquired, "upstream_identity": expected_identity, "source_directory": str(source), "archive_payload": upstream_archive.resolve_payload(_recipe, source)}
            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(configured, store=store, dry_run=True, manual_source_provenance=expected)["id"],
                store=store, acquire=acquire,
            )
            persisted = store.load(result["run_id"])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["build"]["commands"], [])
        self.assertEqual(result["source"]["asset"]["sha256"], "a" * 64)
        self.assertNotIn("source_directory", result["source"])
        self.assertEqual(result["source"]["archive_payload"]["selected_files"], 1)
        self.assertNotIn("files", result["source"]["archive_payload"])
        self.assertNotIn("files", result["detection"]["archive_payload"])
        self.assertEqual(result["detection"]["file_count"], 1)
        self.assertNotIn("files", result["build"]["output"]["payload"])
        self.assertEqual(next(step for step in persisted["steps"] if step["name"] == "build")["status"], "skipped")

    def test_test_and_build_share_raw_resolution_and_explicit_executable_mapping(self):
        payload = b"\x7fELFdemo"
        digest = hashlib.sha256(payload).hexdigest()
        configured = recipe(asset_name="demo", payload={"mode": "entire_archive", "include": [], "exclude": []})
        asset = {
            "asset_id": 456, "api_url": "https://api.github.com/repos/example/demo/releases/assets/456",
            "name": "demo", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo",
            "size": len(payload), "content_type": "application/octet-stream", "digest": "sha256:" + digest,
        }
        downloads = []

        def downloader(url, destination, token=""):
            downloads.append(url)
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": digest}

        expected = release_asset_identity(
            runtime_recipe_for_storage(configured), release([asset]), asset, "raw_file",
        )

        def acquire(_recipe, workspace, token="", expected_identity=None):
            return upstream_archive.acquire(
                _recipe, workspace, token=token,
                expected_identity=expected_identity,
                release_resolver=lambda *_a, **_k: release([asset]), downloader=downloader,
            )

        def build_deb(_recipe, _staging, workspace, **_kwargs):
            artifact = Path(workspace) / "artifacts/demo_1.2.3-1_amd64.deb"
            artifact.write_bytes(b"deb")
            return {
                "name": artifact.name, "path": str(artifact), "size": 3,
                "sha256": hashlib.sha256(b"deb").hexdigest(),
                "build_command": {"status": "success", "exit_code": 0},
            }

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(build_pipeline.debian_packaging, "build_deb", side_effect=build_deb):
            root = Path(temporary)
            admitted = runtime_recipe_for_storage(configured)
            tested = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(
                    admitted,
                    store=BuildStore(root / "test-builds"),
                    dry_run=True,
                    manual_source_provenance=expected,
                )["id"],
                store=BuildStore(root / "test-builds"), acquire=acquire,
            )
            built = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(
                    admitted,
                    store=BuildStore(root / "real-builds"),
                    dry_run=False,
                    manual_source_provenance=expected,
                )["id"],
                store=BuildStore(root / "real-builds"), acquire=acquire,
            )
            for result in (tested, built):
                self.assertEqual(result["source"]["payload_kind"], "raw_file")
                self.assertEqual(result["source"]["file_count"], 1)
                self.assertEqual(result["source"]["asset"]["sha256"], digest)
                self.assertNotIn("source_directory", result["source"])
                installed = Path(result["workspace"]) / "staging/usr/bin/demo"
                self.assertEqual(installed.read_bytes(), payload)
                self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
                self.assertEqual(result["detection"]["detected_files"], ["demo"])
            self.assertEqual(tested["status"], "prepared")
            self.assertEqual(built["status"], "success")
        immutable_url = "https://api.github.com/repos/example/demo/releases/assets/456"
        self.assertEqual(downloads, [immutable_url, immutable_url])

    def test_pipeline_stages_recursive_payload_with_exclusion_and_records_only_compact_facts(self):
        configured = recipe(payload={"mode": "paths", "include": ["app/"], "exclude": ["app/cache/"]})
        configured["install"].update({
            "destination": "/opt/demo", "content": {"source": "build_output", "path": ""},
            "config_files": [],
        })
        acquired = {
            "repository": "example/demo", "strategy": "latest_release", "ref": "v1.2.3", "tag": "v1.2.3",
            "release_name": "v1.2.3", "release_url": "https://github.com/example/demo/releases/tag/v1.2.3",
            "upstream_version": "1.2.3", "debian_version": "1.2.3-1", "artifact_mode": "upstream_archive",
            "asset": {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz", "download_size": 12, "sha256": "a" * 64, "archive_format": "tar.gz"},
            "extraction": {"files": 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            admitted = runtime_recipe_for_storage(configured)
            expected = admitted_asset_identity(admitted)

            def acquire(_recipe, workspace, token="", expected_identity=None):
                source = Path(workspace) / "source"
                (source / "app/cache").mkdir(parents=True)
                (source / "app/main.py").write_text("print('ready')\n")
                (source / "app/cache/state.db").write_bytes(b"cache")
                return {
                    **acquired, "upstream_identity": expected_identity, "source_directory": str(source),
                    "archive_payload": upstream_archive.resolve_payload(_recipe, source),
                }

            result = build_pipeline.execute_pipeline_run(
                build_pipeline.create_pipeline_run(admitted, store=store, dry_run=True, manual_source_provenance=expected)["id"],
                store=store, acquire=acquire,
            )
            persisted = store.load(result["run_id"])
            staged = Path(result["workspace"]) / "staging/opt/demo"
            self.assertTrue((staged / "app/main.py").is_file())
            self.assertFalse((staged / "app/cache").exists())

        self.assertEqual(result["status"], "prepared")
        facts = result["detection"]["archive_payload"]
        self.assertEqual(facts["selected_directories"], 1)
        self.assertEqual(facts["excluded_directories"], 1)
        self.assertEqual(facts["selected_files"], 1)
        self.assertNotIn("files", facts)
        stored_staging = next(step for step in persisted["steps"] if step["name"] == "staging")["details"]
        self.assertEqual(stored_staging["content_file_count"], 1)
        self.assertNotIn("content_files", stored_staging)

if __name__ == "__main__":
    unittest.main()
