import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from debbuilder.project_detection import MAX_AUXILIARY_MANIFEST_BYTES, DetectionError, auxiliary_npm_install_inputs, detect_project


class ProjectDetectionTests(unittest.TestCase):
    def write_npm_ci_inputs(self, root: Path, *, lockfile_version=3, package_updates=None, lock_updates=None):
        frontend = root / "frontend"
        frontend.mkdir(exist_ok=True)
        package = self.auxiliary_manifest(dependencies={"demo-dependency": "1.0.0"})
        package.update(package_updates or {})
        lock = {
            "name": "demo", "lockfileVersion": lockfile_version, "requires": True,
            "packages": {
                "": {"dependencies": {"demo-dependency": "1.0.0"}},
                "node_modules/demo-dependency": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/demo-dependency/-/demo-dependency-1.0.0.tgz", "integrity": "sha512-example"},
            },
        }
        lock.update(lock_updates or {})
        (frontend / "package.json").write_text(json.dumps(package))
        (frontend / "package-lock.json").write_text(json.dumps(lock))

    def test_auxiliary_npm_inputs_accept_v2_v3_and_seal_files_and_absence(self):
        for version in (2, 3):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_npm_ci_inputs(root, lockfile_version=version)
                seal = auxiliary_npm_install_inputs(root, "frontend")
                self.assertEqual(seal["lockfile_version"], version)
                self.assertEqual([row["path"] for row in seal["files"]], ["frontend/package.json", "frontend/package-lock.json"])
                self.assertTrue(all(len(row["sha256"]) == 64 for row in seal["files"]))
                self.assertEqual(seal["absent"], ["frontend/.npmrc", "frontend/npm-shrinkwrap.json"])

    def test_auxiliary_npm_inputs_reject_unsupported_graph_and_detect_mutation(self):
        cases = (
            (1, None, None, "auxiliary_node_lockfile_version_unsupported"),
            (3, {"workspaces": ["packages/*"]}, None, "auxiliary_node_workspaces_unsupported"),
            (3, {"dependencies": {"local": "file:../local"}}, None, "auxiliary_node_local_dependency_unsupported"),
            (3, {"overrides": {"local": "link:../local"}}, None, "auxiliary_node_local_dependency_unsupported"),
            (3, None, {"packages": {"": {}, "node_modules/pkg": {"version": "file:../pkg", "link": True}}}, "auxiliary_node_local_dependency_unsupported"),
            (3, None, {"packages": {"": {}, "node_modules/pkg": {"version": "1.0.0"}}}, "auxiliary_node_lockfile_integrity_missing"),
        )
        for version, package_updates, lock_updates, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_npm_ci_inputs(root, lockfile_version=version, package_updates=package_updates, lock_updates=lock_updates)
                with self.assertRaises(DetectionError) as raised:
                    auxiliary_npm_install_inputs(root, "frontend")
                self.assertEqual(raised.exception.code, code)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_npm_ci_inputs(root)
            seal = auxiliary_npm_install_inputs(root, "frontend")
            (root / "frontend/.npmrc").write_text("registry=https://example.invalid")
            with self.assertRaises(DetectionError) as raised:
                auxiliary_npm_install_inputs(root, "frontend", expected=seal)
            self.assertEqual(raised.exception.code, "auxiliary_node_inputs_changed")

    def test_auxiliary_npm_inputs_reject_git_file_references_even_with_integrity(self):
        package_fields = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "overrides")
        for field in package_fields:
            with self.subTest(location=f"package.json.{field}"), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_npm_ci_inputs(root, package_updates={field: {"local": "Git+File:///tmp/local-package"}})
                with self.assertRaises(DetectionError) as raised:
                    auxiliary_npm_install_inputs(root, "frontend")
                self.assertEqual(raised.exception.code, "auxiliary_node_local_dependency_unsupported")
                self.assertEqual(raised.exception.details["field"], f"package.json.{field}.local")

        for lockfile_version in (2, 3):
            for field in ("version", "resolved"):
                with self.subTest(lockfile_version=lockfile_version, field=field), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    entry = {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
                        "integrity": "sha512-artificial-integrity-must-not-authorize-local-git",
                        field: "git+file:///tmp/local-package#0123456789abcdef",
                    }
                    self.write_npm_ci_inputs(root, lockfile_version=lockfile_version, lock_updates={
                        "packages": {"": {}, "node_modules/pkg": entry},
                    })
                    with self.assertRaises(DetectionError) as raised:
                        auxiliary_npm_install_inputs(root, "frontend")
                    self.assertEqual(raised.exception.code, "auxiliary_node_local_dependency_unsupported")
                    self.assertEqual(raised.exception.details["field"], f"package-lock.json.packages.node_modules/pkg.{field}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_npm_ci_inputs(root, lockfile_version=2, lock_updates={
                "dependencies": {
                    "local": {
                        "version": "git+file:///tmp/local-package#0123456789abcdef",
                        "resolved": "git+file:///tmp/local-package#0123456789abcdef",
                        "integrity": "sha512-artificial-integrity-must-not-authorize-local-git",
                    },
                },
            })
            with self.assertRaises(DetectionError) as raised:
                auxiliary_npm_install_inputs(root, "frontend")
            self.assertEqual(raised.exception.code, "auxiliary_node_local_dependency_unsupported")
            self.assertEqual(raised.exception.details["field"], "package-lock.json.dependencies.local.version")

    def test_auxiliary_npm_inputs_preserve_registry_and_real_frontend_lockfiles(self):
        for lockfile_version in (2, 3):
            with self.subTest(lockfile_version=lockfile_version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_npm_ci_inputs(root, lockfile_version=lockfile_version)
                self.assertEqual(
                    auxiliary_npm_install_inputs(root, "frontend")["lockfile_version"],
                    lockfile_version,
                )

        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(auxiliary_npm_install_inputs(repository, "frontend")["lockfile_version"], 3)

    def test_primary_node_project_is_not_subject_to_auxiliary_npm_reference_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({
                "scripts": {"build": "vite build"},
                "dependencies": {"local": "git+file:///tmp/local-package"},
            }))
            result = detect_project(root)
        self.assertEqual(result["project_type"], "nodejs")
        self.assertEqual(result["proposed_commands"][0], "npm install")

    def test_auxiliary_npm_inputs_require_lock_and_reject_project_npm_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "frontend").mkdir()
            (root / "frontend/package.json").write_text(json.dumps(self.auxiliary_manifest()))
            with self.assertRaises(DetectionError) as raised:
                auxiliary_npm_install_inputs(root, "frontend")
            self.assertEqual(raised.exception.code, "auxiliary_node_install_input_missing")
        for name in (".npmrc", "npm-shrinkwrap.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.write_npm_ci_inputs(root)
                (root / "frontend" / name).write_text("{}")
                with self.assertRaises(DetectionError) as raised:
                    auxiliary_npm_install_inputs(root, "frontend")
                self.assertEqual(raised.exception.code, "auxiliary_node_install_configuration_unsupported")

    @staticmethod
    def python_project(root: Path) -> None:
        (root / "pyproject.toml").write_text('[project]\nname="demo"\n')

    @staticmethod
    def auxiliary_manifest(**overrides) -> dict:
        document = {
            "packageManager": "npm@11.20.0",
            "engines": {"node": ">=24 <25", "npm": ">=11 <12"},
        }
        document.update(overrides)
        return document

    def test_python_with_explicit_auxiliary_node_project_keeps_primary_facts_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            (root / "frontend/package.json").write_text(json.dumps(self.auxiliary_manifest()))
            baseline = detect_project(root)
            result = detect_project(root, node_project_directory="frontend")
        self.assertEqual(result["project_type"], "python")
        self.assertEqual(result["detected_files"], baseline["detected_files"])
        self.assertEqual(result["build_tools"], baseline["build_tools"])
        self.assertEqual(result["tool_version_requirements"], baseline["tool_version_requirements"])
        self.assertEqual(result["auxiliary_node_project"], {
            "project_directory": "frontend",
            "manifest": "frontend/package.json",
            "package_manager": "npm",
            "package_manager_spec": "npm@11.20.0",
            "package_manager_version": "11.20.0",
            "node_version": ">=24 <25",
            "npm_version": ">=11 <12",
            "tool_version_requirements": {"node": ">=24 <25", "npm": "11.20.0"},
            "detected_files": ["frontend/package.json"],
            "diagnostics": [],
        })

    def test_current_debbuilder_checkout_reports_python_and_real_frontend_requirements(self):
        root = Path(__file__).resolve().parents[1]
        result = detect_project(root, node_project_directory="frontend")
        self.assertEqual(result["project_type"], "python")
        auxiliary = result["auxiliary_node_project"]
        self.assertEqual(auxiliary["node_version"], ">=24 <25")
        self.assertEqual(auxiliary["npm_version"], ">=11 <12")
        self.assertEqual(auxiliary["package_manager_spec"], "npm@11.20.0")
        self.assertEqual(auxiliary["manifest"], "frontend/package.json")

    def test_omitted_auxiliary_project_preserves_python_and_node_results_exactly(self):
        fixtures = (
            ("python", lambda root: self.python_project(root)),
            ("nodejs", lambda root: (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))),
        )
        for expected, prepare in fixtures:
            with self.subTest(project_type=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prepare(root)
                baseline = detect_project(root)
                self.assertEqual(detect_project(root, node_project_directory=None), baseline)
                self.assertEqual(baseline["project_type"], expected)
                self.assertNotIn("auxiliary_node_project", baseline)

    def test_auxiliary_node_manifest_requires_complete_valid_requirements(self):
        cases = (
            ({"packageManager": "npm@11.20.0"}, "auxiliary_node_requirements_missing"),
            ({"packageManager": "npm@11.20.0", "engines": {"npm": ">=11 <12"}}, "auxiliary_node_requirement_missing"),
            ({"packageManager": "npm@11.20.0", "engines": {"node": ">=24 <25"}}, "auxiliary_node_requirement_missing"),
            ({"engines": {"node": ">=24 <25", "npm": ">=11 <12"}}, "auxiliary_node_package_manager_missing"),
            ({"packageManager": "npm@11", "engines": {"node": ">=24 <25", "npm": ">=11 <12"}}, "auxiliary_node_package_manager_invalid"),
            ({"packageManager": "pnpm@10.24.0", "engines": {"node": ">=24 <25", "npm": ">=11 <12"}}, "auxiliary_node_package_manager_invalid"),
            ({"packageManager": "npm@11.20.0", "engines": {"node": "latest", "npm": ">=11 <12"}}, "auxiliary_node_requirement_invalid"),
        )
        for manifest, code in cases:
            with self.subTest(code=code, manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.python_project(root)
                (root / "frontend").mkdir()
                (root / "frontend/package.json").write_text(json.dumps(manifest))
                with self.assertRaises(DetectionError) as raised:
                    detect_project(root, node_project_directory="frontend")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.details["manifest"], "frontend/package.json")

    def test_auxiliary_exact_npm_version_must_satisfy_npm_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            manifest = self.auxiliary_manifest(packageManager="npm@10.9.4")
            (root / "frontend/package.json").write_text(json.dumps(manifest))
            with self.assertRaises(DetectionError) as raised:
                detect_project(root, node_project_directory="frontend")
        self.assertEqual(raised.exception.code, "auxiliary_node_package_manager_mismatch")
        self.assertEqual(raised.exception.details["package_manager_version"], "10.9.4")
        self.assertEqual(raised.exception.details["requested_range"], ">=11 <12")

    def test_auxiliary_version_ranges_validate_every_disjunction_branch(self):
        valid = self.auxiliary_manifest(
            engines={"node": ">=24 <25 || 26.x", "npm": ">=11 <12 || 12.x"},
        )
        invalid = (
            (self.auxiliary_manifest(engines={"node": "latest", "npm": ">=11 <12"}), "node"),
            (self.auxiliary_manifest(engines={"node": ">=0 || latest", "npm": ">=11 <12"}), "node"),
            (self.auxiliary_manifest(engines={"node": "latest || >=0", "npm": ">=11 <12"}), "node"),
            (self.auxiliary_manifest(engines={"node": ">=24 <25", "npm": "latest"}), "npm"),
            (self.auxiliary_manifest(engines={"node": ">=24 <25", "npm": ">=0 || latest"}), "npm"),
            (self.auxiliary_manifest(engines={"node": ">=24 <25", "npm": "latest || >=0"}), "npm"),
            (self.auxiliary_manifest(engines={"node": ">=24 <25", "npm": "<1 || latest"}), "npm"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            manifest = root / "frontend/package.json"
            manifest.write_text(json.dumps(valid))
            result = detect_project(root, node_project_directory="frontend")
            self.assertEqual(result["auxiliary_node_project"]["node_version"], ">=24 <25 || 26.x")
            self.assertEqual(result["auxiliary_node_project"]["npm_version"], ">=11 <12 || 12.x")

            for document, requirement in invalid:
                with self.subTest(requirement=requirement, document=document):
                    manifest.write_text(json.dumps(document))
                    with self.assertRaises(DetectionError) as raised:
                        detect_project(root, node_project_directory="frontend")
                    self.assertEqual(raised.exception.code, "auxiliary_node_requirement_invalid")
                    self.assertEqual(raised.exception.details["requirement"], requirement)

    def test_auxiliary_package_manager_requires_a_canonical_exact_version(self):
        versions = ("npm@01.2.3", "npm@1.02.3", "npm@1.2.03")
        for package_manager in versions:
            with self.subTest(package_manager=package_manager), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.python_project(root)
                (root / "frontend").mkdir()
                document = self.auxiliary_manifest(packageManager=package_manager)
                (root / "frontend/package.json").write_text(json.dumps(document))
                with self.assertRaises(DetectionError) as raised:
                    detect_project(root, node_project_directory="frontend")
                self.assertEqual(raised.exception.code, "auxiliary_node_package_manager_invalid")

        valid = (
            ("npm@0.0.0", ">=0"),
            ("npm@1.2.3", ">=1 <2"),
            ("npm@11.20.0", ">=11 <12"),
        )
        for package_manager, npm_range in valid:
            with self.subTest(package_manager=package_manager), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.python_project(root)
                (root / "frontend").mkdir()
                document = self.auxiliary_manifest(
                    packageManager=package_manager,
                    engines={"node": ">=24 <25", "npm": npm_range},
                )
                (root / "frontend/package.json").write_text(json.dumps(document))
                result = detect_project(root, node_project_directory="frontend")
            version = package_manager.removeprefix("npm@")
            self.assertEqual(result["auxiliary_node_project"]["package_manager_spec"], package_manager)
            self.assertEqual(result["auxiliary_node_project"]["package_manager_version"], version)

    def test_auxiliary_path_missing_traversal_and_directory_file_fail_structurally(self):
        cases = (
            ("missing", "auxiliary_node_directory_missing"),
            ("../outside", "auxiliary_node_project_invalid_path"),
            ("frontend", "auxiliary_node_directory_invalid"),
        )
        for directory, code in cases:
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.python_project(root)
                if directory == "frontend":
                    (root / "frontend").write_text("not a directory")
                with self.assertRaises(DetectionError) as raised:
                    detect_project(root, node_project_directory=directory)
                self.assertEqual(raised.exception.code, code)

    def test_auxiliary_manifest_must_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            with self.assertRaises(DetectionError) as raised:
                detect_project(root, node_project_directory="frontend")
        self.assertEqual(raised.exception.code, "auxiliary_node_manifest_missing")
        self.assertEqual(raised.exception.details["manifest"], "frontend/package.json")

    def test_auxiliary_directory_and_manifest_symlinks_are_never_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            (outside / "package.json").write_text(json.dumps(self.auxiliary_manifest()))

            directory_case = base / "directory-case"
            directory_case.mkdir()
            self.python_project(directory_case)
            (directory_case / "frontend").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(DetectionError) as directory_error:
                detect_project(directory_case, node_project_directory="frontend")
            self.assertEqual(directory_error.exception.code, "auxiliary_node_directory_symlink")

            manifest_case = base / "manifest-case"
            manifest_case.mkdir()
            self.python_project(manifest_case)
            (manifest_case / "frontend").mkdir()
            (manifest_case / "frontend/package.json").symlink_to(outside / "package.json")
            with self.assertRaises(DetectionError) as manifest_error:
                detect_project(manifest_case, node_project_directory="frontend")
            self.assertEqual(manifest_error.exception.code, "auxiliary_node_manifest_symlink")

    def test_auxiliary_non_encodable_path_always_fails_structurally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            with self.assertRaises(DetectionError) as raised:
                detect_project(root, node_project_directory="front\ud800end")
            unicode_directory = "interface-équipe"
            (root / unicode_directory).mkdir()
            (root / unicode_directory / "package.json").write_text(json.dumps(self.auxiliary_manifest()))
            result = detect_project(root, node_project_directory=unicode_directory)
        self.assertEqual(raised.exception.code, "auxiliary_node_project_invalid_path")
        self.assertEqual(raised.exception.details["field"], "build.node_project_directory")
        self.assertEqual(result["auxiliary_node_project"]["project_directory"], unicode_directory)

    def test_auxiliary_manifest_replacement_by_fifo_is_nonblocking_and_structured(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            manifest = root / "frontend/package.json"
            manifest.write_text(json.dumps(self.auxiliary_manifest()))
            real_stat, real_open = os.stat, os.open
            replaced = False

            def replace_after_stat(path, *args, **kwargs):
                nonlocal replaced
                result = real_stat(path, *args, **kwargs)
                if path == "package.json" and kwargs.get("follow_symlinks") is False and not replaced:
                    manifest.unlink()
                    os.mkfifo(manifest)
                    replaced = True
                return result

            def guarded_open(path, flags, *args, **kwargs):
                if path == "package.json":
                    self.assertTrue(flags & os.O_NONBLOCK)
                return real_open(path, flags, *args, **kwargs)

            with patch.object(os, "stat", side_effect=replace_after_stat), \
                    patch.object(os, "open", side_effect=guarded_open), \
                    self.assertRaises(DetectionError) as raised:
                detect_project(root, node_project_directory="frontend")
        self.assertTrue(replaced)
        self.assertEqual(raised.exception.code, "auxiliary_node_manifest_invalid")

    def test_auxiliary_node_project_is_rejected_for_every_non_python_primary(self):
        fixtures = (
            ("nodejs", lambda root: (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))),
            ("rust", lambda root: (root / "Cargo.toml").write_text("[package]\n")),
            ("static", lambda root: (root / ".bashrc").write_text("alias ll='ls -la'\n")),
        )
        for expected, prepare in fixtures:
            with self.subTest(project_type=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prepare(root)
                with self.assertRaises(DetectionError) as raised:
                    detect_project(root, node_project_directory="frontend")
                self.assertEqual(raised.exception.code, "auxiliary_node_project_requires_python")
                self.assertEqual(raised.exception.details["project_type"], expected)

    def test_auxiliary_manifest_malformed_oversized_and_special_entries_fail_closed(self):
        cases = (
            (lambda path: path.write_text("{"), "auxiliary_node_manifest_invalid"),
            (lambda path: path.write_bytes(b" " * (MAX_AUXILIARY_MANIFEST_BYTES + 1)), "auxiliary_node_manifest_too_large"),
            (lambda path: path.mkdir(), "auxiliary_node_manifest_invalid"),
            (lambda path: os.mkfifo(path), "auxiliary_node_manifest_invalid"),
        )
        for prepare, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.python_project(root)
                (root / "frontend").mkdir()
                prepare(root / "frontend/package.json")
                with self.assertRaises(DetectionError) as raised:
                    detect_project(root, node_project_directory="frontend")
                self.assertEqual(raised.exception.code, code)

    def test_auxiliary_detection_never_executes_source_or_host_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.python_project(root)
            (root / "frontend").mkdir()
            (root / "frontend/package.json").write_text(json.dumps(self.auxiliary_manifest()))
            (root / "frontend/postinstall.py").write_text("raise RuntimeError('must not run')\n")
            with patch("subprocess.run", side_effect=AssertionError("must not execute")), \
                    patch.object(os, "system", side_effect=AssertionError("must not execute")):
                result = detect_project(root, node_project_directory="frontend")
        self.assertEqual(result["auxiliary_node_project"]["package_manager_version"], "11.20.0")

    def test_debbuilder_fixture_declares_node_24_and_npm_11(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_bytes((repository / "package.json").read_bytes())
            (root / "package-lock.json").write_bytes((repository / "package-lock.json").read_bytes())
            result = detect_project(root)
        self.assertEqual(result["project_type"], "nodejs")
        self.assertEqual(result["tool_version_requirements"], {"node": ">=24 <25", "npm": ">=11 <12"})

    def test_node_detection_uses_lock_and_build_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
            (root / "package-lock.json").write_text("{}")
            result = detect_project(root)
        self.assertEqual(result["project_type"], "nodejs")
        self.assertEqual(result["build_dependencies"], ["nodejs", "npm"])
        self.assertEqual(result["proposed_commands"], ["npm ci", "npm run build"])

    def test_modern_pyproject_metadata_and_backend_are_parsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[build-system]\nrequires = ["hatchling>=1.20"]\nbuild-backend = "hatchling.build"\n'
                '[project]\nrequires-python = ">=3.11"\ndependencies = ["httpx>=0.27", "rich"]\n'
                '[project.scripts]\ndemo = "demo.cli:main"\n'
                '[project.gui-scripts]\ndemo-gui = "demo.gui:main"\n'
                '[project.entry-points."demo.plugins"]\njson = "demo.json:Plugin"\n'
                '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
            )
            result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertEqual(result["build_backend"], "hatchling")
        self.assertEqual(result["build_backend_module"], "hatchling.build")
        self.assertEqual(result["build_backend_requires"], ["hatchling>=1.20"])
        self.assertEqual(result["python_requirement"], ">=3.11")
        self.assertEqual(result["declared_dependencies"], ["httpx", "rich"])
        self.assertEqual(result["entry_point_hints"], ["demo = demo.cli:main", "demo-gui = demo.gui:main", "demo.plugins: json = demo.json:Plugin"])
        self.assertEqual(result["optional_dependencies"], ["pytest"])
        self.assertEqual(result["dependency_sources"], ["pyproject.toml"])
        self.assertEqual(result["build_dependencies"], ["python3", "python3-build"])
        self.assertEqual(result["proposed_commands"], ["python3 -m build"])

    def test_legacy_setup_py_is_parsed_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "setup.py").write_text(
                "from setuptools import setup\n"
                "setup(python_requires='>=3.9', install_requires=['requests>=2'], "
                "entry_points={'console_scripts':['demo=demo:main']})\n"
            )
            result = detect_project(root)
        self.assertEqual(result["build_backend"], "setuptools")
        self.assertEqual(result["python_requirement"], ">=3.9")
        self.assertEqual(result["declared_dependencies"], ["requests"])
        self.assertEqual(result["entry_point_hints"], ["demo=demo:main"])
        self.assertEqual(result["proposed_commands"], ["python3 -m build"])

    def test_setup_cfg_metadata_is_parsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "setup.cfg").write_text(
                "[metadata]\nname = demo\n[options]\npython_requires = >=3.10\n"
                "install_requires =\n  click>=8\n  rich\n"
                "[options.entry_points]\nconsole_scripts =\n  demo = demo.cli:main\n"
            )
            result = detect_project(root)
        self.assertEqual(result["build_backend"], "setuptools")
        self.assertEqual(result["python_requirement"], ">=3.10")
        self.assertEqual(result["declared_dependencies"], ["click", "rich"])
        self.assertEqual(result["entry_point_hints"], ["demo = demo.cli:main"])

    def test_requirements_variants_are_runtime_metadata_not_build_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("requests\n")
            (root / "requirements-dev.txt").write_text("pytest\n")
            (root / "requirements").mkdir()
            (root / "requirements/base.txt").write_text("urllib3\n")
            result = detect_project(root)
        self.assertEqual(result["build_mode"], "source")
        self.assertEqual(result["dependency_sources"], ["requirements/base.txt", "requirements-dev.txt", "requirements.txt"])
        self.assertEqual(result["build_dependencies"], [])
        self.assertEqual(result["proposed_commands"], [])
        self.assertEqual(result["packaging_tool"], "requirements")

    def test_poetry_and_uv_metadata_and_locks_are_reported(self):
        cases = [
            (
                '[build-system]\nrequires=["poetry-core"]\nbuild-backend="poetry.core.masonry.api"\n'
                '[tool.poetry]\nname="demo"\n[tool.poetry.dependencies]\npython="^3.11"\nhttpx="*"\n'
                '[tool.poetry.scripts]\ndemo="demo:main"\n',
                "poetry.lock", "poetry-core", "^3.11", ["httpx"],
            ),
            (
                '[build-system]\nrequires=["uv_build"]\nbuild-backend="uv_build"\n'
                '[project]\nname="demo"\nrequires-python=">=3.12"\ndependencies=["anyio"]\n[tool.uv]\npackage=true\n',
                "uv.lock", "uv", ">=3.12", ["anyio"],
            ),
        ]
        for manifest, lock, backend, requirement, dependencies in cases:
            with self.subTest(lock=lock), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "pyproject.toml").write_text(manifest)
                (root / lock).write_text("")
                result = detect_project(root)
                self.assertEqual(result["build_backend"], backend)
                self.assertEqual(result["python_requirement"], requirement)
                self.assertEqual(result["declared_dependencies"], dependencies)
                self.assertEqual(result["lockfile"], lock)
                self.assertIn(lock, result["dependency_sources"])

    def test_pipfile_metadata_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Pipfile").write_text('[requires]\npython_version="3.11"\n[packages]\nflask="*"\n[scripts]\nserve="python app.py"\n')
            result = detect_project(root)
        self.assertEqual(result["python_requirement"], "==3.11")
        self.assertEqual(result["declared_dependencies"], ["flask"])
        self.assertEqual(result["entry_point_hints"], ["serve = python app.py"])
        self.assertEqual(result["dependency_sources"], ["Pipfile"])

    def test_source_application_requires_package_and_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo").mkdir()
            (root / "demo/__init__.py").write_text("")
            (root / "server.py").write_text('if __name__ == "__main__":\n    print("ok")\n')
            result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertEqual(result["build_mode"], "source")
        self.assertEqual(result["detected_files"], ["server.py", "demo/__init__.py"])
        self.assertEqual(result["entry_point_hints"], ["server.py (__main__)"])
        self.assertEqual(result["proposed_commands"], [])
        self.assertEqual(result["build_dependencies"], [])
        self.assertIn("no build", result["display_name"])
        self.assertIn("No build command is required", result["build_description"])

    def test_python_application_prefers_itself_over_weak_node_tooling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo").mkdir()
            (root / "demo/__init__.py").write_text("")
            (root / "server.py").write_text('if __name__ == "__main__":\n    print("ok")\n')
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "node tests.js"}}))
            result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertNotIn("nodejs", result["build_dependencies"])
        self.assertNotIn("npm", result["build_dependencies"])
        self.assertFalse(any(command.startswith("npm") for command in result["proposed_commands"]))

    def test_python_application_and_node_build_script_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo").mkdir()
            (root / "demo/__init__.py").write_text("")
            (root / "server.py").write_text('if __name__ == "__main__":\n    print("ok")\n')
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "ambiguous_project")
        self.assertEqual([row["project_type"] for row in raised.exception.details["candidates"]], ["nodejs", "python"])

    def test_python_application_and_node_start_script_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo").mkdir()
            (root / "demo/__init__.py").write_text("")
            (root / "server.py").write_text('if __name__ == "__main__":\n    print("ok")\n')
            (root / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "ambiguous_project")
        self.assertEqual([row["project_type"] for row in raised.exception.details["candidates"]], ["nodejs", "python"])

    def test_weak_node_project_remains_node_when_it_is_the_only_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "node tests.js"}}))
            result = detect_project(root)
        self.assertEqual(result["project_type"], "nodejs")
        self.assertFalse(result["strong_application"])

    def test_private_node_manifest_alone_is_not_a_strong_application_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"private": True}))
            result = detect_project(root)
        self.assertEqual(result["project_type"], "nodejs")
        self.assertFalse(result["strong_application"])

    def test_current_debbuilder_checkout_is_detected_as_python_not_node_tooling(self):
        root = Path(__file__).resolve().parents[1]
        result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertNotIn("nodejs", result["build_dependencies"])
        self.assertNotIn("npm", result["build_dependencies"])
        self.assertFalse(any(command.startswith("npm") for command in result["proposed_commands"]))

    def test_project_metadata_without_build_system_does_not_imply_a_wheel_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text('[project]\nname="demo"\nversion="1.0"\nrequires-python=">=3.11"\n')
            result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertEqual(result["build_mode"], "source")
        self.assertEqual(result["build_dependencies"], [])
        self.assertEqual(result["proposed_commands"], [])

    def test_requirements_marker_names_are_deliberate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirementsprod.txt").write_text("requests\n")
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "project_not_detected")

    def test_isolated_python_script_is_not_a_strong_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "helper.py").write_text('if __name__ == "__main__":\n    print("helper")\n')
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "project_not_detected")

    def test_rust_with_auxiliary_python_script_remains_rust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\n")
            (root / "helper.py").write_text('if __name__ == "__main__":\n    print("helper")\n')
            result = detect_project(root)
        self.assertEqual(result["project_type"], "rust")

    def test_rust_workspace_with_auxiliary_package_manifest_remains_rust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text('[workspace]\nmembers=["server"]\n')
            (root / "package.json").write_text(json.dumps({"private": True, "scripts": {"lint": "eslint ."}}))
            (root / "server").mkdir()
            (root / "server/Cargo.toml").write_text('[package]\nname="server"\nversion="1"\n')
            result = detect_project(root)
        self.assertEqual(result["project_type"], "rust")

    def test_rust_and_strong_node_application_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\n")
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "ambiguous_project")
        self.assertEqual([row["project_type"] for row in raised.exception.details["candidates"]], ["nodejs", "rust"])

    def test_strong_python_and_rust_markers_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\n")
            (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
            with self.assertRaises(DetectionError) as raised:
                detect_project(root)
        self.assertEqual(raised.exception.code, "ambiguous_project")
        self.assertEqual([row["project_type"] for row in raised.exception.details["candidates"]], ["python", "rust"])

    def test_pnpm_detection_respects_upstream_toolchain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"packageManager": "pnpm@10.24.0", "engines": {"node": "^22.19.0"}, "scripts": {"build": "next build"}}))
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
            result = detect_project(root)
        self.assertEqual(result["build_dependencies"], ["nodejs"])
        self.assertEqual(result["proposed_commands"], ["pnpm install --frozen-lockfile", "pnpm build"])
        self.assertEqual((result["package_manager_spec"], result["node_version"]), ("pnpm@10.24.0", "^22.19.0"))

    def test_rust_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\n")
            result = detect_project(root)
        self.assertEqual(result["project_type"], "rust")
        self.assertEqual(result["build_dependencies"], ["cargo", "rustc"])
        self.assertEqual(result["build_tools"], ["cargo", "rustc"])
        self.assertEqual(result["system_build_dependencies"], [])
        self.assertEqual(result["proposed_commands"], ["cargo build --release"])

    def test_rust_application_lockfile_enables_locked_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text('[package]\nname="demo"\nversion="1.2.3"\nrust-version="1.85"\ndefault-run="demo"\n[features]\ndefault=["sqlite"]\n[[bin]]\nname="demo"\npath="src/server.rs"\n')
            (root / "Cargo.lock").write_text("version = 4\n")
            result = detect_project(root)
        self.assertEqual(result["proposed_commands"], ["cargo build --release --locked"])
        self.assertEqual(result["package_name"], "demo")
        self.assertEqual(result["package_version"], "1.2.3")
        self.assertEqual(result["rust_version"], "1.85")
        self.assertEqual(result["default_run"], "demo")
        self.assertEqual(result["default_features"], ["sqlite"])
        self.assertEqual(result["binary_targets"], [{"name": "demo", "path": "src/server.rs"}])
        self.assertEqual(result["suggested_output_paths"], ["target/release/demo"])

    def test_rust_workspace_selects_its_only_binary_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/*"]\ndefault-members=["crates/server"]\n')
            (root / "Cargo.lock").write_text("version = 4\n")
            for member, manifest in (
                ("lib", '[package]\nname="support"\nversion="1.0.0"\n'),
                ("server", '[package]\nname="mail-server"\nversion="1.0.0"\n[[bin]]\nname="maild"\npath="src/main.rs"\n'),
            ):
                path = root / "crates" / member
                path.mkdir(parents=True)
                (path / "Cargo.toml").write_text(manifest)
            result = detect_project(root)
        self.assertEqual(result["workspace_members"], ["crates/*"])
        self.assertEqual(result["workspace_default_members"], ["crates/server"])
        self.assertEqual(result["selected_package"], "mail-server")
        self.assertEqual(result["proposed_commands"], ["cargo build --release --locked --package mail-server"])
        self.assertEqual(result["suggested_output_paths"], ["target/release/maild"])

    def test_rust_toolchain_and_cargo_config_are_parsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\nname='demo'\nversion='1'\n")
            (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel="1.85.1"\nprofile="minimal"\ncomponents=["rustfmt"]\ntargets=["x86_64-unknown-linux-gnu"]\n')
            (root / ".cargo").mkdir()
            (root / ".cargo/config.toml").write_text('[build]\ntarget="x86_64-unknown-linux-gnu"\ntarget-dir="out"\n')
            result = detect_project(root)
        self.assertEqual(result["toolchain"], {"channel": "1.85.1", "profile": "minimal", "components": ["rustfmt"], "targets": ["x86_64-unknown-linux-gnu"], "source": "rust-toolchain.toml"})
        self.assertEqual(result["cargo_config"]["build"]["target-dir"], "out")
        self.assertIn("toolchain 1.85.1", result["display_name"])
        self.assertEqual(result["detected_files"], ["Cargo.toml", "rust-toolchain.toml", ".cargo/config.toml"])

    def test_rust_lockfile_does_not_force_locked_build_for_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text('[package]\nname="library"\nversion="1"\n')
            (root / "Cargo.lock").write_text("version = 4\n")
            result = detect_project(root)
        self.assertEqual(result["proposed_commands"], ["cargo build --release"])
        self.assertFalse(result["locked"])

    def test_static_detection_has_no_commands_or_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".bashrc").write_text("alias ll='ls -la'\n")
            (root / "README.md").write_text("docs")
            result = detect_project(root)
        self.assertEqual(result["project_type"], "static")
        self.assertEqual(result["detected_files"], [".bashrc"])
        self.assertEqual(result["build_dependencies"], [])
        self.assertEqual(result["proposed_commands"], [])

    def test_detection_honors_working_directory_without_executing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "frontend").mkdir()
            (root / "frontend/package.json").write_text("{}")
            (root / "danger.py").write_text("raise RuntimeError('must not run')")
            result = detect_project(root, working_directory="frontend")
        self.assertEqual(result["detected_files"], ["frontend/package.json"])
        self.assertEqual(result["working_directory"], "frontend")

    def test_no_marker_and_multiple_project_types_are_explicit_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DetectionError) as missing:
                detect_project(temporary)
            self.assertEqual(missing.exception.code, "project_not_detected")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
            (root / "Cargo.toml").write_text("[package]\n")
            with self.assertRaises(DetectionError) as ambiguous:
                detect_project(root)
            self.assertEqual(ambiguous.exception.code, "ambiguous_project")
            self.assertEqual(len(ambiguous.exception.details["candidates"]), 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("documentation alone is not a deployable static marker")
            with self.assertRaises(DetectionError) as unknown:
                detect_project(root)
            self.assertEqual(unknown.exception.code, "project_not_detected")


if __name__ == "__main__":
    unittest.main()
