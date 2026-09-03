"""Versioned Recipe model, compatibility migration, and validation."""
from __future__ import annotations

import copy
import re

SCHEMA_VERSION = 1
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.+-]+$")
SAFE_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
SAFE_ARCH = {"all", "amd64", "arm64", "armhf"}
STANDARD_STEP_TYPES = frozenset()
SUPPORTED_STEP_TYPES = STANDARD_STEP_TYPES
SOURCE_CHANGE_TYPES = {"replace", "insert_before", "insert_after", "remove", "create_file", "remove_file"}
OUTPUT_MODES = {"path", "paths", "source"}
ARTIFACT_MODES = {"source_build", "upstream_deb"}
CONFIG_POLICIES = {"dpkg_conffile", "replace", "create_if_missing"}
SERVICE_TYPES = {"simple", "exec", "forking", "oneshot", "notify", "dbus"}
RESTART_POLICIES = {"", "no", "always", "on-success", "on-failure", "on-abnormal", "on-abort", "on-watchdog"}


def require_safe_name(value: str, what: str = "name") -> str:
    if not value or not SAFE_NAME.fullmatch(value):
        raise ValueError(f"unsafe {what}")
    return value


def _dict(value, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    return copy.deepcopy(value)


def _list(value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    return copy.deepcopy(value)


def _string_list(value, what: str) -> list[str]:
    rows = _list(value, what)
    if any(not isinstance(row, str) or not row.strip() for row in rows):
        raise ValueError(f"{what} must contain non-empty strings")
    return [row.strip() for row in rows]


def _environment(value, what: str) -> dict[str, str]:
    rows = _dict(value, what)
    if any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in rows.items()):
        raise ValueError(f"{what} must contain string keys and values")
    return rows


def _config_files(value) -> list[str | dict]:
    rows = _list(value, "install.config_files")
    normalized = []
    for row in rows:
        if isinstance(row, str) and row.strip():
            normalized.append(row.strip())
        elif isinstance(row, dict) and isinstance(row.get("source"), str) and isinstance(row.get("destination"), str):
            normalized.append({"source": row["source"].strip(), "destination": row["destination"].strip()})
        else:
            raise ValueError("install.config_files must contain paths or source/destination mappings")
    return normalized


def normalize_steps(workflow: dict) -> list[dict]:
    """Preserve legacy step payloads without promoting them to executable steps."""
    steps = workflow.get("steps") or []
    if not isinstance(steps, list):
        raise ValueError("workflow.steps must be a list")
    normalized = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        normalized.append(copy.deepcopy(step))
    return normalized


def normalize_recipe(workflow: dict, *, compatibility_aliases: bool = True) -> dict:
    """Return a canonical Recipe v1, migrating the former flat metadata shape."""
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    version = workflow.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported recipe schema version: {version}")
    package_in = _dict(workflow.get("package"), "package")
    source_in = _dict(workflow.get("source"), "source")
    build_in = _dict(workflow.get("build"), "build")
    install_in = _dict(workflow.get("install"), "install")
    service_in = _dict(workflow.get("service"), "service")
    artifact_in = _dict(workflow.get("artifact"), "artifact")
    version_in = _dict(source_in.get("version"), "source.version")
    output_in = _dict(build_in.get("output"), "build.output")
    owner_in = _dict(install_in.get("owner"), "install.owner")
    content_in = _dict(install_in.get("content"), "install.content")
    scripts_in = _dict(install_in.get("maintainer_scripts"), "install.maintainer_scripts")
    name = str(workflow.get("name") or "recipe")
    legacy_step_package = next((str(step.get("package")) for step in normalize_steps(workflow) if step.get("package")), "")
    package_name = str(package_in.get("name") or workflow.get("package_name") or legacy_step_package or name).lower()
    repository = str(source_in.get("repository") or workflow.get("github_repository") or "").strip()
    tracking = str(source_in.get("tracking") or workflow.get("version_tracking") or "latest_release")
    version_source = str(version_in.get("source") or workflow.get("version_source") or "tag")
    expression = str(version_in.get("expression") or workflow.get("version_expression") or "")
    output_mode = str(output_in.get("mode") or ("path" if "output" in build_in else "source"))
    output_path = str(output_in.get("path") if "path" in output_in else ("dist" if output_mode == "path" else ""))
    output_paths = _string_list(output_in.get("paths"), "build.output.paths")
    content_source = str(content_in.get("source") or "build_output")
    install_destination = "" if content_source == "configured_files" else str(install_in.get("destination") or f"/opt/{package_name}")
    service_configured = bool(service_in.get("configured", service_in.get("enabled", False) or bool(service_in.get("command"))))
    service_enabled = bool(service_in.get("enabled", False))
    recipe = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "active": workflow.get("active", True),
        "package": {
            "name": package_name,
            "version_revision": str(package_in.get("version_revision") or "1"),
            "architecture": str(package_in.get("architecture") or "amd64"),
            "section": str(package_in.get("section") or "misc"),
            "priority": str(package_in.get("priority") or "optional"),
            "maintainer": str(package_in.get("maintainer") or ""),
            "description": str(package_in.get("description") or package_name),
            "runtime_dependencies": _string_list(package_in.get("runtime_dependencies"), "package.runtime_dependencies"),
        },
        "source": {
            "provider": str(source_in.get("provider") or "github"),
            "repository": repository,
            "tracking": tracking,
            "ref": str(source_in.get("ref") or ""),
            "version": {"source": version_source, "expression": expression},
        },
        "artifact": {
            "mode": str(artifact_in.get("mode") or "source_build"),
            "type": str(artifact_in.get("type") or "deb"),
            "architecture": str(artifact_in.get("architecture") or package_in.get("architecture") or "amd64"),
            "name_pattern": str(artifact_in.get("name_pattern") or ""),
            "match_package": artifact_in.get("match_package", True),
            "match_version": artifact_in.get("match_version", True),
        },
        "build": {
            "detected_project": build_in.get("detected_project"),
            "detected_files": _string_list(build_in.get("detected_files"), "build.detected_files"),
            "detected_dependencies": _string_list(build_in.get("detected_dependencies"), "build.detected_dependencies"),
            "extra_dependencies": _string_list(build_in.get("extra_dependencies"), "build.extra_dependencies"),
            "source_changes": _list(build_in.get("source_changes"), "build.source_changes"),
            "commands": _string_list(build_in.get("commands"), "build.commands"),
            "timeout": int(build_in.get("timeout") or 120),
            "environment": _environment(build_in.get("environment"), "build.environment"),
            "working_directory": str(build_in.get("working_directory") or "."),
            "output": {"mode": output_mode, "path": output_path, **({"paths": output_paths} if output_mode == "paths" else {})},
        },
        "install": {
            "destination": install_destination,
            "content": {"source": content_source, "path": str(content_in.get("path") or "")},
            "owner": {
                "user": str(owner_in.get("user") or package_name), "group": str(owner_in.get("group") or package_name),
                "create_user": owner_in.get("create_user", False), "create_group": owner_in.get("create_group", False),
            },
            "directory_mode": str(install_in.get("directory_mode") or "0755"),
            "file_mode": str(install_in.get("file_mode") or "0644"),
            "config_files": _config_files(install_in.get("config_files")),
            "config_policy": str(install_in.get("config_policy") or "dpkg_conffile"),
            "maintainer_scripts": {key: str(scripts_in.get(key) or "") for key in ("preinst", "postinst", "prerm", "postrm")},
        },
        "service": {
            "configured": service_configured, "enabled": service_enabled,
            "name": str(service_in.get("name") or "") if service_configured else "",
            "description": str(service_in.get("description") or package_name) if service_configured else "",
            "type": str(service_in.get("type") or "simple") if service_configured else "",
            "user": str(service_in.get("user") or "") if service_configured else "",
            "group": str(service_in.get("group") or "") if service_configured else "",
            "restart": str(service_in.get("restart") or "on-failure") if service_configured else "", "command": str(service_in.get("command") or ""),
            "environment_files": _string_list(service_in.get("environment_files"), "service.environment_files"),
            "environment": _environment(service_in.get("environment"), "service.environment"),
            "after": _string_list(service_in.get("after"), "service.after"), "wants": _string_list(service_in.get("wants"), "service.wants"),
            "requires": _string_list(service_in.get("requires"), "service.requires"), "restart_sec": str(service_in.get("restart_sec") or ""),
            "timeout_start_sec": str(service_in.get("timeout_start_sec") or ""), "timeout_stop_sec": str(service_in.get("timeout_stop_sec") or ""),
            "kill_signal": str(service_in.get("kill_signal") or ""),
            "exec_start_pre": _string_list(service_in.get("exec_start_pre"), "service.exec_start_pre"),
            "exec_start_post": _string_list(service_in.get("exec_start_post"), "service.exec_start_post"),
            "exec_stop": _string_list(service_in.get("exec_stop"), "service.exec_stop"),
            "standard_output": str(service_in.get("standard_output") or ""), "standard_error": str(service_in.get("standard_error") or ""),
            "working_directory": str(service_in.get("working_directory") or ""),
        },
        "steps": normalize_steps(workflow),
    }
    if not service_configured:
        recipe["service"] = {
            "configured": False, "enabled": False, "name": "", "description": "", "type": "", "user": "", "group": "",
            "restart": "", "command": "", "environment_files": [], "environment": {}, "after": [], "wants": [], "requires": [],
            "restart_sec": "", "timeout_start_sec": "", "timeout_stop_sec": "", "kill_signal": "", "exec_start_pre": [],
            "exec_start_post": [], "exec_stop": [], "standard_output": "", "standard_error": "", "working_directory": "",
        }
    if compatibility_aliases:
        recipe.update({"package_name": package_name, "github_repository": repository, "version_tracking": tracking, "version_source": version_source})
        if expression:
            recipe["version_expression"] = expression
    return recipe


