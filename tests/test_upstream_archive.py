import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from debbuilder import build_pipeline, upstream_archive
from debbuilder.build_store import BuildStore
from debbuilder.recipe_schema import validate_recipe_metadata


def recipe(**artifact):
    return validate_recipe_metadata({
        "name": "demo", "package": {"name": "demo", "architecture": "amd64", "maintainer": "Demo <demo@example.org>"},
        "source": {"repository": "example/demo", "tracking": "latest_release"},
        "artifact": {"mode": "upstream_archive", "type": "archive", "architecture": "amd64", "asset_name": "demo-linux.tar.gz", "selected_files": ["demo"], **artifact},
        "build": {"commands": [], "source_changes": [], "output": {"mode": "source"}},
        "install": {"content": {"source": "configured_files"}, "owner": {"user": "root", "group": "root"}, "config_files": [{"source": "demo", "destination": "/usr/bin/demo", "policy": "replace", "mode": "0755", "owner": "root", "group": "root"}]},
    })


def release(assets):
    return {"repository": "example/demo", "tag": "v1.2.3", "ref": "v1.2.3", "name": "v1.2.3", "url": "https://github.com/example/demo/releases/tag/v1.2.3", "upstream_version": "1.2.3", "assets": assets}


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
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
    def test_exact_and_unique_pattern_selection_and_ambiguity(self):
        assets = [{"name": "demo-linux.tar.gz"}, {"name": "demo-arm64.tar.gz"}, {"name": "notes.txt"}]
        self.assertEqual(upstream_archive.select_asset(release(assets), recipe()["artifact"])["name"], "demo-linux.tar.gz")
        patterned = recipe(asset_name="", name_pattern="demo-linux*.tar.gz")
        self.assertEqual(upstream_archive.select_asset(release(assets), patterned["artifact"])["name"], "demo-linux.tar.gz")
        with self.assertRaises(upstream_archive.UpstreamArchiveError) as caught:
            upstream_archive.select_asset(release(assets), recipe(asset_name="", name_pattern="demo-*.tar.gz")["artifact"])
        self.assertEqual(caught.exception.code, "ambiguous_release_asset")

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
        self.assertEqual(result["selected_files"][0]["relative_path"], "demo")
        self.assertEqual(result["tag"], "v1.2.3")

    def test_checksum_and_missing_selected_file_fail(self):
        payload = tar_bytes([("other", b"binary", "file")])
        def downloader(_url, destination, token=""):
            Path(destination).write_bytes(payload)
            return {"path": str(destination), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        base = {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1/demo-linux.tar.gz", "size": len(payload), "digest": ""}
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(upstream_archive.UpstreamArchiveError) as missing:
            upstream_archive.acquire(recipe(), temporary, release_resolver=lambda *_a, **_k: release([base]), downloader=downloader)
        self.assertEqual(missing.exception.code, "selected_file_not_found")
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
            "asset": {"name": "demo-linux.tar.gz", "url": "https://github.com/example/demo/releases/download/v1.2.3/demo-linux.tar.gz", "download_size": 6, "sha256": "a" * 64, "archive_format": "tar.gz"}, "extraction": {"files": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = BuildStore(Path(temporary) / "builds")
            def acquire(_recipe, workspace, token=""):
                source = Path(workspace) / "source"
                source.mkdir(exist_ok=True)
                binary = source / "demo"
                binary.write_bytes(b"binary")
                return {**acquired, "source_directory": str(source), "selected_files": [{"relative_path": "demo", "path": str(binary), "size": 6, "mode": "0755"}]}
            result = build_pipeline.run_pipeline(recipe(), store=store, dry_run=True, acquire=acquire)
            persisted = store.load(result["run_id"])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["build"]["commands"], [])
        self.assertEqual(result["source"]["asset"]["sha256"], "a" * 64)
        self.assertEqual(next(step for step in persisted["steps"] if step["name"] == "build")["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
