"""Central runtime configuration for a DebBuilder process."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import ipaddress
import re
import socket
import subprocess
from typing import Mapping


@lru_cache(maxsize=1)
def native_debian_architecture() -> str:
    """Use dpkg's host architecture without imposing a Validation profile limit."""
    result = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, timeout=10, check=True)
    value = result.stdout.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) or value in {"all", "source"}:
        raise ValueError("dpkg returned an invalid native architecture")
    return value


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
    repository_host: str
    repository_port: int
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
            repository_url=environ.get("DEBBUILDER_REPO_URL", ""),
            suite=environ.get("DEBBUILDER_SUITE", "stable"),
            component=environ.get("DEBBUILDER_COMPONENT", "main"),
            host=validate_listen_host(environ.get("DEBBUILDER_HOST", "127.0.0.1")),
            port=validate_listen_port(environ.get("DEBBUILDER_PORT", "8099")),
            repository_host=validate_listen_host(environ.get("DEBBUILDER_REPOSITORY_HOST", "127.0.0.1")),
            repository_port=validate_listen_port(environ.get("DEBBUILDER_REPOSITORY_PORT", "8081")),
            auth_mode=environ.get("DEBBUILDER_AUTH_MODE", "none").lower(),
            auth_header=environ.get("DEBBUILDER_AUTH_HEADER", "X-Forwarded-User"),
            oidc_issuer=environ.get("DEBBUILDER_OIDC_ISSUER", "https://auth.example.invalid").rstrip("/"),
            oidc_client_id=environ.get("DEBBUILDER_OIDC_CLIENT_ID", ""),
            oidc_redirect_uri=environ.get("DEBBUILDER_OIDC_REDIRECT_URI", ""),
            public_url=environ.get("DEBBUILDER_PUBLIC_URL", ""),
        )


def validate_listen_host(value: str) -> str:
    if not value or value != value.strip() or len(value) > 253:
        raise ValueError("listener host is invalid")
    try:
        ipaddress.IPv4Address(value)
        return value
    except ipaddress.AddressValueError:
        pass
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
               for part in value.split(".")):
        raise ValueError("listener host is invalid")
    try:
        socket.getaddrinfo(value, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"listener host cannot resolve: {value}") from exc
    return value


def validate_listen_port(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", str(value)) or not 0 <= int(value) <= 65535:
        raise ValueError(f"listener port is invalid: {value}")
    return int(value)


def listeners_overlap(admin_host: str, admin_port: int, repo_host: str, repo_port: int) -> bool:
    if admin_port != repo_port or admin_port == 0:
        return False
    addresses = []
    for host in (admin_host, repo_host):
        addresses.append({row[4][0] for row in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)})
    return "0.0.0.0" in addresses[0] or "0.0.0.0" in addresses[1] or bool(addresses[0] & addresses[1])
