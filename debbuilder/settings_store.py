"""Persistent public-safe application settings and local secrets."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from . import storage
from .workspace_cleanup import DEFAULT_POLICY, validate_policy
from .resource_limits import FIELDS, ResourceLimitError, empty_policy, normalize_policy
from .runtime import native_debian_architecture

_SECRET_WORDS = re.compile(r"(?i)(token|secret|password|passwd|apikey|api_key|client_secret)")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_ARCHES = {"all", "amd64", "arm64", "armhf", "i386"}
_MIN_COOKIE_SECRET_LENGTH = 40
SECRET_PRESERVE_SENTINEL = "masked"
SETTINGS_SCHEMA_VERSION = 1
SECRETS_SCHEMA_VERSION = 1
AUTOMATION_POLL_DEFAULT_SECONDS = 3600
AUTOMATION_POLL_MIN_SECONDS = 60
AUTOMATION_POLL_MAX_SECONDS = 24 * 60 * 60
AUTOMATION_CONCURRENCY_DEFAULT = 4
AUTOMATION_CONCURRENCY_MIN = 1
AUTOMATION_CONCURRENCY_MAX = 8


class SessionSecretError(RuntimeError):
    """The session signing secret is unavailable or invalid."""


class SettingsDocumentError(ValueError):
    """A persisted Settings or secrets document is not canonical v1."""

    def __init__(self, code: str, message: str, *, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.path = path

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "path": self.path, "details": {}}


def default_settings(repo_url: str, suite: str, component: str, architecture: str = "amd64", public_url: str = "", *, security: dict | None = None) -> dict:
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
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
        "notifications": {
            "type": "none",
            "server_url": "https://ntfy.sh",
            "topic": "debbuilder",
        },
        "automation": {
            "auto_validate_after_successful_build": False,
            "auto_publish_after_successful_validation": False,
            "upstream_checks_enabled": True,
            "upstream_check_interval_seconds": AUTOMATION_POLL_DEFAULT_SECONDS,
            "upstream_check_concurrency": AUTOMATION_CONCURRENCY_DEFAULT,
        },
        "resource_limits": empty_policy(),
        "workspace_cleanup": dict(DEFAULT_POLICY),
        "security": security or {"auth_mode": "none", "oidc_issuer": "", "oidc_client_id": "", "oidc_redirect_uri": ""},
    }


def settings_path(data_dir: Path) -> Path:
    return data_dir / "settings.json"


def secrets_path(data_dir: Path) -> Path:
    return data_dir / "secrets.json"


def _require_regular_document(path: Path, *, code: str, label: str):
    try:
        info = path.lstat()
    except OSError as exc:
        raise SettingsDocumentError(code, f"Stored {label} cannot be read safely") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SettingsDocumentError(code, f"Stored {label} must be a regular file")
    return info


def load_settings(data_dir: Path, defaults: dict) -> dict:
    path = settings_path(data_dir)
    if not path.exists() and not path.is_symlink():
        return validate_settings_document(defaults)
    return validate_settings_document(_read_settings_document(path))


def _read_settings_document(path: Path) -> dict:
    _require_regular_document(
        path, code="unreadable_settings_document", label="Settings",
    )
    try:
        stored = json.loads(path.read_text())
    except Exception as exc:
        raise SettingsDocumentError(
            "invalid_settings_json", "Stored Settings are not valid JSON",
        ) from exc
    return stored


def load_secrets(data_dir: Path) -> dict:
    path = secrets_path(data_dir)
    if not path.exists() and not path.is_symlink():
        return {"schema_version": SECRETS_SCHEMA_VERSION}
    info = _require_regular_document(
        path, code="unreadable_secrets_document", label="secrets",
    )
    mode = info.st_mode & 0o777
    if mode != 0o600:
        raise SettingsDocumentError(
            "insecure_secrets_permissions", "Stored secrets must use mode 0600",
        )
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise SettingsDocumentError(
            "invalid_secrets_json", "Stored secrets are not valid JSON",
        ) from exc
    return validate_secrets_document(data)


def _write_secrets_document(path: Path, value: dict) -> None:
    storage.atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_secrets(data_dir: Path, value: dict) -> None:
    """Persist one already assembled canonical Secrets v1 document."""
    canonical = validate_secrets_document(value)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_path(data_dir)
    with storage.locked_path(path):
        _write_secrets_document(path, canonical)


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


def validate_github_token(token: str, *, path: str = "$.github.token") -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if token == SECRET_PRESERVE_SENTINEL or len(token) < 20 or _SECRET_WORDS.search(token[:12]):
        raise SettingsDocumentError(
            "invalid_secret_value", "Invalid GitHub token", path=path,
        )
    return token


def ntfy_token(data_dir: Path) -> str:
    env = os.environ.get("DEBBUILDER_NTFY_TOKEN", "").strip()
    if env:
        return env
    notifications = load_secrets(data_dir).get("notifications")
    return str(notifications.get("token") or "") if isinstance(notifications, dict) else ""


def ntfy_token_configured(data_dir: Path) -> bool:
    return bool(ntfy_token(data_dir))


def oidc_client_secret(data_dir: Path) -> str:
    env = os.environ.get("DEBBUILDER_OIDC_CLIENT_SECRET", "").strip()
    if env:
        return env
    section = load_secrets(data_dir).get("oidc")
    return str(section.get("client_secret") or "") if isinstance(section, dict) else ""


def oidc_client_secret_configured(data_dir: Path) -> bool:
    return bool(oidc_client_secret(data_dir))


def validate_cookie_secret(value: str) -> str:
    value = str(value or "").strip()
    if len(value) < _MIN_COOKIE_SECRET_LENGTH:
        raise SessionSecretError("Session cookie secret is unavailable or invalid")
    return value


def _load_cookie_secret_document(data_dir: Path) -> dict:
    try:
        return load_secrets(data_dir)
    except SettingsDocumentError as exc:
        raise SessionSecretError("Session cookie secret is unavailable or invalid") from exc


def cookie_secret(data_dir: Path) -> str:
    """Read the prepared cookie secret without mutating persistent state."""
    env = os.environ.get("DEBBUILDER_COOKIE_SECRET", "").strip()
    if env:
        return validate_cookie_secret(env)
    section = _load_cookie_secret_document(data_dir).get("session")
    value = str(section.get("cookie_secret") or "") if isinstance(section, dict) else ""
    return validate_cookie_secret(value)


def prepare_cookie_secret(data_dir: Path) -> str:
    """Create or validate session signing state during owned startup/mutation."""
    env = os.environ.get("DEBBUILDER_COOKIE_SECRET", "").strip()
    if env:
        return validate_cookie_secret(env)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = secrets_path(data_dir)
    with storage.locked_path(settings_path(data_dir)):
        with storage.locked_path(path):
            data = _load_cookie_secret_document(data_dir)
            section = data.get("session")
            value = str(section.get("cookie_secret") or "") if isinstance(section, dict) else ""
            if value:
                return validate_cookie_secret(value)
            value = secrets.token_urlsafe(48)
            data.setdefault("session", {})["cookie_secret"] = value
            _write_secrets_document(path, validate_secrets_document(data))
            return validate_cookie_secret(value)


def _validate_url(value: str, field: str, *, allow_empty: bool = False, path: str = "$") -> str:
    value = (value or "").strip()
    if allow_empty and not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SettingsDocumentError("invalid_settings_field", f"{field} must be an http(s) URL", path=path)
    if parsed.username or parsed.password or _SECRET_WORDS.search(parsed.netloc):
        raise SettingsDocumentError("invalid_settings_field", f"{field} must not contain credentials", path=path)
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        if _SECRET_WORDS.search(key) or _SECRET_WORDS.search(val):
            raise SettingsDocumentError("invalid_settings_field", f"{field} must not contain secret-like query values", path=path)
    return value.rstrip("/")


def _validate_repo_url(value: str, *, path: str = "$.apt.repository") -> str:
    if value == "":
        return ""
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?", value):
        raise SettingsDocumentError("invalid_settings_field", "repository must be a safe HTTP(S) base URL", path=path)
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsDocumentError("invalid_settings_field", "repository port is invalid", path=path) from exc
    if (not parsed.hostname or parsed.hostname.startswith(".") or parsed.hostname.endswith(".")
            or ".." in parsed.hostname or port == 0 or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise SettingsDocumentError("invalid_settings_field", "repository URL is invalid", path=path)
    return value.rstrip("/")


def _validate_name(value: str, field: str, *, path: str = "$") -> str:
    value = (value or "").strip()
    if not value or not _SAFE_NAME.fullmatch(value):
        raise SettingsDocumentError("invalid_settings_field", f"invalid {field}", path=path)
    return value


def _validate_label(value: str, field: str, *, max_len: int = 80, allow_empty: bool = False, path: str = "$") -> str:
    value = (value or "").strip()
    if not value and allow_empty:
        return ""
    if not value or len(value) > max_len or _SECRET_WORDS.search(value):
        raise SettingsDocumentError("invalid_settings_field", f"invalid {field}", path=path)
    return value


def _object(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise SettingsDocumentError("invalid_settings_field", f"{path} must be an object", path=path)
    return value


def _reject_unknown(value: dict, allowed, path: str, *, document: str = "Settings") -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        child = f"{path}.{unknown[0]}" if path != "$" else f"$.{unknown[0]}"
        raise SettingsDocumentError(
            f"unknown_{document.lower()}_field", f"Unknown {document} field: {child}", path=child,
        )


def _require_exact_fields(value: dict, fields, path: str, *, document: str = "Settings") -> None:
    _reject_unknown(value, fields, path, document=document)
    missing = sorted(set(fields) - set(value))
    if missing:
        child = f"{path}.{missing[0]}"
        raise SettingsDocumentError(
            f"missing_{document.lower()}_field", f"Missing {document} field: {child}", path=child,
        )


def _string(value, path: str) -> str:
    if not isinstance(value, str):
        raise SettingsDocumentError("invalid_settings_field", f"{path} must be a string", path=path)
    return value


def _boolean(value, path: str) -> bool:
    if type(value) is not bool:
        raise SettingsDocumentError("invalid_settings_field", f"{path} must be a boolean", path=path)
    return value


def _integer(value, path: str) -> int:
    if type(value) is not int:
        raise SettingsDocumentError("invalid_settings_field", f"{path} must be an integer", path=path)
    return value


def _validated_notifications(value: dict, *, exact: bool) -> dict:
    path = "$.notifications"
    value = _object(value, path)
    fields = {"type", "server_url", "topic"}
    _require_exact_fields(value, fields, path) if exact else _reject_unknown(value, fields | {"token"}, path)
    result = {}
    if "type" in value:
        notif_type = _string(value["type"], f"{path}.type").strip()
        if notif_type not in {"none", "ntfy"}:
            raise SettingsDocumentError(
                "invalid_settings_field", "Unsupported notification type", path=f"{path}.type",
            )
        result["type"] = notif_type
    if "server_url" in value:
        server_url = _string(value["server_url"], f"{path}.server_url").strip()
        result["server_url"] = _validate_url(server_url, "ntfy server URL", path=f"{path}.server_url") if server_url else ""
    if "topic" in value:
        topic = _string(value["topic"], f"{path}.topic").strip()
        result["topic"] = _validate_name(topic, "ntfy topic", path=f"{path}.topic") if topic else ""
    if "token" in value:
        _string(value["token"], f"{path}.token")
    return result


def _validated_automation(value: dict, *, exact: bool) -> dict:
    path = "$.automation"
    value = _object(value, path)
    fields = {
        "auto_validate_after_successful_build",
        "auto_publish_after_successful_validation",
        "upstream_checks_enabled",
        "upstream_check_interval_seconds",
        "upstream_check_concurrency",
    }
    _require_exact_fields(value, fields, path) if exact else _reject_unknown(value, fields, path)
    result = {}
    for field in (
        "auto_validate_after_successful_build",
        "auto_publish_after_successful_validation",
        "upstream_checks_enabled",
    ):
        if field in value:
            result[field] = _boolean(value[field], f"{path}.{field}")
    if "upstream_check_interval_seconds" in value:
        interval = _integer(value["upstream_check_interval_seconds"], f"{path}.upstream_check_interval_seconds")
        if not AUTOMATION_POLL_MIN_SECONDS <= interval <= AUTOMATION_POLL_MAX_SECONDS:
            raise SettingsDocumentError(
                "invalid_settings_field", "upstream_check_interval_seconds is outside the supported bounds",
                path=f"{path}.upstream_check_interval_seconds",
            )
        result["upstream_check_interval_seconds"] = interval
    if "upstream_check_concurrency" in value:
        concurrency = _integer(value["upstream_check_concurrency"], f"{path}.upstream_check_concurrency")
        if not AUTOMATION_CONCURRENCY_MIN <= concurrency <= AUTOMATION_CONCURRENCY_MAX:
            raise SettingsDocumentError(
                "invalid_settings_field", "upstream_check_concurrency is outside the supported bounds",
                path=f"{path}.upstream_check_concurrency",
            )
        result["upstream_check_concurrency"] = concurrency
    return result


def _validated_security(value: dict, *, exact: bool) -> dict:
    path = "$.security"
    value = _object(value, path)
    fields = {"auth_mode", "oidc_issuer", "oidc_client_id", "oidc_redirect_uri"}
    _require_exact_fields(value, fields, path) if exact else _reject_unknown(value, fields | {"oidc_client_secret"}, path)
    result = {}
    if "auth_mode" in value:
        mode = _string(value["auth_mode"], f"{path}.auth_mode").lower().strip()
        if mode not in {"none", "header", "oidc"}:
            raise SettingsDocumentError(
                "invalid_settings_field", "Unsupported authentication mode", path=f"{path}.auth_mode",
            )
        result["auth_mode"] = mode
    for field in ("oidc_issuer", "oidc_client_id", "oidc_redirect_uri", "oidc_client_secret"):
        if field in value:
            result[field] = _string(value[field], f"{path}.{field}")
    return result


def validate_settings_document(value: dict) -> dict:
    """Validate one complete canonical Settings v1 document without defaults."""
    value = _object(value, "$")
    sections = {
        "schema_version", "general", "apt", "notifications", "automation",
        "workspace_cleanup", "resource_limits", "security",
    }
    _require_exact_fields(value, sections, "$")
    version = value["schema_version"]
    if type(version) is not int or version != SETTINGS_SCHEMA_VERSION:
        raise SettingsDocumentError(
            "unsupported_settings_schema_version",
            f"Settings schema_version must be {SETTINGS_SCHEMA_VERSION}",
            path="$.schema_version",
        )

    general = _object(value["general"], "$.general")
    _require_exact_fields(general, {"app_name", "url"}, "$.general")
    normalized_general = {
        "app_name": _validate_label(_string(general["app_name"], "$.general.app_name"), "app name", path="$.general.app_name"),
        "url": _validate_url(_string(general["url"], "$.general.url"), "public url", allow_empty=True, path="$.general.url"),
    }

    apt = _object(value["apt"], "$.apt")
    _require_exact_fields(apt, {"repository", "distribution", "component", "architecture"}, "$.apt")
    architecture = _validate_name(_string(apt["architecture"], "$.apt.architecture"), "architecture", path="$.apt.architecture")
    if architecture not in _ARCHES and architecture != native_debian_architecture():
        raise SettingsDocumentError(
            "invalid_settings_field", "Unsupported architecture", path="$.apt.architecture",
        )
    normalized_apt = {
        "repository": _validate_repo_url(_string(apt["repository"], "$.apt.repository"), path="$.apt.repository"),
        "distribution": _validate_name(_string(apt["distribution"], "$.apt.distribution"), "distribution", path="$.apt.distribution"),
        "component": _validate_name(_string(apt["component"], "$.apt.component"), "component", path="$.apt.component"),
        "architecture": architecture,
    }

    notifications = _validated_notifications(value["notifications"], exact=True)
    if notifications["type"] == "ntfy" and (not notifications["server_url"] or not notifications["topic"]):
        raise SettingsDocumentError(
            "invalid_settings_field", "Enabled ntfy settings require a server URL and topic",
            path="$.notifications",
        )

    automation = _validated_automation(value["automation"], exact=True)
    if automation["auto_publish_after_successful_validation"] and not automation["auto_validate_after_successful_build"]:
        raise SettingsDocumentError(
            "invalid_settings_field", "Automatic publication requires automatic validation",
            path="$.automation.auto_publish_after_successful_validation",
        )

    cleanup = _object(value["workspace_cleanup"], "$.workspace_cleanup")
    _require_exact_fields(cleanup, set(DEFAULT_POLICY), "$.workspace_cleanup")
    try:
        normalized_cleanup = validate_policy(cleanup)
    except ValueError as exc:
        field = "enabled" if "enabled" in str(exc) else "failed_workspaces_to_retain"
        raise SettingsDocumentError(
            "invalid_settings_field", str(exc), path=f"$.workspace_cleanup.{field}",
        ) from exc

    limits = _object(value["resource_limits"], "$.resource_limits")
    _require_exact_fields(limits, set(FIELDS), "$.resource_limits")
    normalized_limits = normalize_policy(limits, path="$.resource_limits")

    security = _validated_security(value["security"], exact=True)
    mode = security["auth_mode"]
    security["oidc_issuer"] = _validate_url(security["oidc_issuer"], "OIDC issuer", allow_empty=mode != "oidc", path="$.security.oidc_issuer")
    security["oidc_client_id"] = _validate_label(
        security["oidc_client_id"], "OIDC client ID", max_len=255, allow_empty=mode != "oidc", path="$.security.oidc_client_id",
    )
    security["oidc_redirect_uri"] = _validate_url(
        security["oidc_redirect_uri"], "OIDC redirect URI", allow_empty=mode != "oidc", path="$.security.oidc_redirect_uri",
    )

    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "general": normalized_general,
        "apt": normalized_apt,
        "notifications": notifications,
        "automation": automation,
        "workspace_cleanup": normalized_cleanup,
        "resource_limits": normalized_limits,
        "security": security,
    }


def validate_settings(payload: dict, current: dict) -> dict:
    if not isinstance(payload, dict):
        raise SettingsDocumentError(
            "invalid_settings_field", "Settings payload must be an object", path="$",
        )
    _reject_unknown(payload, {
        "general", "apt", "github", "notifications", "automation",
        "workspace_cleanup", "resource_limits", "security",
    }, "$")
    result = validate_settings_document(current)

    if "general" in payload:
        general = _object(payload["general"], "$.general")
        _reject_unknown(general, {"app_name", "url"}, "$.general")
        if "app_name" in general:
            result["general"]["app_name"] = _validate_label(
                _string(general["app_name"], "$.general.app_name"), "app name", path="$.general.app_name",
            )
        if "url" in general:
            result["general"]["url"] = _validate_url(
                _string(general["url"], "$.general.url"), "public url", allow_empty=True, path="$.general.url",
            )

    if "apt" in payload:
        apt = _object(payload["apt"], "$.apt")
        _reject_unknown(apt, {"repository", "distribution", "component", "architecture"}, "$.apt")
        if "repository" in apt:
            result["apt"]["repository"] = _validate_repo_url(_string(apt["repository"], "$.apt.repository"), path="$.apt.repository")
        for field in ("distribution", "component"):
            if field in apt:
                result["apt"][field] = _validate_name(_string(apt[field], f"$.apt.{field}"), field, path=f"$.apt.{field}")
        if "architecture" in apt:
            architecture = _validate_name(_string(apt["architecture"], "$.apt.architecture"), "architecture", path="$.apt.architecture")
            if architecture not in _ARCHES and architecture != native_debian_architecture():
                raise SettingsDocumentError(
                    "invalid_settings_field", "Unsupported architecture", path="$.apt.architecture",
                )
            result["apt"]["architecture"] = architecture

    if "github" in payload:
        github = _object(payload["github"], "$.github")
        _reject_unknown(github, {"token"}, "$.github")
        if "token" in github:
            _string(github["token"], "$.github.token")

    if "notifications" in payload:
        result["notifications"].update(_validated_notifications(payload["notifications"], exact=False))

    if "automation" in payload:
        automation = _validated_automation(payload["automation"], exact=False)
        auto_validate = automation.get("auto_validate_after_successful_build", result["automation"]["auto_validate_after_successful_build"])
        auto_publish = automation.get("auto_publish_after_successful_validation", result["automation"]["auto_publish_after_successful_validation"])
        if automation.get("auto_validate_after_successful_build") is False and automation.get("auto_publish_after_successful_validation") is not True:
            auto_publish = False
        if auto_publish:
            auto_validate = True
        result["automation"]["auto_validate_after_successful_build"] = auto_validate
        result["automation"]["auto_publish_after_successful_validation"] = auto_publish
        result["automation"].update({
            key: value for key, value in automation.items()
            if key not in {"auto_validate_after_successful_build", "auto_publish_after_successful_validation"}
        })

    if "workspace_cleanup" in payload:
        policy = _object(payload["workspace_cleanup"], "$.workspace_cleanup")
        _reject_unknown(policy, set(DEFAULT_POLICY), "$.workspace_cleanup")
        try:
            result["workspace_cleanup"] = validate_policy({**result.get("workspace_cleanup", DEFAULT_POLICY), **policy})
        except ValueError as exc:
            field = "enabled" if "enabled" in str(exc) else "failed_workspaces_to_retain"
            raise SettingsDocumentError(
                "invalid_settings_field", str(exc), path=f"$.workspace_cleanup.{field}",
            ) from exc

    if "resource_limits" in payload:
        if not isinstance(payload["resource_limits"], dict):
            raise ResourceLimitError(
                "invalid_resource_limits", "Resource limits must be an object",
                path="$.resource_limits",
            )
        result["resource_limits"] = normalize_policy(
            {**result.get("resource_limits", empty_policy()), **payload["resource_limits"]},
            path="$.resource_limits",
        )

    if "security" in payload:
        security = _validated_security(payload["security"], exact=False)
        mode = security.get("auth_mode", result["security"]["auth_mode"])
        result["security"]["auth_mode"] = mode
        result["security"]["oidc_issuer"] = _validate_url(
            security.get("oidc_issuer", result["security"]["oidc_issuer"]),
            "OIDC issuer", allow_empty=mode != "oidc", path="$.security.oidc_issuer",
        )
        result["security"]["oidc_client_id"] = _validate_label(
            security.get("oidc_client_id", result["security"]["oidc_client_id"]),
            "OIDC client ID", max_len=255, allow_empty=mode != "oidc", path="$.security.oidc_client_id",
        )
        result["security"]["oidc_redirect_uri"] = _validate_url(
            security.get("oidc_redirect_uri", result["security"]["oidc_redirect_uri"]),
            "OIDC redirect URI", allow_empty=mode != "oidc", path="$.security.oidc_redirect_uri",
        )

    return validate_settings_document(result)


def validate_secrets_document(value: dict) -> dict:
    """Validate the versioned local secret store; configured sections are optional."""
    if not isinstance(value, dict):
        raise SettingsDocumentError("invalid_secrets_document", "Stored secrets must be an object")
    _reject_unknown(
        value, {"schema_version", "github", "notifications", "oidc", "session"},
        "$", document="Secrets",
    )
    version = value.get("schema_version")
    if type(version) is not int or version != SECRETS_SCHEMA_VERSION:
        raise SettingsDocumentError(
            "unsupported_secrets_schema_version",
            f"Secrets schema_version must be {SECRETS_SCHEMA_VERSION}",
            path="$.schema_version",
        )
    result = {"schema_version": SECRETS_SCHEMA_VERSION}
    specifications = {
        "github": ("token", None),
        "notifications": ("token", None),
        "oidc": ("client_secret", None),
        "session": ("cookie_secret", _MIN_COOKIE_SECRET_LENGTH),
    }
    for section, (key, minimum) in specifications.items():
        if section not in value:
            continue
        section_value = value[section]
        path = f"$.{section}"
        if not isinstance(section_value, dict):
            raise SettingsDocumentError("invalid_secrets_field", f"{path} must be an object", path=path)
        _require_exact_fields(section_value, {key}, path, document="Secrets")
        secret = section_value[key]
        if not isinstance(secret, str) or not secret.strip() or (minimum is not None and len(secret.strip()) < minimum):
            raise SettingsDocumentError(
                "invalid_secrets_field", f"{path}.{key} is invalid", path=f"{path}.{key}",
            )
        secret = secret.strip()
        if secret == SECRET_PRESERVE_SENTINEL:
            raise SettingsDocumentError(
                "invalid_secrets_field", f"{path}.{key} cannot contain the preservation sentinel",
                path=f"{path}.{key}",
            )
        if section == "github":
            secret = validate_github_token(secret, path=f"{path}.{key}")
        result[section] = {key: secret}
    return result


def _resource_repair_document(data_dir: Path) -> tuple[dict, Exception]:
    """Read a current-v1 document whose only invalid state is resource limits."""
    path = settings_path(data_dir)
    if not path.exists() and not path.is_symlink():
        raise SettingsDocumentError(
            "resource_settings_repair_not_required",
            "No persisted Settings document requires resource-limit repair",
            path="$.resource_limits",
        )
    stored = _read_settings_document(path)
    try:
        validate_settings_document(stored)
    except (SettingsDocumentError, ResourceLimitError) as exc:
        if not str(getattr(exc, "path", "$")).startswith("$.resource_limits"):
            raise
        original_error = exc
    else:
        raise SettingsDocumentError(
            "resource_settings_repair_not_required",
            "Stored resource-limit Settings are already valid",
            path="$.resource_limits",
        )

    candidate = deepcopy(stored)
    candidate["resource_limits"] = empty_policy()
    validate_settings_document(candidate)
    return stored, original_error


def resource_repair_security(data_dir: Path) -> dict:
    """Return security only when resource limits are the sole v1 corruption."""
    stored, _error = _resource_repair_document(data_dir)
    candidate = deepcopy(stored)
    candidate["resource_limits"] = empty_policy()
    return validate_settings_document(candidate)["security"]


def repair_resource_limits(data_dir: Path, payload: dict) -> dict:
    """Explicitly repair only resource limits in an otherwise canonical v1 document."""
    payload = _object(payload, "$")
    _reject_unknown(payload, {"resource_limits"}, "$")
    if "resource_limits" not in payload:
        raise SettingsDocumentError(
            "missing_settings_field", "Resource-limit repair requires $.resource_limits",
            path="$.resource_limits",
        )
    patch = _object(payload["resource_limits"], "$.resource_limits")
    _reject_unknown(patch, set(FIELDS), "$.resource_limits")
    stored, _error = _resource_repair_document(data_dir)
    stored_limits = stored.get("resource_limits")
    repaired = empty_policy()
    if isinstance(stored_limits, dict):
        for field in FIELDS:
            if field in patch or field not in stored_limits:
                continue
            try:
                repaired[field] = normalize_policy(
                    {field: stored_limits[field]}, path="$.resource_limits",
                )[field]
            except ResourceLimitError:
                pass
    repaired.update(patch)
    candidate = deepcopy(stored)
    candidate["resource_limits"] = normalize_policy(repaired, path="$.resource_limits")
    canonical = validate_settings_document(candidate)
    save_settings(data_dir, canonical)
    return canonical


def save_settings(data_dir: Path, settings: dict) -> None:
    canonical = validate_settings_document(settings)
    data_dir.mkdir(parents=True, exist_ok=True)
    storage.atomic_write_text(settings_path(data_dir), json.dumps(canonical, indent=2, sort_keys=True) + "\n")