def _safe_relative(value: str, what: str, *, allow_dot: bool = False) -> None:
    if not value or value.startswith("/") or ".." in value.split("/") or (value == "." and not allow_dot):
        raise ValueError(f"{what} must be a safe relative path")


def validate_recipe_metadata(workflow: dict) -> dict:
    """Normalize and validate all fields represented by Recipe v1."""
    recipe = normalize_recipe(workflow)
    require_safe_name(recipe["name"], "recipe name")
    package = recipe["package"]
    if not SAFE_PACKAGE.fullmatch(package["name"]):
        raise ValueError("package.name must be a valid Debian package name")
    if package["architecture"] not in SAFE_ARCH:
        raise ValueError("unsupported architecture")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package["section"]):
        raise ValueError("package.section must be a valid Debian section")
    if package["priority"] not in {"required", "important", "standard", "optional", "extra"}:
        raise ValueError("package.priority must be a valid Debian priority")
    if any(character in package["maintainer"] for character in "\r\n"):
        raise ValueError("package.maintainer must be a single line")
    for dependency in package["runtime_dependencies"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", dependency):
            raise ValueError("runtime dependencies must be simple Debian package names")
    if not re.fullmatch(r"[A-Za-z0-9.+~]+", package["version_revision"]):
        raise ValueError("package.version_revision is invalid")
    if not isinstance(recipe["active"], bool):
        raise ValueError("active must be a boolean")
    source = recipe["source"]
    if source["provider"] != "github":
        raise ValueError("unsupported source provider")
    if source["repository"] and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source["repository"]):
        raise ValueError("source.repository must look like owner/name")
    if source["tracking"] not in {"latest_release", "tag", "manual"}:
        raise ValueError("unsupported version tracking mode")
    if source["tracking"] in {"tag", "manual"}:
        ref = source["ref"]
        if not ref or len(ref) > 200 or any(character.isspace() for character in ref):
            raise ValueError("source.ref is required for explicit tag or manual tracking")
    if source["version"]["source"] not in {"tag", "release_name", "regex", "build"}:
        raise ValueError("unsupported GitHub version source")
    expression = source["version"]["expression"]
    if source["version"]["source"] == "regex":
        if not expression or len(expression) > 200:
            raise ValueError("a bounded version expression is required")
        re.compile(expression)
    build = recipe["build"]
    artifact = recipe["artifact"]
    if not 1 <= build["timeout"] <= 3600:
        raise ValueError("build.timeout must be between 1 and 3600 seconds")
    if artifact["mode"] not in ARTIFACT_MODES:
        raise ValueError("unsupported artifact mode")
    if artifact["type"] != "deb":
        raise ValueError("unsupported upstream artifact type")
    if artifact["architecture"] not in SAFE_ARCH:
        raise ValueError("unsupported artifact architecture")
    if len(artifact["name_pattern"]) > 200 or any(character in artifact["name_pattern"] for character in "\r\n"):
        raise ValueError("artifact.name_pattern is invalid")
    if not isinstance(artifact["match_package"], bool) or not isinstance(artifact["match_version"], bool):
        raise ValueError("artifact matching flags must be booleans")
    if build["detected_project"] not in {None, "nodejs", "python", "rust", "static"}:
        raise ValueError("unsupported configured project type")
    _safe_relative(build["working_directory"], "build.working_directory", allow_dot=True)
    if build["output"]["mode"] not in OUTPUT_MODES:
        raise ValueError("unsupported build output mode")
    if build["output"]["mode"] == "path":
        _safe_relative(build["output"]["path"], "build.output.path")
    if build["output"]["mode"] == "paths":
        if not build["output"].get("paths"):
            raise ValueError("build.output.paths must not be empty")
        for path in build["output"]["paths"]:
            _safe_relative(path, "build.output.paths entry")
    for index, change in enumerate(build["source_changes"], 1):
        if not isinstance(change, dict) or change.get("operation") not in SOURCE_CHANGE_TYPES:
            raise ValueError(f"source change {index} has an unsupported operation")
        _safe_relative(str(change.get("path") or ""), f"source change {index} path")
    install = recipe["install"]
    if install["content"]["source"] == "configured_files":
        if install["destination"]:
            raise ValueError("install.destination is not applicable to configured_files")
    elif not re.fullmatch(r"/opt/[A-Za-z0-9._+/-]+", install["destination"]) or ".." in install["destination"].split("/"):
        raise ValueError("install.destination must be below /opt")
    if install["directory_mode"] not in {"0755", "0750", "0700"} or install["file_mode"] not in {"0644", "0640", "0600"}:
        raise ValueError("unsupported install permissions")
    if install["config_policy"] not in CONFIG_POLICIES:
        raise ValueError("unsupported configuration policy")
    if install["content"]["source"] not in {"build_output", "source", "configured_files"}:
        raise ValueError("unsupported install content source")
    for configured in install["config_files"]:
        path = configured if isinstance(configured, str) else configured["destination"]
        source_path = path.lstrip("/") if isinstance(configured, str) else configured["source"]
        if not re.fullmatch(r"/[A-Za-z0-9._+/-]+", path) or ".." in path.split("/"):
            raise ValueError("configuration files must use safe absolute paths")
        _safe_relative(source_path, "configuration source")
    for key in ("create_user", "create_group"):
        if not isinstance(install["owner"][key], bool):
            raise ValueError(f"install.owner.{key} must be a boolean")
    for key in ("user", "group"):
        require_safe_name(install["owner"][key], f"install owner {key}")
    service = recipe["service"]
    if not isinstance(service["enabled"], bool):
        raise ValueError("service.enabled must be a boolean")
    if not isinstance(service["configured"], bool):
        raise ValueError("service.configured must be a boolean")
    if service["enabled"] and not service["configured"]:
        raise ValueError("an enabled service must be configured")
    if service["configured"] and (service["type"] not in SERVICE_TYPES or service["restart"] not in RESTART_POLICIES):
        raise ValueError("unsupported systemd service setting")
    if service["name"] and not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service["name"]):
        raise ValueError("service.name must end in .service")
    if service["configured"]:
        if service["working_directory"] and (not re.fullmatch(r"/[A-Za-z0-9._+/-]+", service["working_directory"]) or ".." in service["working_directory"].split("/")):
            raise ValueError("service.working_directory must be a safe absolute path")
        for key in ("user", "group"):
            if service[key]:
                require_safe_name(service[key], f"service {key}")
    return recipe


def recipe_for_storage(workflow: dict) -> dict:
    """Return canonical persisted data without deprecated flat aliases."""
    recipe = validate_recipe_metadata(workflow)
    for key in ("package_name", "github_repository", "version_tracking", "version_source", "version_expression"):
        recipe.pop(key, None)
    return recipe


def uses_automatic_pipeline(workflow: dict) -> bool:
    recipe = normalize_recipe(workflow)
    return bool(recipe["package"]["name"] and recipe["source"]["repository"])


def normalize_github_version(value: str) -> str:
    value = re.sub(r"^[vV]", "", str(value or "").strip())
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", value) and re.search(r"[a-fA-F]", value):
        raise ValueError("GitHub version is not automatically usable as a Debian version")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", value):
        raise ValueError("GitHub version is not automatically usable as a Debian version")
    return value
