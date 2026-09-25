"""Exact, bounded, read-only reprepro inventory contract."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from debbuilder.repository_inventory import (
    MAX_ENTRIES, RepositoryInventoryError, _run_list, inventory, parse_inventory,
)
from debbuilder.repository_lock import repository_lease


CONFIG = "Origin: DebBuilder\nLabel: DebBuilder\nSuite: stable\nCodename: stable\nArchitectures: amd64 arm64\nComponents: main contrib\n"


class RepositoryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="debbuilder-inventory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "conf").mkdir()
        (self.root / "conf/distributions").write_text(CONFIG)
        with repository_lease(self.root, operation="test-setup") as lease:
            lease.pin_standard_layout()

    def read(self):
        return inventory(self.root, distribution="stable", component="main", architecture="amd64")

    def test_zero_one_multiple_versions_and_architectures_are_exact_and_sorted(self):
        self.assertEqual(parse_inventory("", codename="stable", components=["main"], architectures=["amd64"]), [])
        lines = (
            "stable|main|arm64: zed 1.0-1\n"
            "stable|main|amd64: demo 2.0-1\n"
            "stable|main|amd64: demo 1.0-1\n"
            "stable|contrib|amd64: demo 1.0-1\n"
        )
        rows = parse_inventory(lines, codename="stable", components=["main", "contrib"], architectures=["amd64", "arm64"])
        self.assertEqual([(r["name"], r["version"], r["architecture"], r["component"]) for r in rows], [
            ("demo", "1.0-1", "amd64", "contrib"), ("demo", "1.0-1", "amd64", "main"),
            ("demo", "2.0-1", "amd64", "main"), ("zed", "1.0-1", "arm64", "main"),
        ])
        with mock.patch("debbuilder.repository_inventory._run_list", return_value=lines):
            self.assertEqual(self.read()["packages"], rows)

    def test_malformed_duplicate_and_oversized_output_fail_closed(self):
        for raw in ("garbage\n", "stable|main|amd64: demo 1\ninvalid\n",
                    "other|main|amd64: demo 1\n", "stable|main|amd64: demo 1\n" * 2):
            with self.subTest(raw=raw), self.assertRaises(RepositoryInventoryError) as caught:
                parse_inventory(raw, codename="stable", components=["main"], architectures=["amd64"])
            self.assertEqual(caught.exception.code, "repository_inventory_invalid")
        with mock.patch("debbuilder.repository_inventory.MAX_ENTRIES", 1):
            with self.assertRaises(RepositoryInventoryError) as caught:
                parse_inventory("stable|main|amd64: a 1\nstable|main|amd64: b 1\n",
                                codename="stable", components=["main"], architectures=["amd64"])
            self.assertEqual(caught.exception.code, "repository_inventory_too_large")

    def test_missing_lock_tool_and_configuration_are_unavailable_without_creation(self):
        with mock.patch("debbuilder.repository_inventory.shutil.which", return_value=None):
            with self.assertRaises(RepositoryInventoryError) as caught:
                self.read()
            self.assertEqual(caught.exception.code, "repository_inventory_unavailable")
        (self.root / ".debbuilder-repository.lock").unlink()
        with self.assertRaises(RepositoryInventoryError):
            self.read()
        self.assertFalse((self.root / ".debbuilder-repository.lock").exists())

    def test_publication_lease_returns_busy_without_waiting(self):
        ready = threading.Event()
        release = threading.Event()
        def hold():
            with repository_lease(self.root, operation="publication"):
                ready.set()
                release.wait(5)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            with self.assertRaises(RepositoryInventoryError) as caught:
                self.read()
            self.assertEqual(caught.exception.code, "repository_mutation_busy")
            self.assertEqual(caught.exception.status, 409)
        finally:
            release.set()
            worker.join(5)

    def test_runner_timeout_and_output_bound(self):
        with mock.patch("debbuilder.repository_inventory.TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(RepositoryInventoryError) as caught:
                _run_list([sys.executable, "-c", "import time; time.sleep(5)"], workspace=self.root, pass_fds=())
            self.assertEqual(caught.exception.code, "repository_inventory_timeout")
        with mock.patch("debbuilder.repository_inventory.MAX_OUTPUT_BYTES", 100):
            with self.assertRaises(RepositoryInventoryError) as caught:
                _run_list([sys.executable, "-c", "print('x' * 1000)"], workspace=self.root, pass_fds=())
            self.assertEqual(caught.exception.code, "repository_inventory_too_large")

    @unittest.skipUnless(shutil.which("reprepro") and shutil.which("dpkg-deb"), "Debian repository tools unavailable")
    def test_real_disposable_reprepro_inventory(self):
        self.assertEqual(self.read()["packages"], [])
        package = self.root / "package"
        (package / "DEBIAN").mkdir(parents=True)
        (package / "usr/share/demo").mkdir(parents=True)
        (package / "DEBIAN/control").write_text(
            "Package: demo\nVersion: 2.0-1\nArchitecture: all\nSection: utils\n"
            "Priority: optional\nMaintainer: Demo <demo@example.test>\nDescription: demo\n"
        )
        (package / "usr/share/demo/data").write_text("fixture")
        artifact = self.root / "demo_2.0-1_all.deb"
        subprocess.run(["dpkg-deb", "--build", str(package), str(artifact)], check=True, capture_output=True)
        subprocess.run(["reprepro", "--basedir", str(self.root), "includedeb", "stable", str(artifact)],
                       check=True, capture_output=True)
        rows = self.read()["packages"]
        self.assertEqual([(r["name"], r["version"], r["architecture"], r["component"]) for r in rows], [
            ("demo", "2.0-1", "amd64", "main"), ("demo", "2.0-1", "arm64", "main"),
        ])


if __name__ == "__main__":
    unittest.main()
