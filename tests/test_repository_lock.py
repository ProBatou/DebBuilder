import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from debbuilder.build_store import BuildStore
from debbuilder.repository_lock import (
    LOCK_FILE,
    RepositoryLockError,
    RepositoryMutationBusy,
    repository_lease,
    repository_lease_held,
)


class RepositoryLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repository"
        self.repo.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_excludes_other_threads_and_releases_after_exception(self):
        outcomes = []

        def contend():
            try:
                with repository_lease(self.repo, operation="thread contender"):
                    outcomes.append("entered")
            except RepositoryMutationBusy as exc:
                outcomes.append(exc.code)

        with self.assertRaisesRegex(RuntimeError, "expected"):
            with repository_lease(self.repo, operation="owner") as lease:
                self.assertTrue(repository_lease_held())
                self.assertEqual(lease.identity["root"], str(self.repo))
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                raise RuntimeError("expected")
        self.assertEqual(outcomes, ["repository_mutation_busy"])
        with repository_lease(self.repo, operation="after exception"):
            self.assertTrue(repository_lease_held())
        self.assertFalse(repository_lease_held())

    def test_cross_process_exclusion(self):
        code = """
import sys
from debbuilder.repository_lock import RepositoryMutationBusy, repository_lease
try:
    with repository_lease(sys.argv[1], operation='child'):
        raise SystemExit(2)
except RepositoryMutationBusy:
    raise SystemExit(0)
"""
        with repository_lease(self.repo, operation="parent"):
            child = subprocess.run(
                [sys.executable, "-c", code, str(self.repo)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_inherited_lease_survives_owner_death_until_mutation_child_exits(self):
        code = """
import subprocess, sys, time
from debbuilder.repository_lock import repository_lease
with repository_lease(sys.argv[1], operation='owner') as lease:
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'], pass_fds=lease.inherited_fds)
    print('READY', flush=True)
    time.sleep(30)
"""
        owner = subprocess.Popen(
            [sys.executable, "-c", code, str(self.repo)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(owner.stdout.readline().strip(), "READY")
        owner.kill()
        owner.wait(timeout=2)
        owner.stdout.close()
        owner.stderr.close()
        with self.assertRaises(RepositoryMutationBusy):
            with repository_lease(self.repo, operation="contender"):
                pass
        deadline = time.monotonic() + 3
        while True:
            try:
                with repository_lease(self.repo, operation="after child"):
                    break
            except RepositoryMutationBusy:
                if time.monotonic() >= deadline:
                    self.fail("inherited repository lease was not released after child exit")
                time.sleep(0.05)

    def test_rejects_symlinked_root_and_lock_file(self):
        root_link = self.base / "repository-link"
        root_link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(RepositoryLockError) as root_error:
            with repository_lease(root_link, operation="unsafe root"):
                pass
        self.assertEqual(root_error.exception.code, "repository_root_invalid")

        outside = self.base / "outside-lock"
        outside.write_text("protected")
        (self.repo / LOCK_FILE).symlink_to(outside)
        with self.assertRaises(RepositoryLockError) as lock_error:
            with repository_lease(self.repo, operation="unsafe lock"):
                pass
        self.assertEqual(lock_error.exception.code, "repository_lock_invalid")
        self.assertEqual(outside.read_text(), "protected")

    def test_identity_and_lock_are_root_scoped(self):
        other = self.base / "other"
        other.mkdir()
        with repository_lease(self.repo, operation="first") as first:
            with self.assertRaises(RepositoryLockError):
                with repository_lease(other, operation="nested"):
                    pass
            first_identity = first.identity
        with repository_lease(other, operation="other") as second:
            second_identity = second.identity
        self.assertNotEqual(
            (first_identity["device"], first_identity["inode"]),
            (second_identity["device"], second_identity["inode"]),
        )

    def test_pinned_root_does_not_follow_a_path_replacement(self):
        (self.repo / "pool").mkdir()
        (self.repo / "pool/artifact.deb").write_bytes(b"original")
        moved = self.base / "repository-moved"
        with repository_lease(self.repo, operation="pinned") as lease:
            self.repo.rename(moved)
            self.repo.mkdir()
            (self.repo / "pool").mkdir()
            (self.repo / "pool/artifact.deb").write_bytes(b"replacement")
            with lease.open_regular("pool/artifact.deb", required_prefix="pool") as (fd, _):
                self.assertEqual(os.read(fd, 32), b"original")

    def test_run_lock_cannot_be_acquired_after_repository_lock(self):
        store = BuildStore(self.base / "builds")
        recipe = {
            "name": "demo",
            "package": {"name": "demo", "architecture": "all"},
            "source": {"repository": "owner/demo"},
        }
        run = store.create(recipe, run_id="run-one")
        with repository_lease(self.repo, operation="ordering"):
            with self.assertRaisesRegex(RuntimeError, "Run lock cannot be acquired"):
                with store.locked_run(run["id"]):
                    pass


if __name__ == "__main__":
    unittest.main()
