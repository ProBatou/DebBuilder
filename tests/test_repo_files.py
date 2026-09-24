import http.client
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server as server_entrypoint
from debbuilder.repo_files import open_public_repo_file


class PublicRepositoryFileTests(unittest.TestCase):
    def test_stream_uses_pinned_descriptor_across_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            target = root / "pool/main/d/demo.deb"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"original")
            with open_public_repo_file(root, "/pool/main/d/demo.deb") as opened:
                self.assertIsNotNone(opened)
                fd, info, relative = opened
                replacement = target.with_suffix(".new")
                replacement.write_bytes(b"replacement")
                os.replace(replacement, target)
                self.assertEqual(os.read(fd, info.st_size), b"original")
                self.assertEqual(relative.as_posix(), "pool/main/d/demo.deb")
            self.assertFalse((root / ".debbuilder-repository.lock").exists())

    def test_symlink_and_traversal_are_not_served(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            pool = root / "pool"
            pool.mkdir(parents=True)
            outside = Path(temporary) / "outside.deb"
            outside.write_bytes(b"secret")
            (pool / "linked.deb").symlink_to(outside)
            for request in (
                "/pool/missing.deb", "/pool/linked.deb",
                "/pool/../outside.deb", "/conf/distributions",
            ):
                with self.subTest(request=request), open_public_repo_file(root, request) as opened:
                    self.assertIsNone(opened)

    def test_server_streams_get_and_head_from_pinned_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            target = root / "pool/main/d/demo.deb"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"package payload")
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_entrypoint.RepositoryHandler)
            thread = threading.Thread(target=httpd.serve_forever)
            thread.start()
            try:
                with mock.patch.object(server_entrypoint, "REPO_ROOT", root):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", httpd.server_address[1], timeout=5,
                    )
                    connection.request("GET", "/pool/main/d/demo.deb")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Length"), str(len(b"package payload")))
                    self.assertEqual(response.read(), b"package payload")
                    connection.close()

                    connection = http.client.HTTPConnection(
                        "127.0.0.1", httpd.server_address[1], timeout=5,
                    )
                    connection.request("HEAD", "/pool/main/d/demo.deb")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Length"), str(len(b"package payload")))
                    self.assertEqual(response.read(), b"")
                    connection.close()
            finally:
                httpd.shutdown()
                thread.join(2)
                httpd.server_close()
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
