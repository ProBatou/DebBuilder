import shutil
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debbuilder import apt_repo
from debbuilder.local_repository_bootstrap import LocalRepositoryBootstrapError, bootstrap_repository
from debbuilder.repository_lock import repository_lease
from debbuilder.runtime import native_debian_architecture


@unittest.skipUnless(all(shutil.which(name) for name in ("gpg", "gpgv", "reprepro", "dpkg")), "repository tools unavailable")
class LocalRepositoryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.data = self.root / "data"

    def bootstrap(self):
        return bootstrap_repository(repository_root=self.repo, data_root=self.data,
                                    suite="Luminous", component="main")

    def test_dot_segments_cannot_place_private_gpg_home_in_public_pool(self):
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            bootstrap_repository(
                repository_root=self.repo / "missing" / "..", data_root=self.data,
                suite="Luminous", component="main", gpg_home=self.repo / "pool" / "private-home",
            )
        self.assertEqual(raised.exception.code, "bootstrap_private_directory_unsafe")
        self.assertFalse((self.repo / "pool/private-home").exists())

    def test_fresh_and_repeated_start_preserve_key_config_and_signed_export(self):
        first = self.bootstrap()
        config = (self.repo / "conf/distributions").read_bytes()
        key = (self.repo / "repository.gpg").read_bytes()
        release = (self.repo / "dists/Luminous/InRelease").read_bytes()
        self.assertIn(f"SignWith: {first['fingerprint']}".encode(), config)
        self.assertTrue(release)
        self.assertEqual((self.data / ".gnupg").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.data / "repository-signing-fingerprint").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.repo / ".gnupg").exists())
        second = self.bootstrap()
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        self.assertEqual((self.repo / "conf/distributions").read_bytes(), config)
        self.assertEqual((self.repo / "repository.gpg").read_bytes(), key)
        self.assertEqual((self.repo / "dists/Luminous/InRelease").read_bytes(), release)

    def test_missing_public_export_is_recovered_without_changing_key(self):
        first = self.bootstrap()
        self.assertTrue((self.repo / "repository.gpg").unlink() is None)
        self.assertEqual(self.bootstrap()["fingerprint"], first["fingerprint"])
        self.assertTrue((self.repo / "repository.gpg").is_file())

    def test_missing_empty_export_is_recovered_with_existing_database(self):
        first = self.bootstrap()
        self.assertTrue(any((self.repo / "db").iterdir()))
        (self.repo / "dists/Luminous/InRelease").unlink()
        self.assertEqual(self.bootstrap()["fingerprint"], first["fingerprint"])
        self.assertTrue((self.repo / "dists/Luminous/InRelease").is_file())

    def test_repository_database_without_config_fails_before_key_generation(self):
        (self.repo / "db").mkdir(parents=True)
        (self.repo / "db/version").write_text("existing")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_repository_incompatible")
        self.assertFalse((self.repo / "conf/distributions").exists())

    def test_stale_public_export_of_same_key_is_regenerated(self):
        first = self.bootstrap()
        armored = subprocess.run(["gpg", "--batch", "--no-options", "--homedir", str(self.data / ".gnupg"),
                                  "--armor", "--export", first["fingerprint"]], check=True, capture_output=True).stdout
        (self.repo / "repository.gpg").write_bytes(armored)
        self.bootstrap()
        self.assertNotEqual((self.repo / "repository.gpg").read_bytes(), armored)

    def test_key_generated_before_config_failure_is_reused(self):
        from debbuilder import local_repository_bootstrap as bootstrap_module
        real_write = bootstrap_module._atomic_file
        def fail_config(directory_fd, name, content, mode, **kwargs):
            if name == "distributions":
                raise OSError("simulated disk write failure")
            return real_write(directory_fd, name, content, mode, **kwargs)
        with mock.patch.object(bootstrap_module, "_atomic_file", side_effect=fail_config):
            with self.assertRaises(OSError):
                self.bootstrap()
        self.assertFalse((self.repo / "conf/distributions").exists())
        first = self.bootstrap()
        self.assertEqual(self.bootstrap()["fingerprint"], first["fingerprint"])

    def test_multiple_unconfigured_secret_keys_are_not_selected(self):
        home = self.data / ".gnupg"
        home.mkdir(parents=True, mode=0o700)
        for uid in ("DebBuilder Repository <repository@debbuilder.invalid>", "Other <other@example.invalid>"):
            subprocess.run(["gpg", "--batch", "--no-options", "--homedir", str(home),
                            "--pinentry-mode", "loopback", "--passphrase", "",
                            "--quick-generate-key", uid, "rsa2048", "sign", "0"],
                           check=True, capture_output=True, timeout=60)
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_signing_key_ambiguous")
        self.assertFalse((self.repo / "conf/distributions").exists())

    def test_config_without_key_fails_closed(self):
        self.bootstrap()
        shutil.rmtree(self.data / ".gnupg")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_signing_key_invalid")

    def test_existing_config_is_not_overwritten_on_settings_mismatch(self):
        self.bootstrap()
        config = self.repo / "conf/distributions"
        original = config.read_bytes()
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            bootstrap_repository(repository_root=self.repo, data_root=self.data,
                                 suite="Different", component="main")
        self.assertEqual(raised.exception.code, "bootstrap_distribution_incompatible")
        self.assertEqual(config.read_bytes(), original)

    def test_existing_suite_alias_keeps_codename_and_config(self):
        first = self.bootstrap()
        config = self.repo / "conf/distributions"
        config.write_text(config.read_text().replace("Suite: Luminous\n", "Suite: stable\n"))
        original = config.read_bytes()
        with mock.patch.dict(os.environ, {"GNUPGHOME": str(self.data / ".gnupg")}):
            subprocess.run(["reprepro", "-b", str(self.repo), "export", "Luminous"],
                           check=True, capture_output=True, timeout=60)
        result = bootstrap_repository(repository_root=self.repo, data_root=self.data,
                                      suite="stable", component="main")
        self.assertEqual(result["fingerprint"], first["fingerprint"])
        self.assertEqual(result["codename"], "Luminous")
        self.assertEqual(config.read_bytes(), original)

    @unittest.skipUnless(os.geteuid() == 0, "alternate repository ownership requires root")
    def test_existing_operator_owned_layout_is_preserved(self):
        first = self.bootstrap()
        owned = [self.repo / "conf", self.repo / "conf/distributions", self.repo / "db", self.repo / "dists"]
        for path in owned:
            os.chown(path, 65534, 65534)
        result = self.bootstrap()
        self.assertEqual(result["fingerprint"], first["fingerprint"])
        self.assertTrue(all(path.stat().st_uid == 65534 for path in owned))

    def test_stale_signed_identity_after_config_change_fails_closed(self):
        self.bootstrap()
        config = self.repo / "conf/distributions"
        config.write_text(config.read_text().replace("Suite: Luminous\n", "Suite: stable\n"))
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            bootstrap_repository(repository_root=self.repo, data_root=self.data,
                                 suite="stable", component="main")
        self.assertEqual(raised.exception.code, "bootstrap_repository_incompatible")

    def test_signing_record_conflict_fails_closed(self):
        self.bootstrap()
        (self.data / "repository-signing-fingerprint").write_text("A" * 40 + "\n")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_signing_record_conflict")

    def test_symlinked_repository_file_fails_closed(self):
        self.bootstrap()
        key = self.repo / "repository.gpg"
        key.unlink()
        key.symlink_to(self.data / "repository-signing-fingerprint")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_file_unsafe")

    def test_writable_repository_parent_fails_before_key_generation(self):
        original = self.root.stat().st_mode & 0o777
        self.root.chmod(0o777)
        self.addCleanup(self.root.chmod, original)
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_private_directory_unsafe")
        self.assertFalse((self.repo / "conf/distributions").exists())

    def test_missing_export_with_package_state_fails_closed(self):
        self.bootstrap()
        (self.repo / "dists/Luminous/InRelease").unlink()
        (self.repo / "pool/sample").write_text("occupied")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_repository_incomplete")

    def test_missing_database_with_signed_index_fails_closed(self):
        self.bootstrap()
        shutil.rmtree(self.repo / "db")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_repository_incomplete")
        self.assertFalse((self.repo / "db").exists())

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_signed_repository_accepts_real_reprepro_publication(self):
        first = self.bootstrap()
        public_key_before = (self.repo / "repository.gpg").read_bytes()
        self._publish_test_package()
        self.assertEqual(self.bootstrap()["fingerprint"], first["fingerprint"])
        self.assertEqual((self.repo / "repository.gpg").read_bytes(), public_key_before)
        verified = subprocess.run(
            ["gpgv", "--status-fd", "1", "--keyring", str(self.repo / "repository.gpg"),
             str(self.repo / "dists/Luminous/InRelease")],
            check=True, capture_output=True, text=True,
        )
        signers = [line.split()[2] for line in verified.stdout.splitlines()
                   if line.startswith("[GNUPG:] VALIDSIG ")]
        self.assertEqual(signers, [first["fingerprint"]])
        result = subprocess.run(["reprepro", "-b", str(self.repo), "list", "Luminous"],
                                check=True, capture_output=True, text=True)
        self.assertIn("bootstrap-test 1.0-1", result.stdout)

    def _publish_test_package(self):
        package = self.root / "package"
        (package / "DEBIAN").mkdir(parents=True)
        (package / "usr/share/bootstrap-test").mkdir(parents=True)
        (package / "DEBIAN/control").write_text(
            "Package: bootstrap-test\nVersion: 1.0-1\nArchitecture: all\nSection: misc\nPriority: optional\n"
            "Maintainer: Test <test@example.invalid>\nDescription: bootstrap test\n")
        (package / "usr/share/bootstrap-test/data").write_text("payload")
        artifact = self.root / "bootstrap-test_1.0-1_all.deb"
        subprocess.run(["dpkg-deb", "--build", str(package), str(artifact)], check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"GNUPGHOME": str(self.data / ".gnupg")}):
            with repository_lease(self.repo, operation="test-publication") as lease, artifact.open("rb") as source:
                lease.pin_standard_layout()
                included = apt_repo.reprepro_include_deb(self.repo, "Luminous", artifact, lease=lease,
                                                          source_fd=source.fileno())
        self.assertEqual(included["command"]["status"], "success", included["command"].get("stderr"))

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_missing_published_pool_file_fails_closed(self):
        self.bootstrap()
        self._publish_test_package()
        pool_file = next((self.repo / "pool").rglob("*.deb"))
        pool_file.unlink()
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_command_failed")

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
    def test_corrupt_published_index_fails_closed(self):
        self.bootstrap()
        self._publish_test_package()
        index = self.repo / f"dists/Luminous/main/binary-{native_debian_architecture()}/Packages.gz"
        index.write_bytes(index.read_bytes() + b"corruption")
        with self.assertRaises(LocalRepositoryBootstrapError) as raised:
            self.bootstrap()
        self.assertEqual(raised.exception.code, "bootstrap_repository_incomplete")


if __name__ == "__main__":
    unittest.main()
