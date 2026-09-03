import os
import tempfile
import unittest
from pathlib import Path

from debbuilder.source_changes import SourceChangeError, apply_change, apply_changes


class SourceChangesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def test_replace(self):
        target = self.write("app.txt", "before old after")
        result = apply_change(self.root, {"operation":"replace","path":"app.txt","search":"old","content":"new"})
        self.assertEqual(target.read_text(), "before new after")
        self.assertEqual(result["matches"], 1)

    def test_insert_before(self):
        target = self.write("app.txt", "target")
        apply_change(self.root, {"operation":"insert_before","path":"app.txt","search":"target","content":"before\n"})
        self.assertEqual(target.read_text(), "before\ntarget")

    def test_insert_after(self):
        target = self.write("app.txt", "target")
        apply_change(self.root, {"operation":"insert_after","path":"app.txt","search":"target","content":"\nafter"})
        self.assertEqual(target.read_text(), "target\nafter")

    def test_remove(self):
        target = self.write("app.txt", "keep remove keep")
        apply_change(self.root, {"operation":"remove","path":"app.txt","search":"remove"})
        self.assertEqual(target.read_text(), "keep  keep")

    def test_create_file(self):
        apply_change(self.root, {"operation":"create_file","path":"config/new.ini","content":"enabled=true\n"})
        self.assertEqual((self.root / "config/new.ini").read_text(), "enabled=true\n")

    def test_remove_file(self):
        target = self.write("obsolete.txt", "old")
        apply_change(self.root, {"operation":"remove_file","path":"obsolete.txt"})
        self.assertFalse(target.exists())

    def test_zero_and_multiple_matches_are_explicit_errors_without_modification(self):
        target = self.write("app.txt", "same same")
        for search, code in (("missing", "source_match_not_found"), ("same", "source_match_ambiguous")):
            with self.subTest(search=search):
                with self.assertRaises(SourceChangeError) as raised:
                    apply_change(self.root, {"operation":"replace","path":"app.txt","search":search,"content":"changed"})
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(target.read_text(), "same same")

    def test_rejects_traversal_absolute_paths_and_symlink_components(self):
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("safe")
        os.symlink(Path(self.temporary.name), self.root / "link")
        changes = [
            {"operation":"create_file","path":"../outside.txt","content":"bad"},
            {"operation":"create_file","path":str(outside),"content":"bad"},
            {"operation":"replace","path":"link/outside.txt","search":"safe","content":"bad"},
        ]
        for change in changes:
            with self.subTest(path=change["path"]), self.assertRaises(SourceChangeError) as raised:
                apply_change(self.root, change)
            self.assertEqual(raised.exception.code, "unsafe_source_path")
            self.assertEqual(outside.read_text(), "safe")

    def test_create_refuses_overwrite_and_text_change_preserves_mode(self):
        target = self.write("run.sh", "old")
        target.chmod(0o755)
        with self.assertRaises(SourceChangeError) as raised:
            apply_change(self.root, {"operation":"create_file","path":"run.sh","content":"new"})
        self.assertEqual(raised.exception.code, "source_file_exists")
        apply_change(self.root, {"operation":"replace","path":"run.sh","search":"old","content":"new"})
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_apply_changes_logs_each_success_through_callback_and_stops_on_failure(self):
        self.write("one.txt", "a")
        events = []
        with self.assertRaises(SourceChangeError) as raised:
            apply_changes(self.root, [
                {"operation":"replace","path":"one.txt","search":"a","content":"b"},
                {"operation":"remove_file","path":"missing.txt"},
                {"operation":"create_file","path":"never.txt","content":"never"},
            ], on_applied=events.append)
        self.assertEqual(raised.exception.index, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual((self.root / "one.txt").read_text(), "b")
        self.assertFalse((self.root / "never.txt").exists())


if __name__ == "__main__":
    unittest.main()
