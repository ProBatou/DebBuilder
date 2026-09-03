import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import github_client, source_acquisition


def recipe(tracking="latest_release", ref="", version_source="tag"):
    return {
        "schema_version": 1, "name": "demo", "active": True,
        "package": {"name": "demo", "version_revision": "2", "architecture": "amd64"},
        "source": {"provider": "github", "repository": "owner/demo", "tracking": tracking, "ref": ref, "version": {"source": version_source, "expression": ""}},
    }


class SourceResolutionTests(unittest.TestCase):
    def test_latest_release_resolves_upstream_and_debian_versions(self):
        release = {"tag": "v1.4.2", "name": "Demo 1.4.2", "archive_url": "https://api.github.com/repos/owner/demo/tarball/v1.4.2", "url": "https://github.com/owner/demo/releases/tag/v1.4.2"}
        with mock.patch("debbuilder.source_acquisition.github_client.repo_info", return_value={"repository": "owner/demo"}), mock.patch("debbuilder.source_acquisition.github_client.latest_release", return_value=release):
            result = source_acquisition.resolve_source(recipe())
        self.assertEqual(result["upstream_version"], "1.4.2")
        self.assertEqual(result["debian_version"], "1.4.2-2")
        self.assertEqual(result["strategy"], "latest_release")

    def test_explicit_tag_and_manual_ref_use_existing_github_client(self):
        resolved = {"tag": "v2.0.0", "ref": "v2.0.0", "name": "v2.0.0", "archive_url": "https://api.github.com/repos/owner/demo/tarball/v2.0.0"}
        with mock.patch("debbuilder.source_acquisition.github_client.repo_info", return_value={"repository": "owner/demo"}), mock.patch("debbuilder.source_acquisition.github_client.resolve_ref", return_value=resolved) as call:
            result = source_acquisition.resolve_source(recipe("tag", "v2.0.0"))
        call.assert_called_once_with("owner/demo", "v2.0.0", kind="tag", token="")
        self.assertEqual(result["debian_version"], "2.0.0-2")

    def test_errors_have_stable_codes_and_messages(self):
        error = github_client.GitHubError("repository_not_found", "Repository not found", status=404)
        with mock.patch("debbuilder.source_acquisition.github_client.repo_info", side_effect=error):
            with self.assertRaises(source_acquisition.SourceError) as raised:
                source_acquisition.resolve_source(recipe())
        self.assertEqual(raised.exception.code, "repository_not_found")
        self.assertEqual(str(raised.exception), "Repository not found")

    def test_build_provided_version_fails_before_download(self):
        release = {"tag": "v1.0", "name": "v1.0", "archive_url": "https://api.github.com/repos/owner/demo/tarball/v1.0"}
        with mock.patch("debbuilder.source_acquisition.github_client.repo_info", return_value={"repository": "owner/demo"}), mock.patch("debbuilder.source_acquisition.github_client.latest_release", return_value=release):
            with self.assertRaises(source_acquisition.SourceError) as raised:
                source_acquisition.resolve_source(recipe(version_source="build"))
        self.assertEqual(raised.exception.code, "unable_to_determine_version")

    def test_download_accepts_only_github_https_hosts_and_checks_redirect(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(github_client.GitHubError):
                github_client.download_archive("https://evil.example/source.tar.gz", Path(temporary) / "bad")

            class Response:
                def __init__(self): self.reads = 0
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def geturl(self): return "https://codeload.github.com/owner/demo/legacy.tar.gz/v1"
                def read(self, _size):
                    self.reads += 1
                    return b"archive" if self.reads == 1 else b""

            target = Path(temporary) / "source.tar.gz"
            result = github_client.download_archive("https://api.github.com/repos/owner/demo/tarball/v1", target, urlopen=lambda *_args, **_kwargs: Response())
            self.assertEqual(target.read_bytes(), b"archive")
            self.assertEqual(result["size"], 7)

    def test_download_rejects_redirect_outside_github_and_removes_partial_file(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return "https://evil.example/archive"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "source.tar.gz"
            with self.assertRaises(github_client.GitHubError):
                github_client.download_archive("https://api.github.com/repos/owner/demo/tarball/v1", target, urlopen=lambda *_args, **_kwargs: Response())
            self.assertFalse(target.exists())


class SafeExtractionTests(unittest.TestCase):
    def make_tar(self, path, entries):
        with tarfile.open(path, "w:gz") as bundle:
            for name, content, kind in entries:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    payload = content.encode()
                    info.size = len(payload)
                    bundle.addfile(info, io.BytesIO(payload))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = content
                    bundle.addfile(info)

    def test_extracts_regular_files_and_strips_github_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            self.make_tar(archive, [("owner-demo-sha/package.json", "{}", "file"), ("owner-demo-sha/src/app.js", "ok", "file")])
            result = source_acquisition.extract_tar_archive(archive, root / "source")
            self.assertEqual((root / "source/package.json").read_text(), "{}")
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["stripped_root"], "owner-demo-sha")

    def test_rejects_traversal_absolute_paths_and_links(self):
        for name, content, kind in [("../escape", "bad", "file"), ("/absolute", "bad", "file"), ("root/link", "../../outside", "symlink")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "source.tar.gz"
                self.make_tar(archive, [(name, content, kind)])
                with self.assertRaises(source_acquisition.SourceError) as raised:
                    source_acquisition.extract_tar_archive(archive, root / "source")
                self.assertEqual(raised.exception.code, "source_extract_failed")
                self.assertFalse((root.parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
