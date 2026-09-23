"""APT repository parsing and reprepro publication helpers."""

from __future__ import annotations

import gzip
import io
import os
import re
import shlex
import stat
from pathlib import Path

from .command_runner import run_command
from .repository_lock import RepositoryLockError, pinned_directory, safe_relative_path


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
        if raw.startswith((" ", "\t")) and last_key:
            cur[last_key] += "\n" + raw[1:]
            continue
        if ": " in raw:
            key, value = raw.split(": ", 1)
            if key in cur:
                raise ValueError(f"duplicate Packages field: {key}")
            cur[key] = value
            last_key = key
            continue
        raise ValueError("malformed Packages index line")
    return rows


MAX_PACKAGES_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PACKAGES_INDEX_BYTES = 32 * 1024 * 1024
_INDEX_MISSING = object()


def _read_pinned_index_file(root_fd: int, relative: str, *, compressed: bool):
    """Read one stable regular index file through a root-pinned no-follow walk."""
    path = safe_relative_path(relative, required_prefix="dists")
    parent_fd = os.dup(root_fd)
    descriptor = -1
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = child
        descriptor = os.open(
            path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        maximum = MAX_PACKAGES_COMPRESSED_BYTES if compressed else MAX_PACKAGES_INDEX_BYTES
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            return None
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        revision = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        rooted_parent = os.dup(root_fd)
        try:
            for part in path.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=rooted_parent,
                )
                os.close(rooted_parent)
                rooted_parent = child
            rooted_current = os.stat(path.parts[-1], dir_fd=rooted_parent, follow_symlinks=False)
        finally:
            os.close(rooted_parent)
        if (
            len(payload) > maximum
            or revision(before) != revision(after)
            or revision(after) != revision(current)
            or revision(after) != revision(rooted_current)
        ):
            return None
        return payload
    except FileNotFoundError:
        # Absence before the target is opened is an ordinary missing index.
        # Disappearance during the post-read rooted identity checks is a
        # publication transition and must not fall back to another index.
        return _INDEX_MISSING if descriptor < 0 else None
    except (OSError, RepositoryLockError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _bounded_gzip_decompress(payload: bytes) -> bytes | None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            decoded = archive.read(MAX_PACKAGES_INDEX_BYTES + 1)
    except (OSError, EOFError):
        return None
    return decoded if len(decoded) <= MAX_PACKAGES_INDEX_BYTES else None


def local_packages_index(repo_root: Path, distribution: str, component: str, architecture: str) -> list[dict] | None:
    """Project a stable bounded exported index without taking the publication lock."""
    try:
        relative_base = safe_relative_path(
            f"dists/{distribution}/{component}/binary-{architecture}", required_prefix="dists",
        ).as_posix()
        with pinned_directory(repo_root) as (_root, root_fd):
            compressed = _read_pinned_index_file(root_fd, f"{relative_base}/Packages.gz", compressed=True)
            if compressed is _INDEX_MISSING:
                payload = _read_pinned_index_file(root_fd, f"{relative_base}/Packages", compressed=False)
            elif compressed is not None:
                payload = _bounded_gzip_decompress(compressed)
            else:
                return None
    except (OSError, RepositoryLockError):
        return None
    if payload is None or payload is _INDEX_MISSING:
        return None
    try:
        text = payload.decode("utf-8")
        rows = parse_packages_index(text)
        if text.strip() and not rows:
            return None
        if any(not all(row.get(field) for field in ("Package", "Version", "Architecture")) for row in rows):
            return None
        return rows
    except (UnicodeError, ValueError, TypeError):
        return None


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


def _distribution_config(data: dict[str, str]) -> dict:
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
        "export_options": data.get("exportoptions", "").split(),
    }


