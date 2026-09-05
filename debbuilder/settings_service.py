"""Settings assembly and persistence helpers."""
from __future__ import annotations

from pathlib import Path

from .settings_store import (
    default_settings,
    github_token_configured,
    load_settings,
    ntfy_token_configured,
    oidc_client_secret,
    oidc_client_secret_configured,
    save_github_token,
    save_oidc_client_secret,
    save_settings,
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
        "amd64",
        public_url,
        security={
            "auth_mode": auth_mode,
            "oidc_issuer": "" if oidc_issuer.endswith("example.invalid") else oidc_issuer,
            "oidc_client_id": oidc_client_id,
            "oidc_redirect_uri": oidc_redirect_uri,
        },
    )


def load_app_settings(data_dir: Path, defaults: dict) -> dict:
    return load_settings(data_dir, defaults)


def public_settings_view(*, data_dir: Path, root: Path, settings: dict, port: int) -> dict:
    general = dict(settings["general"])
    general.update({"port": port, "workdir": str(root)})
    return {
        "general": general,
        "apt": settings["apt"],
        "github": {
            "token": "masked",
            "token_configured": github_token_configured(data_dir),
        },
        "security": {
            **settings["security"],
            "pocket_id_active": settings["security"]["auth_mode"] == "oidc",
            "oidc_client_secret_configured": oidc_client_secret_configured(data_dir),
        },
        "notifications": {
            **settings["notifications"],
            "token_configured": ntfy_token_configured(data_dir),
        },
        "automation": settings.get("automation", {}),
        "workspace_cleanup": settings["workspace_cleanup"],
    }


def update_settings(data_dir: Path, payload: dict, current: dict, view_factory) -> dict:
    new_settings = validate_settings(payload, current)
    github_payload = payload.get("github") if isinstance(payload, dict) else None
    if isinstance(github_payload, dict) and github_payload.get("token"):
        save_github_token(data_dir, str(github_payload["token"]))

    security_payload = payload.get("security") if isinstance(payload, dict) else None
    new_secret = isinstance(security_payload, dict) and str(security_payload.get("oidc_client_secret") or "").strip()
    if new_settings["security"]["auth_mode"] == "oidc" and not (new_secret or oidc_client_secret(data_dir)):
        raise ValueError("OIDC client secret is required before enabling authentication")
    if new_secret:
        save_oidc_client_secret(data_dir, str(security_payload["oidc_client_secret"]))

    save_settings(data_dir, new_settings)
    return view_factory()
