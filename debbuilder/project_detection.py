"""Deterministic, non-executing project detection."""
from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
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


def _unique(values: list) -> list:
    return list(dict.fromkeys(value for value in values if value))


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


def _dependency_names(values) -> list[str]:
    if not isinstance(values, list):
        return []
    names = []
    for value in values:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", str(value))
        if match:
            names.append(match.group(1))
    return _unique(names)


def _backend_name(module: str) -> str:
    module = str(module or "")
    for prefix, name in (
        ("setuptools", "setuptools"), ("hatchling", "hatchling"),
        ("poetry.core", "poetry-core"), ("flit_core", "flit"),
        ("uv_build", "uv"),
    ):
        if module.startswith(prefix):
            return name
    return module or "unspecified"


def _main_entrypoint(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(errors="strict"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare) or len(node.test.ops) != 1 or not isinstance(node.test.ops[0], ast.Eq):
            continue
        values = [node.test.left, *node.test.comparators]
        has_name = any(isinstance(value, ast.Name) and value.id == "__name__" for value in values)
        has_main = any(isinstance(value, ast.Constant) and value.value == "__main__" for value in values)
        if has_name and has_main:
            return True
    return False


def _python_package_directories(root: Path) -> list[Path]:
    ignored = {"build", "dist", "docs", "examples", "test", "tests", "venv", ".venv", "__pycache__"}
    packages = []
    for parent in (root, root / "src"):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and child.name not in ignored and not child.name.startswith(".") and (child / "__init__.py").is_file():
                packages.append(child)
    return packages


def _setup_py_metadata(path: Path, warnings: list[str]) -> dict:
    result = {"python_requirement": "", "dependencies": [], "entry_points": []}
    try:
        tree = ast.parse(path.read_text(errors="strict"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        warnings.append("setup.py could not be parsed safely; it was not executed")
        return result
    call = next((node for node in ast.walk(tree) if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "setup") or (isinstance(node.func, ast.Attribute) and node.func.attr == "setup"))), None)
    if not call:
        return result
    values = {}
    for keyword in call.keywords:
        if keyword.arg in {"python_requires", "install_requires", "entry_points"}:
            try:
                values[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                pass
    result["python_requirement"] = str(values.get("python_requires") or "")
    result["dependencies"] = _dependency_names(values.get("install_requires"))
    entry_points = values.get("entry_points")
    if isinstance(entry_points, dict) and isinstance(entry_points.get("console_scripts"), list):
        result["entry_points"] = [str(row) for row in entry_points["console_scripts"]]
    return result


def _setup_cfg_metadata(path: Path, warnings: list[str]) -> dict:
    result = {"python_requirement": "", "dependencies": [], "entry_points": []}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(errors="strict"))
    except (OSError, UnicodeError, configparser.Error):
        warnings.append("setup.cfg could not be parsed")
        return result
    result["python_requirement"] = parser.get("options", "python_requires", fallback="").strip()
    result["dependencies"] = _dependency_names(parser.get("options", "install_requires", fallback="").splitlines())
    result["entry_points"] = [row.strip() for row in parser.get("options.entry_points", "console_scripts", fallback="").splitlines() if row.strip()]
    return result


def _requirements(root: Path) -> list[Path]:
    paths = []
    default = root / "requirements.txt"
    if default.is_file():
        paths.append(default)
    paths.extend(sorted(path for path in root.glob("requirements-*.txt") if path.is_file()))
    directory = root / "requirements"
    if directory.is_dir():
        paths.extend(sorted(path for path in directory.glob("*.txt") if path.is_file()))
    return sorted(_unique(paths))


def _detect_python(source: Path, root: Path) -> dict | None:
    pyproject, setup_py, setup_cfg = root / "pyproject.toml", root / "setup.py", root / "setup.cfg"
    pipfile, poetry_lock, uv_lock = root / "Pipfile", root / "poetry.lock", root / "uv.lock"
    requirement_files = _requirements(root)
    metadata_files = [path for path in (pyproject, setup_py, setup_cfg, *requirement_files, pipfile, poetry_lock, uv_lock) if path.is_file()]
    packages = _python_package_directories(root)
    entrypoint_files = [path for path in sorted(root.glob("*.py")) if path.is_file() and _main_entrypoint(path)]
    source_application = bool(packages and entrypoint_files)
    if not metadata_files and not source_application:
        return None

    warnings: list[str] = []
    python_requirement = ""
    backend_module = ""
    backend_requires: list[str] = []
    declared_dependencies: list[str] = []
    entry_points: list[str] = [f"{path.name} (__main__)" for path in entrypoint_files]
    optional_dependencies: list[str] = []
    dependency_sources: list[Path] = []
    lockfiles = [path for path in (poetry_lock, uv_lock) if path.is_file()]
    pyproject_data = {}

    if pyproject.is_file():
        try:
            pyproject_data = tomllib.loads(pyproject.read_text(errors="strict"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            warnings.append("pyproject.toml could not be parsed")
        build_system = pyproject_data.get("build-system") if isinstance(pyproject_data.get("build-system"), dict) else {}
        project = pyproject_data.get("project") if isinstance(pyproject_data.get("project"), dict) else {}
        tools = pyproject_data.get("tool") if isinstance(pyproject_data.get("tool"), dict) else {}
        poetry = tools.get("poetry") if isinstance(tools.get("poetry"), dict) else {}
        backend_module = str(build_system.get("build-backend") or "")
        backend_requires = [str(row) for row in build_system.get("requires", []) if isinstance(row, str)] if isinstance(build_system.get("requires"), list) else []
        python_requirement = str(project.get("requires-python") or "")
        declared_dependencies.extend(_dependency_names(project.get("dependencies")))
        for section in ("scripts", "gui-scripts"):
            scripts = project.get(section) if isinstance(project.get(section), dict) else {}
            entry_points.extend(f"{name} = {target}" for name, target in scripts.items() if isinstance(target, str))
        groups = project.get("entry-points") if isinstance(project.get("entry-points"), dict) else {}
        for group, values in groups.items():
            if isinstance(values, dict):
                entry_points.extend(f"{group}: {name} = {target}" for name, target in values.items() if isinstance(target, str))
        optional = project.get("optional-dependencies") if isinstance(project.get("optional-dependencies"), dict) else {}
        for values in optional.values():
            optional_dependencies.extend(_dependency_names(values))
        poetry_dependencies = poetry.get("dependencies") if isinstance(poetry.get("dependencies"), dict) else {}
        if not python_requirement and poetry_dependencies.get("python"):
            python_requirement = str(poetry_dependencies["python"])
        declared_dependencies.extend(str(name) for name in poetry_dependencies if name != "python")
        poetry_scripts = poetry.get("scripts") if isinstance(poetry.get("scripts"), dict) else {}
        entry_points.extend(f"{name} = {target}" for name, target in poetry_scripts.items() if isinstance(target, str))
        if project.get("dependencies") or poetry_dependencies or isinstance(tools.get("uv"), dict):
            dependency_sources.append(pyproject)

    setup_metadata = _setup_py_metadata(setup_py, warnings) if setup_py.is_file() else {}
    cfg_metadata = _setup_cfg_metadata(setup_cfg, warnings) if setup_cfg.is_file() else {}
    for metadata in (setup_metadata, cfg_metadata):
        if not python_requirement:
            python_requirement = str(metadata.get("python_requirement") or "")
        declared_dependencies.extend(metadata.get("dependencies") or [])
        entry_points.extend(metadata.get("entry_points") or [])
    if (setup_py.is_file() or setup_cfg.is_file()) and not backend_module:
        backend_module = "setuptools.build_meta:__legacy__"

    dependency_sources.extend(requirement_files)
    if pipfile.is_file():
        dependency_sources.append(pipfile)
        try:
            pipfile_data = tomllib.loads(pipfile.read_text(errors="strict"))
            requires = pipfile_data.get("requires") if isinstance(pipfile_data.get("requires"), dict) else {}
            if not python_requirement:
                version = requires.get("python_full_version") or requires.get("python_version") or ""
                python_requirement = f"=={version}" if version else ""
            for section in ("packages", "dev-packages"):
                values = pipfile_data.get(section)
                if isinstance(values, dict):
                    declared_dependencies.extend(str(name) for name in values)
            scripts = pipfile_data.get("scripts") if isinstance(pipfile_data.get("scripts"), dict) else {}
            entry_points.extend(f"{name} = {target}" for name, target in scripts.items() if isinstance(target, str))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            warnings.append("Pipfile could not be parsed")
    dependency_sources.extend(lockfiles)

    tools = pyproject_data.get("tool") if isinstance(pyproject_data.get("tool"), dict) else {}
    buildable = bool(setup_py.is_file() or setup_cfg.is_file() or (pyproject.is_file() and pyproject_data.get("build-system")))
    if buildable:
        build_mode = "wheel"
        commands = ["python3 -m build"]
        build_dependencies = ["python3", "python3-build"]
    else:
        build_mode = "source"
        commands = []
        build_dependencies = []

    packaging_tool = ""
    if poetry_lock.is_file() or isinstance(tools.get("poetry"), dict):
        packaging_tool = "poetry"
    elif uv_lock.is_file() or isinstance(tools.get("uv"), dict):
        packaging_tool = "uv"
    elif pipfile.is_file():
        packaging_tool = "pipenv"
    elif requirement_files:
        packaging_tool = "requirements"
    elif backend_module:
        packaging_tool = _backend_name(backend_module)

    display_name = "Python · PEP 517 package" if buildable else "Python · source application · no build"

    marker_files = list(metadata_files)
    if source_application:
        marker_files.extend(entrypoint_files)
        marker_files.extend(package / "__init__.py" for package in packages)
    return {
        "project_type": "python", "display_name": display_name, "detected_files": _relative(source, _unique(marker_files)),
        "build_dependencies": build_dependencies, "proposed_commands": commands, "warnings": warnings,
        "build_mode": build_mode, "build_backend": _backend_name(backend_module), "build_backend_module": backend_module,
        "build_backend_requires": backend_requires, "python_requirement": python_requirement,
        "dependency_sources": _relative(source, _unique(dependency_sources)), "lockfile": _relative(source, lockfiles)[0] if lockfiles else "",
        "lockfiles": _relative(source, lockfiles), "declared_dependencies": _unique(declared_dependencies),
        "optional_dependencies": _unique(optional_dependencies), "entry_point_hints": _unique(entry_points),
        "package_directories": _relative(source, packages), "packaging_tool": packaging_tool,
        "python_runtime_requirement": python_requirement,
        "python_runtime_dependencies": _unique(declared_dependencies),
        "python_packaging_requirements": backend_requires,
        "debian_runtime_dependencies": [],
        "build_description": "Build a Python distribution with the declared PEP 517 backend" if buildable else "No build command is required; package the selected source files directly",
    }


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
