import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from debbuilder.apt_repository_trust import (
    RepositoryTrustError,
    apt_configuration,
    deb822_source,
    inspect_public_key,
)


def generate_key(home: Path, uid: str, *, usage="sign") -> tuple[str, str]:
    home.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(
        ["gpg", "--batch", "--homedir", str(home), "--passphrase", "", "--quick-generate-key", uid, "rsa2048", usage, "0"],
        check=True, capture_output=True,
    )
    listing = subprocess.run(
        ["gpg", "--batch", "--homedir", str(home), "--with-colons", "--list-keys", uid],
        check=True, capture_output=True, text=True,
    ).stdout
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    armored = subprocess.run(
        ["gpg", "--batch", "--homedir", str(home), "--armor", "--export", fingerprint],
        check=True, capture_output=True, text=True,
    ).stdout
    return fingerprint, armored


@unittest.skipUnless(shutil.which("gpg"), "gpg unavailable")
class RepositoryTrustTests(unittest.TestCase):
    def test_single_public_certificate_extracts_crypto_identity_and_dearmors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, armored = generate_key(root / "source", "Repository One <repo-one@example.invalid>")
            keyring = root / "out/repository.gpg"
            result = inspect_public_key(armored, workspace=root, output_keyring=keyring)
            self.assertEqual(result["primary_fingerprint"], fingerprint)
            self.assertIn(fingerprint, result["signing_fingerprints"])
            self.assertEqual(result["content_sha256"], hashlib.sha256(armored.encode("ascii")).hexdigest())
            self.assertTrue(keyring.is_file())
            self.assertEqual(keyring.stat().st_mode & 0o777, 0o400)

    def test_signing_subkey_is_reported_but_unrelated_primary_certs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary, _armored = generate_key(root / "source", "Repository Subkey <repo-sub@example.invalid>", usage="cert")
            subprocess.run(
                ["gpg", "--batch", "--homedir", str(root / "source"), "--passphrase", "", "--quick-add-key", primary, "rsa2048", "sign", "0"],
                check=True, capture_output=True,
            )
            armored = subprocess.run(
                ["gpg", "--batch", "--homedir", str(root / "source"), "--armor", "--export", primary],
                check=True, capture_output=True, text=True,
            ).stdout
            inspected = inspect_public_key(armored, workspace=root, output_keyring=root / "subkey.gpg")
            self.assertEqual(inspected["primary_fingerprint"], primary)
            self.assertEqual(len(inspected["signing_fingerprints"]), 1)
            self.assertNotEqual(inspected["signing_fingerprints"][0], primary)

            second, _ = generate_key(root / "source", "Repository Two <repo-two@example.invalid>")
            combined = subprocess.run(
                ["gpg", "--batch", "--homedir", str(root / "source"), "--armor", "--export", primary, second],
                check=True, capture_output=True, text=True,
            ).stdout
            with self.assertRaises(RepositoryTrustError) as raised:
                inspect_public_key(combined, workspace=root, output_keyring=root / "combined.gpg")
            self.assertEqual(raised.exception.code, "repository_signing_certificate_ambiguous")

    def test_private_and_malformed_material_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint, _ = generate_key(root / "source", "Repository Secret <repo-secret@example.invalid>")
            private = subprocess.run(
                ["gpg", "--batch", "--homedir", str(root / "source"), "--armor", "--export-secret-keys", fingerprint],
                check=True, capture_output=True, text=True,
            ).stdout
            with self.assertRaises(RepositoryTrustError) as raised:
                inspect_public_key(private, workspace=root, output_keyring=root / "private.gpg")
            self.assertEqual(raised.exception.code, "repository_private_key_rejected")
            with self.assertRaises(RepositoryTrustError):
                inspect_public_key("not a key\n", workspace=root, output_keyring=root / "bad.gpg")

    def test_deb822_and_no_redirect_config_are_deterministic_and_isolated(self):
        repository = {
            "id": "vendor", "uri": "https://repo.example.invalid/vendor", "suite": "bookworm",
            "components": ["main", "extras"],
            "signing_key": {"armored": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB\n-----END PGP PUBLIC KEY BLOCK-----\n"},
        }
        expected = (
            "Types: deb\nURIs: https://repo.example.invalid/vendor\nSuites: bookworm\n"
            "Components: main extras\nSigned-By: /etc/apt/keyrings/debbuilder-attempt-vendor.gpg\n"
        )
        self.assertEqual(deb822_source(repository, signed_by="/etc/apt/keyrings/debbuilder-attempt-vendor.gpg"), expected)
        config = apt_configuration()
        self.assertIn('Acquire::http::AllowRedirect "false";', config)
        self.assertIn('Acquire::https::AllowRedirect "false";', config)
        self.assertIn('Acquire::AllowInsecureRepositories "false";', config)
        self.assertIn('Acquire::AllowDowngradeToInsecureRepositories "false";', config)
        self.assertIn('APT::Get::AllowUnauthenticated "false";', config)
        self.assertNotIn("trusted=yes", config)
        self.assertNotIn("apt-key", config)
