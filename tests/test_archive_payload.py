import unittest

from debbuilder.archive_payload import (
    normalize_archive_payload,
    parse_archive_path,
    payload_selects,
    selector_matches,
)


class ArchivePayloadTests(unittest.TestCase):
    def test_canonical_file_and_directory_selectors(self):
        file_path = parse_archive_path("static/js/app.js")
        directory = parse_archive_path("static/")
        self.assertEqual(file_path.parts, ("static", "js", "app.js"))
        self.assertFalse(file_path.is_directory)
        self.assertEqual(directory.parts, ("static",))
        self.assertTrue(directory.is_directory)

    def test_duplicate_and_redundant_includes_are_removed(self):
        payload = normalize_archive_payload({
            "mode": "paths",
            "include": ["server.py", "static/js/app.js", "static/", "server.py"],
            "exclude": [],
        })
        self.assertEqual(payload["include"], ["server.py", "static/"])

    def test_duplicate_and_redundant_exclusions_are_removed(self):
        payload = normalize_archive_payload({
            "mode": "paths",
            "include": ["debbuilder/"],
            "exclude": ["tests/unit/", "tests/", "README.md", "README.md"],
        })
        self.assertEqual(payload["exclude"], ["README.md", "tests/"])

    def test_include_and_nested_exclusion_remain_independent(self):
        payload = normalize_archive_payload({
            "mode": "paths",
            "include": ["debbuilder/"],
            "exclude": ["debbuilder/dev/"],
        })
        self.assertEqual(payload["include"], ["debbuilder/"])
        self.assertEqual(payload["exclude"], ["debbuilder/dev/"])
        self.assertTrue(payload_selects(payload, "debbuilder/app.py"))
        self.assertFalse(payload_selects(payload, "debbuilder/dev/server.py"))

    def test_entire_archive_discards_include_and_applies_exclusions(self):
        payload = normalize_archive_payload({
            "mode": "entire_archive",
            "include": ["ignored/"],
            "exclude": ["tests/"],
        })
        self.assertEqual(payload["include"], [])
        self.assertTrue(payload_selects(payload, "server.py"))
        self.assertFalse(payload_selects(payload, "tests/test_app.py"))

    def test_paths_mode_requires_an_include(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            normalize_archive_payload({"mode": "paths", "include": [], "exclude": []})

    def test_unsafe_and_noncanonical_paths_are_rejected(self):
        cases = [
            "", "/path", "../path", "foo/../bar", "./foo", "foo/./bar",
            "foo//bar", "foo\\bar", "C:/foo", "foo//", "foo\x00bar",
            "foo\nbar", "foo\x7fbar",
        ]
        for value in cases:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                parse_archive_path(value)

    def test_directory_matching_uses_components_not_string_prefixes(self):
        self.assertTrue(selector_matches("foo/", "foo/bar.txt"))
        self.assertTrue(selector_matches("foo/", "foo/"))
        self.assertFalse(selector_matches("foo/", "foo"))
        self.assertFalse(selector_matches("foo/", "foobar/file.txt"))
        self.assertFalse(selector_matches("foo", "foo/"))

    def test_file_and_directory_selectors_for_same_path_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            normalize_archive_payload({"mode": "paths", "include": ["foo", "foo/"], "exclude": []})


if __name__ == "__main__":
    unittest.main()
