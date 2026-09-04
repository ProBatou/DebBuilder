"""Central runtime configuration for a DebBuilder process."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeConfig:
    root: Path
    static: Path
    examples: Path
    data: Path
    repository_root: Path
    repository_url: str
    suite: str
    component: str
    host: str
    port: int
    auth_mode: str
    auth_header: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_redirect_uri: str
    public_url: str

    @property
    def workflows(self) -> Path:
        return self.data / "workflows"

    @property
    def builds(self) -> Path:
        return self.data / "builds"

    def prepare_data_directories(self) -> None:
        self.workflows.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls, root: Path, environ: Mapping[str, str]) -> "RuntimeConfig":
        root = Path(root).resolve()
        configured_data = str(environ.get("DEBBUILDER_DATA_DIR") or "").strip()
        data = Path(configured_data).expanduser() if configured_data else root / "data"
        return cls(
            root=root,
            static=root / "static",
            examples=root / "examples" / "recipes",
            data=data,
            repository_root=Path(environ.get("DEBBUILDER_REPO_ROOT", "/var/www/html")).expanduser(),
            repository_url=environ.get("DEBBUILDER_REPO_URL", "https://repo.example.invalid"),
            suite=environ.get("DEBBUILDER_SUITE", "stable"),
            component=environ.get("DEBBUILDER_COMPONENT", "main"),
            host=environ.get("DEBBUILDER_HOST", "0.0.0.0"),
            port=int(environ.get("DEBBUILDER_PORT", "8099")),
            auth_mode=environ.get("DEBBUILDER_AUTH_MODE", "none").lower(),
            auth_header=environ.get("DEBBUILDER_AUTH_HEADER", "X-Forwarded-User"),
            oidc_issuer=environ.get("DEBBUILDER_OIDC_ISSUER", "https://auth.example.invalid").rstrip("/"),
            oidc_client_id=environ.get("DEBBUILDER_OIDC_CLIENT_ID", ""),
            oidc_redirect_uri=environ.get("DEBBUILDER_OIDC_REDIRECT_URI", ""),
            public_url=environ.get("DEBBUILDER_PUBLIC_URL", ""),
        )
