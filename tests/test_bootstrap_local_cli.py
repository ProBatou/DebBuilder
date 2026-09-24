import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bootstrap_local


class BootstrapLocalCliTests(unittest.TestCase):
    def test_environment_file_is_loaded_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "debbuilder.env"
            path.write_text("# packaged settings\nGNUPGHOME=/tmp/bootstrap-test-gnupg\nDEBBUILDER_SUITE='test suite'\n")
            runtime = SimpleNamespace(data=Path(temporary) / "data", repository_root=Path(temporary) / "repo")
            with mock.patch.object(bootstrap_local, "ENVIRONMENT_FILE", path), \
                    mock.patch.dict(os.environ, {"GNUPGHOME": "", "DEBBUILDER_SUITE": ""}), \
                    mock.patch("debbuilder.app.RUNTIME", runtime), \
                    mock.patch("debbuilder.app.repo_settings", return_value={"distribution": "Luminous", "component": "main", "repository": "http://127.0.0.1:8099/repository"}), \
                    mock.patch("debbuilder.local_repository_bootstrap.bootstrap_repository") as bootstrap:
                self.assertEqual(bootstrap_local.main(["--environment-file", str(path)]), 0)
                self.assertEqual(os.environ["GNUPGHOME"], "/tmp/bootstrap-test-gnupg")
                self.assertEqual(os.environ["DEBBUILDER_SUITE"], "test suite")
                self.assertEqual(bootstrap.call_args.kwargs["repository_root"], runtime.repository_root)

    def test_symlinked_environment_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.write_text("GNUPGHOME=/tmp/test\n")
            link = Path(temporary) / "debbuilder.env"
            link.symlink_to(source)
            with mock.patch.object(bootstrap_local, "ENVIRONMENT_FILE", link), self.assertRaises(OSError):
                bootstrap_local.main(["--environment-file", str(link)])


if __name__ == "__main__":
    unittest.main()
