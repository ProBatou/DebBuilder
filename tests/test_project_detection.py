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

    def test_python_detection_reports_markers_and_deterministic_proposals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[build-system]\n")
            (root / "requirements.txt").write_text("requests\n")
            result = detect_project(root)
        self.assertEqual(result["project_type"], "python")
        self.assertEqual(result["detected_files"], ["pyproject.toml", "requirements.txt"])
        self.assertEqual(result["build_dependencies"], ["python3", "python3-pip", "python3-build"])
        self.assertEqual(result["proposed_commands"], ["python3 -m pip install -r requirements.txt", "python3 -m build"])

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
            (root / "package.json").write_text("{}")
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
