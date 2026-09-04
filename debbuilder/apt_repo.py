"""APT repository parsing and reprepro publication helpers."""

from __future__ import annotations

import gzip
import os
import re
import shlex
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from .command_runner import run_command


DEBIAN_VERSION_PATTERN = re.compile(
    r"(?:(?P<epoch>[0-9]+):)?"
    r"(?P<upstream>[0-9][A-Za-z0-9.+:~\-]*?)"
    r"(?:-(?P<revision>[A-Za-z0-9.+~]+))?"
)


def debian_upstream_version(version: str) -> str:
    """Extract epoch and upstream version according to Debian's version grammar."""
    value = str(version or "").strip()
    match = DEBIAN_VERSION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"invalid Debian version: {version}")
    epoch = match.group("epoch")
    upstream = match.group("upstream")
    return f"{epoch}:{upstream}" if epoch is not None else upstream


def reprepro_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Keep repository signing bound to the invoking account's GnuPG home."""
    environment = os.environ if environ is None else environ
    configured = str(environment.get("GNUPGHOME") or "").strip()
    home = Path(str(environment.get("HOME") or Path.home())).expanduser()
    return {"LC_ALL": "C", "GNUPGHOME": configured or str(home / ".gnupg")}


def parse_packages_index(text: str) -> list[dict]:
    """Parse a Debian Packages index preserving all entries and versions."""
    rows: list[dict] = []
    cur: dict[str, str] = {}
    last_key: str | None = None
    for raw in (text or "").splitlines() + [""]:
        if not raw.strip():
            if cur:
                rows.append(cur)
                cur = {}
                last_key = None
            continue
        if raw.startswith(" ") and last_key:
            cur[last_key] += "\n" + raw[1:]
            continue
        if ": " in raw:
            key, value = raw.split(": ", 1)
            cur[key] = value
            last_key = key
    return rows


def fetch_packages_index(repo_url: str, distribution: str, component: str, architecture: str, timeout: int = 20) -> list[dict]:
    base = repo_url.rstrip("/") + "/"
    rel = f"dists/{distribution}/{component}/binary-{architecture}/Packages.gz"
    with urllib.request.urlopen(urljoin(base, rel), timeout=timeout) as response:
        payload = response.read()
    try:
        text = gzip.decompress(payload).decode(errors="replace")
    except OSError:
        text = payload.decode(errors="replace")
    return parse_packages_index(text)


def local_packages_index(repo_root: Path, distribution: str, component: str, architecture: str) -> list[dict]:
    """Read the repository's exported index, never just reprepro's internal database."""
    binary = Path(repo_root).resolve() / "dists" / distribution / component / f"binary-{architecture}"
    compressed = binary / "Packages.gz"
    plain = binary / "Packages"
    if compressed.is_file():
        return parse_packages_index(gzip.decompress(compressed.read_bytes()).decode(errors="replace"))
    if plain.is_file():
        return parse_packages_index(plain.read_text(encoding="utf-8", errors="replace"))
    return []


def debian_version_relation(candidate: str, published: str, *, workspace: Path, runner=run_command) -> dict:
    """Compare versions with dpkg's Debian version semantics."""
    if candidate == published:
        return {"relation": "equal", "command": None}
    commands = []
    for operator, relation in (("gt", "newer"), ("lt", "older")):
        command = " ".join(shlex.quote(value) for value in ("dpkg", "--compare-versions", candidate, operator, published))
        result = runner(command, workspace=Path(workspace).resolve(), working_directory=".", environment={"LC_ALL": "C"}, timeout=30)
        commands.append(result)
        if result.get("status") == "success":
            return {"relation": relation, "command": result}
        if result.get("exit_code") not in {1}:
            raise RuntimeError(result.get("stderr") or "dpkg version comparison failed")
    raise RuntimeError("dpkg could not order Debian versions")


def upstream_version_relation(available_upstream: str, published_debian: str, *, workspace: Path, runner=run_command) -> dict:
    """Compare an available upstream version with a published Debian package version."""
    published_upstream = debian_upstream_version(published_debian)
    result = debian_version_relation(available_upstream, published_upstream, workspace=workspace, runner=runner)
    return {**result, "available_upstream": available_upstream, "published_upstream": published_upstream, "published_debian": published_debian}


def published_versions(rows: list[dict], package: str, architecture: str | None = None) -> list[dict]:
    out = []
    for row in rows:
        if row.get("Package") != package:
            continue
        if architecture and row.get("Architecture") not in {architecture, "all"}:
            continue
        out.append({
            "package": row.get("Package", ""),
            "version": row.get("Version", ""),
            "architecture": row.get("Architecture", ""),
            "filename": row.get("Filename", ""),
            "size": row.get("Size", ""),
            "sha256": row.get("SHA256", ""),
            "description": row.get("Description", ""),
            "depends": row.get("Depends", ""),
            "homepage": row.get("Homepage", ""),
        })
    return out


def parse_reprepro_distributions(text: str) -> dict:
    data = {}
    for raw in (text or "").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        data[key.strip().lower()] = value.strip()
    return {
        "origin": data.get("origin", ""),
        "label": data.get("label", ""),
        "suite": data.get("suite", ""),
        "codename": data.get("codename", ""),
        "version": data.get("version", ""),
        "architectures": data.get("architectures", "").split(),
        "components": data.get("components", "").split(),
        "description": data.get("description", ""),
        "sign_with": data.get("signwith", ""),
    }


def detect_repo_backend(repo_root: Path) -> str:
    repo_root = Path(repo_root)
    if (repo_root / "conf" / "distributions").exists():
        return "reprepro"
    if (repo_root / "dists").exists() or (repo_root / "pool").exists():
        return "manual"
    return "unknown"


def reprepro_config(repo_root: Path) -> dict:
    """Read the active reprepro distribution configuration."""
    path = Path(repo_root) / "conf" / "distributions"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return parse_reprepro_distributions(path.read_text(encoding="utf-8"))


def reprepro_list(repo_root: Path, distribution: str, *, runner=run_command) -> dict:
    root = Path(repo_root).resolve()
    result = runner(
        f"reprepro --basedir {shlex.quote(str(root))} list {shlex.quote(distribution)}",
        workspace=root, working_directory=".", environment=reprepro_environment(), timeout=60,
    )
    rows = []
    for line in result.get("stdout", "").splitlines():
        match = re.fullmatch(r"([^|]+)\|([^|]+)\|([^:]+):\s+(\S+)\s+(\S+)", line.strip())
        if match:
            rows.append(dict(zip(("distribution", "component", "architecture", "package", "version"), match.groups())))
    return {"command": result, "packages": rows}


def reprepro_include_deb(repo_root: Path, distribution: str, deb_path: Path, component: str = "main", *, runner=run_command) -> dict:
    """Publish a verified .deb through the repository's native reprepro database."""
    root = Path(repo_root).resolve()
    command = " ".join(shlex.quote(value) for value in ("reprepro", "--basedir", str(root), "--component", component, "includedeb", distribution, str(Path(deb_path).resolve())))
    result = runner(command, workspace=root, working_directory=".", environment=reprepro_environment(), timeout=120)
    return {"backend": "reprepro", "command": result}
