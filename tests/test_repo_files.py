import os
import tempfile
import unittest
from pathlib import Path

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
            for request in ("/pool/linked.deb", "/pool/../outside.deb", "/conf/distributions"):
                with self.subTest(request=request), open_public_repo_file(root, request) as opened:
                    self.assertIsNone(opened)


if __name__ == "__main__":
    unittest.main()
