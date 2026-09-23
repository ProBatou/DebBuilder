import io
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from debbuilder import github_client, source_acquisition


def recipe(tracking="latest_release", ref="", version_source="tag", version_expression=""):
    return {
        "schema_version": 5, "name": "demo", "active": True,
        "package": {"name": "demo", "version_revision": "2", "architecture": "amd64"},
        "source": {"provider": "github", "repository": "owner/demo", "tracking": tracking, "ref": ref, "version": {"source": version_source, "expression": version_expression}},
    }


class SourceResolutionTests(unittest.TestCase):
    def test_rate_limit_retry_metadata_is_normalized_without_persisting_headers(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/demo", 429, "limited",
            {"Retry-After": "120", "X-RateLimit-Reset": "2000000180"}, None,
        )
        with mock.patch.object(github_client.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(github_client.GitHubError) as caught:
                github_client.request_json("/repos/owner/demo")
        self.assertEqual(caught.exception.code, "github_rate_limited")
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertEqual(caught.exception.rate_limit_reset, 2000000180)
        self.assertNotIn("Retry-After", caught.exception.as_dict())

    def test_unbounded_retry_headers_are_ignored(self):
        headers = {
            "Retry-After": "9" * 1000,
            "X-RateLimit-Reset": "8" * 1000,
        }
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "https://api.github.com/rate", 429, "limited", headers, None,
        )):
            with self.assertRaises(github_client.GitHubError) as caught:
                github_client.request_json("/rate")
        self.assertIsNone(caught.exception.retry_after_seconds)
        self.assertIsNone(caught.exception.rate_limit_reset)

    def test_latest_release_preserves_bounded_release_and_asset_identity(self):
        response = {
            "id": 123, "tag_name": "v1.2.3", "name": "v1.2.3",
            "html_url": "https://github.com/owner/demo/releases/tag/v1.2.3",
            "tarball_url": "https://api.github.com/repos/owner/demo/tarball/v1.2.3",
            "zipball_url": "https://api.github.com/repos/owner/demo/zipball/v1.2.3",
            "assets": [{
                "id": 456, "url": "https://api.github.com/repos/owner/demo/releases/assets/456",
                "name": "demo", "browser_download_url": "https://github.com/owner/demo/releases/download/v1.2.3/demo",
                "size": 42, "content_type": "application/octet-stream", "digest": "sha256:" + "a" * 64,
                "uploader": {"login": "not-needed"},
            }],
            "body": "not-needed",
        }
        with mock.patch.object(github_client, "request_json", return_value=response):
            result = github_client.latest_release("owner/demo")
        self.assertEqual(result["release_id"], 123)
        self.assertEqual(result["assets"], [{
            "asset_id": 456, "api_url": "https://api.github.com/repos/owner/demo/releases/assets/456",
            "name": "demo", "url": "https://github.com/owner/demo/releases/download/v1.2.3/demo",
            "size": 42, "content_type": "application/octet-stream", "digest": "sha256:" + "a" * 64,
        }])
        self.assertNotIn("body", result)

    def test_latest_release_resolves_upstream_and_debian_versions(self):
        release = {"tag": "v1.4.2", "name": "Demo 1.4.2", "archive_url": "https://api.github.com/repos/owner/demo/tarball/v1.4.2", "url": "https://github.com/owner/demo/releases/tag/v1.4.2"}
        with mock.patch("debbuilder.source_acquisition.github_client.repo_info", return_value={"repository": "owner/demo"}), mock.patch("debbuilder.source_acquisition.github_client.latest_release", return_value=release):
            result = source_acquisition.resolve_source(recipe())
        self.assertEqual(result["upstream_version"], "1.4.2")
        self.assertEqual(result["debian_version"], "1.4.2-2")
        self.assertEqual(result["strategy"], "latest_release")

    def test_exact_latest_release_pins_generated_archive_to_resolved_commit(self):
        release = {
            "release_id": 123, "tag": "v1.4.2", "name": "Demo 1.4.2",
            "archive_url": "https://api.github.com/repos/owner/demo/tarball/v1.4.2",
        }
        commit = "ab" * 20
        with mock.patch.object(github_client, "repo_info", return_value={"repository": "owner/demo"}), \
                mock.patch.object(github_client, "latest_release_exact", return_value=release), \
                mock.patch.object(github_client, "resolve_ref", return_value={
                    "commit": commit, "ref_object_sha": "cd" * 20,
                }):
            result = source_acquisition.resolve_source(recipe(), exact=True)
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["archive_url"], f"https://api.github.com/repos/owner/demo/tarball/{commit}")
        self.assertEqual(result["upstream_identity"]["release_id"], "123")
        self.assertEqual(result["upstream_identity"]["commit_sha"], commit)

    def test_release_asset_enumeration_is_bounded_and_complete(self):
        first = [{
            "id": index, "name": f"asset-{index}", "size": index,
            "url": f"https://api.github.com/repos/owner/demo/releases/assets/{index}",
            "browser_download_url": f"https://github.com/owner/demo/releases/download/v1/asset-{index}",
        } for index in range(100)]
        last = [{
            "id": 100, "name": "asset-100", "size": 100,
            "url": "https://api.github.com/repos/owner/demo/releases/assets/100",
            "browser_download_url": "https://github.com/owner/demo/releases/download/v1/asset-100",
        }]
        with mock.patch.object(github_client, "request_json", side_effect=[first, last]) as request:
            assets = github_client.release_assets("owner/demo", 123)
        self.assertEqual(len(assets), 101)
        self.assertEqual(request.call_count, 2)
        with mock.patch.object(github_client, "request_json", return_value=first):
            with self.assertRaises(github_client.GitHubError):
                github_client.release_assets("owner/demo", 123, max_pages=2)

    def test_tag_resolution_retains_ref_object_and_peeled_commit(self):
        ref_object = "cd" * 20
        commit = "ab" * 20
        with mock.patch.object(github_client, "request_json", side_effect=[
            {"object": {"sha": ref_object, "type": "tag"}},
            {"object": {"sha": commit, "type": "commit"}},
        ]) as request:
            result = github_client.resolve_ref("owner/demo", "v1.2.3", kind="tag")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["ref_object_sha"], ref_object)
        self.assertEqual(result["commit"], commit)
        self.assertTrue(result["archive_url"].endswith("/" + commit))

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

    def test_supported_version_sources_resolve_before_build(self):
        resolved = {
            "tag": "v2.3.4", "ref": "v2.3.4", "name": "2.3.4",
            "archive_url": "https://api.github.com/repos/owner/demo/tarball/v2.3.4",
        }
        for source, expression in (("tag", ""), ("release_name", ""), ("regex", r"([0-9]+(?:\.[0-9]+)+)")):
            with self.subTest(source=source), \
                    mock.patch.object(github_client, "repo_info", return_value={"repository": "owner/demo"}), \
                    mock.patch.object(github_client, "latest_release", return_value=resolved):
                result = source_acquisition.resolve_source(recipe(version_source=source, version_expression=expression))
            self.assertEqual(result["upstream_version"], "2.3.4")
            self.assertEqual(result["debian_version"], "2.3.4-2")

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
