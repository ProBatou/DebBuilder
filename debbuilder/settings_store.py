"""Persistent public-safe application settings and local secrets."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

_SECRET_WORDS = re.compile(r"(?i)(token|secret|password|passwd|apikey|api_key|client_secret)")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_ARCHES = {"all", "amd64", "arm64", "armhf", "i386"}


def default_settings(repo_url: str, suite: str, component: str, architecture: str = "amd64", public_url: str = "", *, security: dict | None = None, build: dict | None = None) -> dict:
    return {
        "general": {
            "app_name": "DebBuilder",
            "url": public_url,
        },
        "apt": {
            "repository": repo_url,
            "distribution": suite,
            "component": component,
            "architecture": architecture,
        },
        "github": {
            "api_url": "https://api.github.com",
        },
        "notifications": {
            "configured": False,
            "type": "none",
            "server_url": "https://ntfy.sh",
            "topic": "debbuilder",
        },
        "security": security or {"auth_mode": "none", "oidc_issuer": "", "oidc_client_id": "", "oidc_redirect_uri": ""},
        "build": build or {"allow_real_run": False, "allow_unsafe_build_command": False, "temp_dir": "/tmp/debbuilder-${WORKFLOW_NAME}"},
    }


def settings_path(data_dir: Path) -> Path:
    return data_dir / "settings.json"


def secrets_path(data_dir: Path) -> Path:
    return data_dir / "secrets.json"


def load_settings(data_dir: Path, defaults: dict) -> dict:
    result = json.loads(json.dumps(defaults))
    path = settings_path(data_dir)
    if not path.exists():
        return result
    try:
        stored = json.loads(path.read_text())
    except Exception:
        return result
    if not isinstance(stored, dict):
        return result
    for section, values in result.items():
        stored_section = stored.get(section)
        if not isinstance(stored_section, dict):
            continue
        for key, value in stored_section.items():
            if key in values and isinstance(value, type(values[key])):
                values[key] = value
            elif key in values and isinstance(values[key], str) and isinstance(value, str):
                values[key] = value
    return result


def load_secrets(data_dir: Path) -> dict:
    path = secrets_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_secret(data_dir: Path, section: str, key: str, value: str) -> None:
    value = (value or "").strip()
    if not value:
        return
    data = load_secrets(data_dir)
    data.setdefault(section, {})[key] = value
    data_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_path(data_dir)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def github_token_configured(data_dir: Path) -> bool:
    if os.environ.get("DEBBUILDER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    github = load_secrets(data_dir).get("github")
    return isinstance(github, dict) and bool(github.get("token"))


def github_token(data_dir: Path) -> str:
    env = (os.environ.get("DEBBUILDER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if env:
        return env
    github = load_secrets(data_dir).get("github")
    return str(github.get("token") or "") if isinstance(github, dict) else ""


def save_github_token(data_dir: Path, token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    if len(token) < 20 or _SECRET_WORDS.search(token[:12]):
        raise ValueError("invalid GitHub token")
    _save_secret(data_dir, "github", "token", token)


def ntfy_token(data_dir: Path) -> str:
    env = os.environ.get("DEBBUILDER_NTFY_TOKEN", "").strip()
    if env:
        return env
    notifications = load_secrets(data_dir).get("notifications")
    return str(notifications.get("token") or "") if isinstance(notifications, dict) else ""


def ntfy_token_configured(data_dir: Path) -> bool:
    return bool(ntfy_token(data_dir))


def save_ntfy_token(data_dir: Path, token: str) -> None:
    token = (token or "").strip()
    if token:
        _save_secret(data_dir, "notifications", "token", token)


def oidc_client_secret(data_dir: Path) -> str:
    env = os.environ.get("DEBBUILDER_OIDC_CLIENT_SECRET", "").strip()
    if env:
        return env
    section = load_secrets(data_dir).get("oidc")
    return str(section.get("client_secret") or "") if isinstance(section, dict) else ""


def oidc_client_secret_configured(data_dir: Path) -> bool:
    return bool(oidc_client_secret(data_dir))


def save_oidc_client_secret(data_dir: Path, value: str) -> None:
    if (value or "").strip():
        _save_secret(data_dir, "oidc", "client_secret", value)


def cookie_secret(data_dir: Path) -> str:
    env = os.environ.get("DEBBUILDER_COOKIE_SECRET", "").strip()
    if env:
        return env
    section = load_secrets(data_dir).get("session")
    value = str(section.get("cookie_secret") or "") if isinstance(section, dict) else ""
    if not value:
        value = secrets.token_urlsafe(48)
        _save_secret(data_dir, "session", "cookie_secret", value)
    return value


def _validate_url(value: str, field: str, *, allow_empty: bool = False) -> str:
    value = (value or "").strip()
    if allow_empty and not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL")
    if parsed.username or parsed.password or _SECRET_WORDS.search(parsed.netloc):
        raise ValueError(f"{field} must not contain credentials")
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        if _SECRET_WORDS.search(key) or _SECRET_WORDS.search(val):
            raise ValueError(f"{field} must not contain secret-like query values")
    return value.rstrip("/")


def _validate_repo_url(value: str) -> str:
    return _validate_url(value, "repository")


def _validate_name(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value or not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_label(value: str, field: str, *, max_len: int = 80, allow_empty: bool = False) -> str:
    value = (value or "").strip()
    if not value and allow_empty:
        return ""
    if not value or len(value) > max_len or _SECRET_WORDS.search(value):
        raise ValueError(f"invalid {field}")
    return value


def validate_settings(payload: dict, current: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings payload must be an object")
    result = json.loads(json.dumps(current))

    if "general" in payload:
        general = payload.get("general")
        if not isinstance(general, dict):
            raise ValueError("general settings must be an object")
        result["general"]["app_name"] = _validate_label(str(general.get("app_name", result["general"]["app_name"])), "app name")
        result["general"]["url"] = _validate_url(str(general.get("url", result["general"].get("url", ""))), "public url", allow_empty=True)

    if "apt" in payload:
        apt = payload.get("apt")
        if not isinstance(apt, dict):
            raise ValueError("apt settings must be an object")
        result["apt"]["repository"] = _validate_repo_url(str(apt.get("repository", result["apt"]["repository"])))
        result["apt"]["distribution"] = _validate_name(str(apt.get("distribution", result["apt"]["distribution"])), "distribution")
        result["apt"]["component"] = _validate_name(str(apt.get("component", result["apt"]["component"])), "component")
        architecture = _validate_name(str(apt.get("architecture", result["apt"]["architecture"])), "architecture")
        if architecture not in _ARCHES:
            raise ValueError("unsupported architecture")
        result["apt"]["architecture"] = architecture

    if "github" in payload:
        github = payload.get("github")
        if not isinstance(github, dict):
            raise ValueError("github settings must be an object")
        result["github"]["api_url"] = _validate_url(str(github.get("api_url", result["github"]["api_url"])), "GitHub API URL")

    if "notifications" in payload:
        notifications = payload.get("notifications")
        if not isinstance(notifications, dict):
            raise ValueError("notification settings must be an object")
        notif_type = str(notifications.get("type", result["notifications"].get("type", "none"))).strip()
        if notif_type not in {"none", "ntfy"}:
            raise ValueError("unsupported notification type")
        result["notifications"]["type"] = notif_type
        result["notifications"]["configured"] = notif_type != "none"
        if "server_url" in notifications:
            server_url = str(notifications.get("server_url") or "").strip()
            result["notifications"]["server_url"] = _validate_url(server_url, "ntfy server URL") if server_url else ""
        if "topic" in notifications:
            topic = str(notifications.get("topic") or "").strip()
            result["notifications"]["topic"] = _validate_name(topic, "ntfy topic") if topic else ""
        if notif_type == "ntfy":
            result["notifications"]["server_url"] = _validate_url(str(result["notifications"].get("server_url") or ""), "ntfy server URL")
            result["notifications"]["topic"] = _validate_name(str(result["notifications"].get("topic") or ""), "ntfy topic")

    if "security" in payload:
        security = payload.get("security")
        if not isinstance(security, dict):
            raise ValueError("security settings must be an object")
        mode = str(security.get("auth_mode", result["security"]["auth_mode"])).lower().strip()
        if mode not in {"none", "header", "oidc"}:
            raise ValueError("unsupported authentication mode")
        result["security"]["auth_mode"] = mode
        result["security"]["oidc_issuer"] = _validate_url(str(security.get("oidc_issuer", result["security"].get("oidc_issuer", ""))), "OIDC issuer", allow_empty=mode != "oidc")
        result["security"]["oidc_client_id"] = _validate_label(str(security.get("oidc_client_id", result["security"].get("oidc_client_id", ""))), "OIDC client ID", max_len=255, allow_empty=mode != "oidc")
        result["security"]["oidc_redirect_uri"] = _validate_url(str(security.get("oidc_redirect_uri", result["security"].get("oidc_redirect_uri", ""))), "OIDC redirect URI", allow_empty=mode != "oidc")

    if "build" in payload:
        build = payload.get("build")
        if not isinstance(build, dict):
            raise ValueError("build settings must be an object")
        for key in ("allow_real_run", "allow_unsafe_build_command"):
            if key in build and not isinstance(build[key], bool):
                raise ValueError(f"{key} must be a boolean")
            if key in build:
                result["build"][key] = build[key]
        temp_dir = str(build.get("temp_dir", result["build"]["temp_dir"])).strip()
        expanded = temp_dir.replace("${WORKFLOW_NAME}", "workflow")
        parsed = Path(expanded)
        if not temp_dir.startswith("/") or ".." in parsed.parts or parsed in {Path("/"), Path("/etc"), Path("/usr"), Path("/var"), Path("/home"), Path("/root")}:
            raise ValueError("build temp directory must be a safe absolute subdirectory")
        if not re.fullmatch(r"[A-Za-z0-9_./${}-]+", temp_dir):
            raise ValueError("build temp directory contains unsupported characters")
        result["build"]["temp_dir"] = temp_dir

    return result


def validate_apt_settings(payload: dict, current: dict) -> dict:
    """Compatibility wrapper for callers/tests that update all settings."""
    return validate_settings(payload, current)


def save_settings(data_dir: Path, settings: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_path(data_dir).write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
