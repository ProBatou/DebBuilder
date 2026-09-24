"""Settings assembly and persistence helpers."""
from __future__ import annotations

import os
import secrets
from copy import deepcopy
from pathlib import Path

from . import storage
from .resource_limits import ResourceLimitError
from .runtime import native_debian_architecture
from .settings_store import (
    SECRET_PRESERVE_SENTINEL,
    SessionSecretError,
    SettingsDocumentError,
    default_settings,
    github_token_configured,
    load_settings,
    load_secrets,
    ntfy_token_configured,
    oidc_client_secret_configured,
    repair_resource_limits,
    resource_repair_security,
    save_secrets,
    save_settings,
    settings_path,
    validate_cookie_secret,
    validate_github_token,
    validate_secrets_document,
    validate_settings,
)


def defaults_from_environment(
    *,
    repo_default: str,
    suite_default: str,
    component_default: str,
    auth_mode: str,
    oidc_issuer: str,
    oidc_client_id: str,
    oidc_redirect_uri: str,
    public_url: str = "",
) -> dict:
    return default_settings(
        repo_default,
        suite_default,
        component_default,
        native_debian_architecture(),
        public_url,
        security={
            "auth_mode": auth_mode,
            "oidc_issuer": "" if oidc_issuer.endswith("example.invalid") else oidc_issuer,
            "oidc_client_id": oidc_client_id,
            "oidc_redirect_uri": oidc_redirect_uri,
        },
    )


def validate_app_settings_storage(data_dir: Path, defaults: dict) -> dict:
    """Read-only startup validation for the canonical Settings and secrets stores."""
    settings = load_settings(data_dir, defaults)
    secret_document = load_secrets(data_dir)
    _require_oidc_client_secret(settings, secret_document)
    return settings


def public_settings_view(*, data_dir: Path, settings: dict) -> dict:
    return {
        "general": settings["general"],
        "apt": settings["apt"],
        "github": {
            "token_configured": github_token_configured(data_dir),
        },
        "security": {
            **settings["security"],
            "oidc_client_secret_configured": oidc_client_secret_configured(data_dir),
        },
        "notifications": {
            **settings["notifications"],
            "token_configured": ntfy_token_configured(data_dir),
        },
        "automation": settings["automation"],
        "workspace_cleanup": settings["workspace_cleanup"],
        "resource_limits": settings["resource_limits"],
    }


def _secret_replacement(payload: dict, section: str, field: str) -> str | None:
    section_payload = payload.get(section)
    if not isinstance(section_payload, dict) or field not in section_payload:
        return None
    value = section_payload[field].strip()
    if not value or value == SECRET_PRESERVE_SENTINEL:
        return None
    return value


def _replace_secret(document: dict, section: str, field: str, value: str | None) -> None:
    if value is not None:
        document.setdefault(section, {})[field] = value


def _require_oidc_client_secret(settings: dict, secret_document: dict) -> None:
    if settings["security"]["auth_mode"] != "oidc":
        return
    effective = os.environ.get("DEBBUILDER_OIDC_CLIENT_SECRET", "").strip()
    if not effective:
        effective = str((secret_document.get("oidc") or {}).get("client_secret") or "").strip()
    if not effective or effective == SECRET_PRESERVE_SENTINEL:
        raise SettingsDocumentError(
            "missing_required_secret",
            "OIDC client secret is required before enabling authentication",
            path="$.security.oidc_client_secret",
        )


def _mutation_plan(payload: dict, current: dict, existing_secrets: dict) -> tuple[dict, dict, bool]:
    """Validate every requested Settings-owned semantic before any durable write."""
    new_settings = validate_settings(payload, current)
    new_secrets = deepcopy(existing_secrets)

    github_secret = _secret_replacement(payload, "github", "token")
    if github_secret is not None:
        github_secret = validate_github_token(github_secret, path="$.github.token")
    _replace_secret(new_secrets, "github", "token", github_secret)

    notification_secret = _secret_replacement(payload, "notifications", "token")
    _replace_secret(new_secrets, "notifications", "token", notification_secret)

    oidc_secret = _secret_replacement(payload, "security", "oidc_client_secret")
    _replace_secret(new_secrets, "oidc", "client_secret", oidc_secret)

    _require_oidc_client_secret(new_settings, new_secrets)
    if new_settings["security"]["auth_mode"] == "oidc":
        cookie_from_environment = os.environ.get("DEBBUILDER_COOKIE_SECRET", "").strip()
        if cookie_from_environment:
            try:
                validate_cookie_secret(cookie_from_environment)
            except SessionSecretError as exc:
                raise SettingsDocumentError(
                    "invalid_required_secret", "Session cookie secret is invalid",
                    path="$.security.auth_mode",
                ) from exc
        elif "session" not in new_secrets:
            new_secrets["session"] = {"cookie_secret": secrets.token_urlsafe(48)}

    canonical_secrets = validate_secrets_document(new_secrets)
    return new_settings, canonical_secrets, canonical_secrets != existing_secrets


def update_settings(data_dir: Path, payload: dict, defaults: dict, *, before_save=None) -> None:
    """Serialize the complete current-v1 mutation, including resource-only repair."""
    with storage.locked_path(settings_path(data_dir)):
        try:
            current = load_settings(data_dir, defaults)
        except (SettingsDocumentError, ResourceLimitError):
            # Repair may change only resource limits. A malformed Secrets store
            # must reject the request before Settings is replaced.
            existing_secrets = load_secrets(data_dir)
            _require_oidc_client_secret(
                {"security": resource_repair_security(data_dir)}, existing_secrets,
            )
            repair_resource_limits(data_dir, payload)
            return

        existing_secrets = load_secrets(data_dir)
        new_settings, new_secrets, secrets_changed = _mutation_plan(
            payload, current, existing_secrets,
        )
        if before_save is not None:
            before_save(current, new_settings)
        if secrets_changed:
            save_secrets(data_dir, new_secrets)
        save_settings(data_dir, new_settings)
