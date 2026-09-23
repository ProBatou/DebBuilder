import copy
import unittest

from debbuilder.recipe_schema import RecipeDocumentError, recipe_document_for_storage, validate_recipe_metadata
from debbuilder.runtime_apt_repositories import MAX_ARMORED_KEY_LENGTH


PUBLIC_KEY = """-----BEGIN PGP PUBLIC KEY BLOCK-----

dGhpcy1pcy1zdHJ1Y3R1cmFsbHktcHVibGljLWtleS1kYXRh
-----END PGP PUBLIC KEY BLOCK-----
"""


def repository(**changes):
    value = {
        "id": "vendor-runtime",
        "uri": "https://APT.Example.invalid/runtime/path/",
        "suite": "nodistro",
        "components": ["main", "vendor-runtime"],
        "signing_key": {"armored": PUBLIC_KEY},
    }
    value.update(changes)
    return value


def recipe(repositories):
    return {
        "schema_version": 5,
        "name": "runtime-repository-demo",
        "runtime_apt_repositories": repositories,
        "package": {"name": "runtime-repository-demo"},
    }


class RuntimeAptRepositoryTests(unittest.TestCase):
    def test_declaration_round_trips_without_reordering_semantic_lists(self):
        second = repository(
            id="second",
            uri="https://packages.example.invalid/repo",
            components=["extras", "main"],
        )
        stored = recipe_document_for_storage(recipe([repository(), second]))
        self.assertEqual(stored["schema_version"], 5)
        self.assertEqual([row["id"] for row in stored["runtime_apt_repositories"]], ["vendor-runtime", "second"])
        self.assertEqual(stored["runtime_apt_repositories"][1]["components"], ["extras", "main"])
        self.assertEqual(stored["runtime_apt_repositories"][0]["uri"], "https://apt.example.invalid/runtime/path/")
        self.assertEqual(recipe_document_for_storage(stored), stored)

    def test_key_line_endings_are_canonicalized_without_changing_payload(self):
        declaration = repository(signing_key={"armored": PUBLIC_KEY.replace("\n", "\r\n")})
        stored = validate_recipe_metadata(recipe([declaration]))
        armored = stored["runtime_apt_repositories"][0]["signing_key"]["armored"]
        self.assertEqual(armored, PUBLIC_KEY)
        self.assertTrue(armored.endswith("\n"))
        self.assertNotIn("\r", armored)

    def test_repository_id_is_safe_bounded_and_unique(self):
        invalid = ["", "UPPER", "with space", "../escape", "slash/name", "a" * 65]
        for identifier in invalid:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([repository(id=identifier)]))
        with self.assertRaisesRegex(ValueError, "duplicate runtime APT repository id"):
            validate_recipe_metadata(recipe([repository(), repository()]))

    def test_uri_requires_credential_free_query_free_absolute_https(self):
        invalid = [
            "http://apt.example.invalid/repo",
            "apt.example.invalid/repo",
            "https://user:password@apt.example.invalid/repo",
            "https://user@apt.example.invalid/repo",
            "https://apt.example.invalid/repo?token=secret",
            "https://apt.example.invalid/repo#fragment",
            "https://apt.example.invalid/repo path",
            "https://apt.example.invalid:bad/repo",
            "https://apt.example.invalid:/repo",
            "https://exa$mple.invalid/repo",
            "https://example..invalid/repo",
        ]
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([repository(uri=uri)]))

    def test_suite_and_components_are_safe_nonempty_bounded_and_unique(self):
        for suite in ("", " ", "../stable", "stable/main", "stable\nnext", "a" * 129):
            with self.subTest(suite=suite), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([repository(suite=suite)]))
        for components in ([], ["main", "main"], ["main contrib"], ["../main"], ["main/control\n"]):
            with self.subTest(components=components), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([repository(components=components)]))

    def test_signing_key_validation_is_structural_not_cryptographic(self):
        stored = validate_recipe_metadata(recipe([repository()]))
        self.assertEqual(stored["runtime_apt_repositories"][0]["signing_key"]["armored"], PUBLIC_KEY)
        invalid = [
            "",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n" + "YWFhYWFhYWFhYWFhYWFhYWFh" + "\n-----END PGP PRIVATE KEY BLOCK-----\n",
            "-----BEGIN CERTIFICATE-----\n\nYWFhYWFhYWFhYWFhYWFhYWFh\n-----END CERTIFICATE-----\n",
            "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nnot base64!\n-----END PGP PUBLIC KEY BLOCK-----\n",
            PUBLIC_KEY + PUBLIC_KEY,
            "x" * (MAX_ARMORED_KEY_LENGTH + 1),
        ]
        for armored in invalid:
            with self.subTest(armored=armored[:60]), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([repository(signing_key={"armored": armored})]))

    def test_only_canonical_repository_fields_are_accepted(self):
        forbidden = {
            "setup_script": "curl example.invalid | sh",
            "trusted": True,
            "key_url": "https://example.invalid/key.asc",
            "options": {"allow-insecure": True},
            "architectures": ["arm64"],
        }
        for field, value in forbidden.items():
            declaration = repository()
            declaration[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_recipe_metadata(recipe([declaration]))

    def test_storage_rejects_unknown_nested_signing_key_fields(self):
        declaration = repository(signing_key={"armored": PUBLIC_KEY, "url": "https://example.invalid/key"})
        with self.assertRaises(RecipeDocumentError) as raised:
            recipe_document_for_storage(recipe([declaration]))
        self.assertEqual(raised.exception.code, "invalid_recipe")

    def test_validation_is_pure(self):
        source = recipe([repository()])
        before = copy.deepcopy(source)
        validate_recipe_metadata(source)
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
