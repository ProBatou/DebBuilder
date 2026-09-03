import json
import tempfile
import unittest
from pathlib import Path

from debbuilder.project_detection import DetectionError, detect_project


class ProjectDetectionTests(unittest.TestCase):
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
        self.assertEqual(result["proposed_commands"], ["corepack enable", "pnpm install --frozen-lockfile", "pnpm build"])
        self.assertEqual((result["package_manager_spec"], result["node_version"]), ("pnpm@10.24.0", "^22.19.0"))

    def test_rust_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text("[package]\n")
            result = detect_project(root)
        self.assertEqual(result["project_type"], "rust")
        self.assertEqual(result["build_dependencies"], ["cargo", "rustc"])
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
