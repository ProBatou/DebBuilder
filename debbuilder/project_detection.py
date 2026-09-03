"""Deterministic, non-executing project detection for the MVP."""
from __future__ import annotations

import json
from pathlib import Path


class DetectionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _project_root(source_directory: str | Path, working_directory: str) -> tuple[Path, Path]:
    source = Path(source_directory).resolve()
    root = (source / working_directory).resolve()
    try:
        root.relative_to(source)
    except ValueError as exc:
        raise DetectionError("invalid_working_directory", "Build working directory escapes the acquired source") from exc
    if not root.is_dir():
        raise DetectionError("invalid_working_directory", "Build working directory does not exist in the acquired source")
    return source, root


def _relative(source: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(source).as_posix() for path in paths]


def _detect_node(source: Path, root: Path) -> dict | None:
    manifest = root / "package.json"
    if not manifest.is_file():
        return None
    files = [manifest]
    package_manager, lock = "npm", root / "package-lock.json"
    for candidate, manager in ((root / "pnpm-lock.yaml", "pnpm"), (root / "yarn.lock", "yarn"), (lock, "npm")):
        if candidate.is_file():
            lock, package_manager = candidate, manager
            files.append(candidate)
            break
    commands = {"pnpm": ["corepack enable", "pnpm install --frozen-lockfile"], "yarn": ["corepack enable", "yarn install --immutable"], "npm": ["npm ci" if lock.is_file() else "npm install"]}[package_manager]
    warnings = []
    data = {}
    try:
        data = json.loads(manifest.read_text(errors="strict"))
        if isinstance(data.get("scripts"), dict) and data["scripts"].get("build"):
            commands.append(f"{package_manager} {'run ' if package_manager == 'npm' else ''}build")
    except (OSError, UnicodeError, json.JSONDecodeError):
        warnings.append("package.json could not be parsed; no build script was proposed")
    package_manager_spec = data.get("packageManager", "") if isinstance(data, dict) else ""
    engines = data.get("engines", {}) if isinstance(data, dict) and isinstance(data.get("engines"), dict) else {}
    dependencies = ["nodejs", "npm"] if package_manager == "npm" else ["nodejs"]
    return {"project_type": "nodejs", "display_name": f"Node.js · {package_manager}", "detected_files": _relative(source, files), "build_dependencies": dependencies, "proposed_commands": commands, "warnings": warnings, "package_manager": package_manager, "package_manager_spec": package_manager_spec, "node_version": str(engines.get("node") or "")}


def _detect_python(source: Path, root: Path) -> dict | None:
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    files = [path for path in (pyproject, requirements) if path.is_file()]
    if not files:
        return None
    dependencies = ["python3"]
    commands = []
    if requirements.is_file():
        dependencies.append("python3-pip")
        commands.append("python3 -m pip install -r requirements.txt")
    if pyproject.is_file():
        dependencies.append("python3-build")
        commands.append("python3 -m build")
    return {"project_type": "python", "display_name": "Python", "detected_files": _relative(source, files), "build_dependencies": dependencies, "proposed_commands": commands, "warnings": []}


def _detect_rust(source: Path, root: Path) -> dict | None:
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return None
    files = [manifest]
    lock = root / "Cargo.lock"
    if lock.is_file():
        files.append(lock)
    return {"project_type": "rust", "display_name": "Rust · Cargo", "detected_files": _relative(source, files), "build_dependencies": ["cargo", "rustc"], "proposed_commands": ["cargo build --release"], "warnings": []}


def _detect_static(source: Path, root: Path) -> dict | None:
    ignored = {"README", "README.md", "README.txt", "LICENSE", "COPYING"}
    suffixes = {".sh", ".bash", ".conf", ".template", ".html", ".css"}
    markers = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name in ignored:
            continue
        if path.name in {".bashrc", ".profile"} or path.suffix.lower() in suffixes or path.name.endswith(".conf.template"):
            markers.append(path)
    if not markers:
        return None
    return {
        "project_type": "static", "display_name": "Static files · no build",
        "detected_files": _relative(source, markers), "build_dependencies": [],
        "proposed_commands": [], "warnings": [],
    }


def detect_project(source_directory: str | Path, *, working_directory: str = ".") -> dict:
    source, root = _project_root(source_directory, working_directory)
    candidates = [candidate for candidate in (_detect_node(source, root), _detect_python(source, root), _detect_rust(source, root)) if candidate]
    if not candidates:
        static = _detect_static(source, root)
        if static:
            return {**static, "working_directory": working_directory}
        raise DetectionError("project_not_detected", "No supported Node.js, Python, Rust, or static project marker was found")
    if len(candidates) > 1:
        names = [candidate["project_type"] for candidate in candidates]
        raise DetectionError("ambiguous_project", f"Multiple project types were detected: {', '.join(names)}; select a more specific working directory", details={"candidates": candidates})
    result = candidates[0]
    return {**result, "working_directory": working_directory}