def parse_reprepro_distribution_stanzas(text: str) -> list[dict]:
    """Parse every conf/distributions paragraph without merging identities."""
    stanzas: list[dict] = []
    current: dict[str, str] = {}
    last_key = ""
    for raw in (text or "").splitlines() + [""]:
        if not raw.strip():
            if current:
                stanzas.append(_distribution_config(current))
                current = {}
                last_key = ""
            continue
        if raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) and last_key:
            current[last_key] += " " + raw.strip()
            continue
        if ":" in raw:
            key, value = raw.split(":", 1)
            last_key = key.strip().lower()
            current[last_key] = value.strip()
    return stanzas


def select_reprepro_distribution(text: str, requested: str) -> dict:
    matches = [
        row for row in parse_reprepro_distribution_stanzas(text)
        if requested in {row.get("codename"), row.get("suite")}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"reprepro distribution {requested!r} must match exactly one configured codename or suite"
        )
    return matches[0]


def _reprepro_layout_arguments(root: Path, *, lease=None) -> tuple[str, ...]:
    """Override mutable path options with DebBuilder's supported local layout."""
    paths = {
        name: lease.directory_path(name) if lease is not None else root / name
        for name in ("conf", "db", "dists", "lists", "logs", "morgue")
    }
    return (
        "reprepro", "--basedir", str(root), "--outdir", str(root),
        "--confdir", str(paths["conf"]), "--dbdir", str(paths["db"]),
        "--distdir", str(paths["dists"]), "--listdir", str(paths["lists"]),
        "--logdir", str(paths["logs"]), "--morguedir", str(paths["morgue"]),
        "--waitforlock", "0",
    )


def reprepro_list(repo_root: Path, distribution: str, *, component: str = "", package: str = "", lease=None, runner=run_command) -> dict:
    root = Path(repo_root).absolute()
    inherited = ()
    command_root = root
    if lease is not None:
        lease.require_active()
        if root != lease.root:
            raise ValueError("reprepro root does not match the active repository lease")
        inherited = lease.inherited_fds
        command_root = Path(f"/proc/self/fd/{lease.root_fd}")
    arguments = [*_reprepro_layout_arguments(command_root, lease=lease)]
    if component:
        arguments.extend(("--component", component))
    arguments.extend(("list", distribution))
    if package:
        arguments.append(package)
    result = runner(
        " ".join(shlex.quote(value) for value in arguments),
        workspace=command_root, working_directory=".", environment=reprepro_environment(), timeout=60,
        pass_fds=inherited,
    )
    rows = []
    for line in result.get("stdout", "").splitlines():
        match = re.fullmatch(r"([^|]+)\|([^|]+)\|([^:]+):\s+(\S+)\s+(\S+)", line.strip())
        if match:
            rows.append(dict(zip(("distribution", "component", "architecture", "package", "version"), match.groups())))
    return {"command": result, "packages": rows}


def reprepro_include_deb(repo_root: Path, distribution: str, deb_path: Path, component: str = "main", *, lease, source_fd: int, runner=run_command) -> dict:
    """Publish a verified .deb through the repository's native reprepro database."""
    lease.require_active()
    root = Path(repo_root).absolute()
    if root != lease.root:
        raise ValueError("reprepro root does not match the active repository lease")
    command_root = Path(f"/proc/self/fd/{lease.root_fd}")
    if isinstance(source_fd, bool) or not isinstance(source_fd, int) or source_fd < 0:
        raise ValueError("a pinned source artifact descriptor is required")
    source = f"/proc/self/fd/{source_fd}"
    inherited = lease.inherited_fds + (source_fd,)
    arguments = (
        *_reprepro_layout_arguments(command_root, lease=lease), "--ignore=extension",
        "--component", component, "includedeb", distribution, source,
    )
    command = " ".join(shlex.quote(value) for value in arguments)
    result = runner(
        command, workspace=command_root, working_directory=".", environment=reprepro_environment(),
        timeout=120, pass_fds=inherited,
    )
    return {"backend": "reprepro", "command": result}
