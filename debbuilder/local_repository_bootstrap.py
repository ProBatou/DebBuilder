"""Idempotent local repository and signing bootstrap for the packaged service."""
from __future__ import annotations

import os
import hashlib
import html
import re
import secrets
import stat
import subprocess
from collections import Counter
from pathlib import Path

from . import apt_repo
from .repository_lock import RepositoryLockError, pinned_directory, repository_lease, safe_relative_path
from .runtime import native_debian_architecture
from .settings_store import _validate_repo_url, SettingsDocumentError


FINGERPRINT = re.compile(r"(?:[0-9A-F]{40}|[0-9A-F]{64})\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*\Z")
SIGNING_UID = "DebBuilder Repository <repository@debbuilder.invalid>"
RECORD = "repository-signing-fingerprint"
TEMPLATE_ROOT = Path(__file__).parent / "repository_templates"


class LocalRepositoryBootstrapError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str):
    raise LocalRepositoryBootstrapError(code, message)


def _command(arguments: list[str], *, timeout: int = 90, environment: dict | None = None,
             pass_fds: tuple[int, ...] = (), input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(arguments, input=input_data, capture_output=True, timeout=timeout, check=False,
                                env=environment, pass_fds=pass_fds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalRepositoryBootstrapError("bootstrap_command_failed", f"{arguments[0]} could not complete") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()[:400]
        _fail("bootstrap_command_failed", f"{arguments[0]} failed: {message}")
    return result.stdout


def _ensure_directory(path: Path, mode: int, *, private: bool = False) -> None:
    """Create missing path components without accepting a symlink at any level."""
    absolute = Path(os.path.abspath(path))
    current = Path("/")
    for component in absolute.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=mode)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            _fail("bootstrap_path_unsafe", f"Bootstrap directory is not a real directory: {current}")
    info = absolute.lstat()
    if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        _fail("bootstrap_private_directory_unsafe", f"Private bootstrap directory has unsafe ownership or mode: {absolute}")


def _require_owned_unwritable_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        _fail("bootstrap_private_directory_unsafe", f"Bootstrap parent directory is writable by another account: {path}")


def _require_restricted_write_fd(fd: int, label: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        _fail("bootstrap_repository_path_unsafe", f"Repository {label} directory is writable by another account")


def _require_restricted_write_file(directory_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        _fail("bootstrap_repository_path_unsafe", f"Repository file {name} is writable by another account")


def _read_regular(directory_fd: int, name: str, *, limit: int = 1024 * 1024) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocalRepositoryBootstrapError("bootstrap_file_unsafe", f"Cannot safely read {name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            _fail("bootstrap_file_unsafe", f"Bootstrap file {name} is unsafe")
        value = os.read(fd, limit + 1)
        if len(value) > limit:
            _fail("bootstrap_file_unsafe", f"Bootstrap file {name} is too large")
        return value
    finally:
        os.close(fd)


def _atomic_file(directory_fd: int, name: str, content: bytes, mode: int, *, replace: bool = False) -> None:
    existing = _read_regular(directory_fd, name)
    if existing is not None and not replace:
        _fail("bootstrap_file_conflict", f"Existing {name} cannot be overwritten")
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=directory_fd)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        if not replace and _read_regular(directory_fd, name) is not None:
            _fail("bootstrap_file_conflict", f"Existing {name} cannot be overwritten")
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _gpg(home: Path, *arguments: str, timeout: int = 90) -> bytes:
    return _command(["gpg", "--batch", "--no-options", "--homedir", str(home), *arguments], timeout=timeout)


def _public_fingerprints(home: Path, content: bytes) -> list[str]:
    output = _command(["gpg", "--batch", "--no-options", "--homedir", str(home), "--with-colons", "--fingerprint",
                       "--import-options", "show-only", "--import"], input_data=content)
    return [fields[9].upper() for line in output.decode("utf-8", "replace").splitlines()
            if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9]


def _secret_keys(home: Path) -> list[dict]:
    output = _gpg(home, "--with-colons", "--fingerprint", "--list-secret-keys").decode("utf-8", "replace")
    keys: list[dict] = []
    current = None
    for line in output.splitlines():
        fields = line.split(":")
        kind = fields[0]
        if kind == "sec":
            current = {"fingerprint": "", "uids": [], "validity": fields[1], "capabilities": fields[11] if len(fields) > 11 else ""}
            keys.append(current)
        elif kind == "fpr" and current and not current["fingerprint"]:
            current["fingerprint"] = fields[9].upper()
        elif kind == "uid" and current:
            current["uids"].append(fields[9])
    return keys


def _key_for(home: Path, fingerprint: str, *, fresh: bool) -> None:
    keys = _secret_keys(home)
    matches = [row for row in keys if row["fingerprint"] == fingerprint]
    if len(matches) != 1 or matches[0]["validity"] in {"r", "e", "d", "i"} or "s" not in matches[0]["capabilities"].lower():
        _fail("bootstrap_signing_key_invalid", "Configured repository signing key is missing or unusable")
    if fresh and (len(keys) != 1 or SIGNING_UID not in matches[0]["uids"]):
        _fail("bootstrap_signing_key_ambiguous", "Unconfigured signing home contains unexpected keys")


def _identity_from_config(text: str, suite: str, component: str) -> tuple[str, dict]:
    try:
        distribution = apt_repo.select_reprepro_distribution(text, suite)
    except ValueError as exc:
        raise LocalRepositoryBootstrapError("bootstrap_distribution_incompatible", str(exc)) from exc
    fingerprint = distribution["sign_with"].upper()
    if not FINGERPRINT.fullmatch(fingerprint):
        _fail("bootstrap_signing_reference_invalid", "Repository SignWith must be one full fingerprint")
    if component not in distribution["components"] or not distribution["architectures"] or any(
        not TOKEN.fullmatch(value) for value in distribution["architectures"]
    ):
        _fail("bootstrap_distribution_incompatible", "Repository component or architectures differ from local settings")
    if {value.lower() for value in distribution.get("export_options", [])}.intersection({"noexport", "never"}):
        _fail("bootstrap_distribution_incompatible", "Repository distribution disables index export")
    if not TOKEN.fullmatch(distribution["codename"]):
        _fail("bootstrap_distribution_incompatible", "Repository codename is unsafe")
    return fingerprint, distribution


def _verify_release_identity(content: bytes, distribution: dict) -> dict[str, tuple[str, int]]:
    """Check signed Release identity after gpgv has verified its signature."""
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("bootstrap_repository_incompatible", "Signed repository metadata is not UTF-8")
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----\n"):
        _fail("bootstrap_repository_incompatible", "Repository InRelease is not a clearsigned Release")
    _headers, separator, signed = text.partition("\n\n")
    if not separator:
        _fail("bootstrap_repository_incompatible", "Repository InRelease has no signed metadata")
    signed = signed.split("\n-----BEGIN PGP SIGNATURE-----", 1)[0]
    fields = {}
    for line in signed.splitlines():
        if line.startswith((" ", "\t")):
            continue
        key, marker, value = line.partition(":")
        if marker:
            if key in fields:
                _fail("bootstrap_repository_incompatible", "Repository Release identity has duplicate fields")
            fields[key] = value.strip()
    expected = {
        "Codename": distribution["codename"],
        "Components": " ".join(distribution["components"]),
        "Architectures": " ".join(distribution["architectures"]),
    }
    if distribution["suite"]:
        expected["Suite"] = distribution["suite"]
    if any(fields.get(key) != value for key, value in expected.items()):
        _fail("bootstrap_repository_incompatible", "Signed repository identity differs from conf/distributions")
    checksums: dict[str, tuple[str, int]] = {}
    in_sha256 = False
    for line in signed.splitlines():
        if line == "SHA256:":
            in_sha256 = True
            continue
        if not in_sha256:
            continue
        if not line.startswith(" "):
            break
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) or not parts[1].isdigit():
            _fail("bootstrap_repository_incompatible", "Signed repository checksum entry is invalid")
        try:
            relative = safe_relative_path(parts[2]).as_posix()
        except RepositoryLockError:
            _fail("bootstrap_repository_incompatible", "Signed repository checksum path is unsafe")
        if relative in checksums:
            _fail("bootstrap_repository_incompatible", "Signed repository checksum path is duplicated")
        checksums[relative] = (parts[0].lower(), int(parts[1]))
    if not checksums:
        _fail("bootstrap_repository_incompatible", "Signed repository has no SHA256 entries")
    return checksums


def _verify_exported_state(lease, distribution: dict, checksums: dict[str, tuple[str, int]], listed: list[dict]) -> None:
    """Check the signed export, database projection and referenced pool files."""
    codename = distribution["codename"]
    if any(row["distribution"] != codename or row["component"] not in distribution["components"] or
           row["architecture"] not in distribution["architectures"] for row in listed):
        _fail("bootstrap_repository_incomplete", "Repository database has packages outside the signed distribution")
    for relative, (expected_hash, expected_size) in checksums.items():
        try:
            with lease.open_regular(f"dists/{codename}/{relative}", required_prefix="dists") as (fd, info):
                if info.st_size != expected_size:
                    _fail("bootstrap_repository_incomplete", f"Signed repository file has the wrong size: {relative}")
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
        except FileNotFoundError:
            _fail("bootstrap_repository_incomplete", f"Signed repository file is missing: {relative}")
        if digest.hexdigest() != expected_hash:
            _fail("bootstrap_repository_incomplete", f"Signed repository file has the wrong checksum: {relative}")
    for component in distribution["components"]:
        for arch in distribution["architectures"]:
            base = f"{component}/binary-{arch}/Packages"
            if base not in checksums and f"{base}.gz" not in checksums:
                _fail("bootstrap_repository_incomplete", f"Signed repository index is missing: {base}")
            rows = apt_repo.local_packages_index(lease.root, codename, component, arch)
            if rows is None:
                _fail("bootstrap_repository_incomplete", f"Repository index is unreadable: {base}")
            indexed = Counter((row["Package"], row["Version"]) for row in rows)
            database = Counter((row["package"], row["version"]) for row in listed
                               if row["component"] == component and row["architecture"] == arch)
            if indexed != database:
                _fail("bootstrap_repository_incomplete", f"Repository index and database differ: {base}")
    command_root = Path(f"/proc/self/fd/{lease.root_fd}")
    arguments = [*apt_repo._reprepro_layout_arguments(command_root, lease=lease), "checkpool", "fast"]
    _command(arguments, timeout=120, environment={**os.environ, "LC_ALL": "C"}, pass_fds=lease.inherited_fds)


def _native_architecture() -> str:
    value = native_debian_architecture()
    if not TOKEN.fullmatch(value) or value in {"all", "source"}:
        _fail("bootstrap_architecture_invalid", "Host Debian architecture is invalid")
    return value


def _directory_has_entries(lease, name: str) -> bool:
    """Inspect an existing standard-layout directory only after pinning it."""
    directory = lease.root / name
    if not directory.exists() and not directory.is_symlink():
        return False
    lease.pin_directory(name, create=False)
    return bool(os.listdir(lease.directory_path(name)))


def _managed_asset(directory_fd: int, name: str, body: bytes, *, executable: bool = False,
                   check_only: bool = False) -> None:
    prefix = b"<!--" if name == "index.html" else b"#"
    suffix = b" -->\n" if name == "index.html" else b"\n"
    marker = prefix + b" DebBuilder repository asset v1 sha256=" + hashlib.sha256(body).hexdigest().encode() + suffix
    if executable:
        if not body.startswith(b"#!/bin/sh\n"):
            _fail("bootstrap_asset_input_invalid", "Installer template has no shell interpreter")
        content = b"#!/bin/sh\n" + marker + body[len(b"#!/bin/sh\n"):]
    else:
        content = marker + body
    existing = _read_regular(directory_fd, name)
    if existing == content:
        return
    if existing is not None:
        if executable:
            shebang, separator, rest = existing.partition(b"\n")
            if not separator or shebang != b"#!/bin/sh":
                _fail("bootstrap_asset_conflict", f"Existing {name} is not a DebBuilder-owned asset")
            first, separator, previous_body = rest.partition(b"\n")
            previous_body = b"#!/bin/sh\n" + previous_body
        else:
            first, separator, previous_body = existing.partition(b"\n")
        expected = prefix + b" DebBuilder repository asset v1 sha256=" + hashlib.sha256(previous_body).hexdigest().encode() + suffix.rstrip(b"\n")
        if not separator or first != expected:
            _fail("bootstrap_asset_conflict", f"Existing {name} is not a DebBuilder-owned asset")
    if not check_only:
        _atomic_file(directory_fd, name, content, 0o755 if executable else 0o644, replace=existing is not None)


def _render_public_assets(root_fd: int, *, public_url: str, suite: str, component: str, fingerprint: str) -> None:
    try:
        public_url = _validate_repo_url(public_url)
    except SettingsDocumentError as exc:
        raise LocalRepositoryBootstrapError("bootstrap_public_url_invalid", str(exc)) from exc
    if not TOKEN.fullmatch(suite) or not TOKEN.fullmatch(component) or not FINGERPRINT.fullmatch(fingerprint):
        _fail("bootstrap_asset_input_invalid", "Repository asset inputs are invalid")
    if public_url:
        command = f"curl -fsSL {public_url}/install.sh | sudo bash"
        instructions = f"Client setup: <code>{html.escape(command)}</code>"
        configuration = ":"
    else:
        instructions = "Public repository URL is not configured."
        configuration = "echo 'Public repository URL is not configured.' >&2\nexit 1"
    index = (TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")
    index = index.replace("@SUITE@", html.escape(suite)).replace("@COMPONENT@", html.escape(component)).replace("@INSTRUCTIONS@", instructions)
    install = (TEMPLATE_ROOT / "install.sh").read_text(encoding="utf-8")
    for token, value in (("@CONFIGURATION@", configuration), ("@REPOSITORY_URL@", public_url),
                         ("@SUITE@", suite), ("@COMPONENT@", component), ("@FINGERPRINT@", fingerprint)):
        install = install.replace(token, value)
    assets = (("index.html", index.encode("utf-8"), False),
              ("install.sh", install.encode("utf-8"), True))
    for name, body, executable in assets:
        _managed_asset(root_fd, name, body, executable=executable, check_only=True)
    for name, body, executable in assets:
        _managed_asset(root_fd, name, body, executable=executable)


def bootstrap_repository(*, repository_root: Path, data_root: Path, suite: str, component: str,
                         gpg_home: Path | None = None, public_url: str = "") -> dict:
    """Prepare only missing local state; existing identity and database remain authoritative."""
    if not TOKEN.fullmatch(suite) or not TOKEN.fullmatch(component):
        _fail("bootstrap_distribution_invalid", "Repository suite and component must be safe tokens")
    # Collapse dot segments before comparing containment; Path.absolute() alone
    # preserves '..' and could hide a private path below the public pool.
    repository_root = Path(os.path.abspath(repository_root))
    data_root = Path(os.path.abspath(data_root))
    gpg_home = Path(os.path.abspath(gpg_home or data_root / ".gnupg"))
    if repository_root == data_root or repository_root in data_root.parents:
        _fail("bootstrap_private_directory_unsafe", "Application data must be outside the repository root")
    if repository_root == gpg_home or repository_root in gpg_home.parents:
        _fail("bootstrap_private_directory_unsafe", "GnuPG home must be outside the repository root")
    _ensure_directory(data_root, 0o700)
    _ensure_directory(repository_root, 0o751)
    _ensure_directory(gpg_home, 0o700, private=True)
    _require_owned_unwritable_directory(data_root)
    _require_owned_unwritable_directory(repository_root.parent)
    _require_owned_unwritable_directory(gpg_home.parent)
    try:
        with repository_lease(repository_root, operation="local-bootstrap", blocking=True) as lease:
            root_info = os.fstat(lease.root_fd)
            if root_info.st_uid != os.geteuid() or root_info.st_mode & 0o022:
                _fail("bootstrap_repository_root_unsafe", "Repository root is writable by another account")
            # The conf directory itself is pinned before any config read or write.
            lease.pin_directory("conf", create=True)
            conf_fd = lease.directory_fds["conf"]
            # Existing operator-managed repositories may have a non-root owner.
            _require_restricted_write_fd(conf_fd, "conf")
            options = _read_regular(conf_fd, "options")
            if options is not None:
                _require_restricted_write_file(conf_fd, "options")
                from .artifact_publication import UNSUPPORTED_OPTION_NAMES, _option_name
                if any(_option_name(line) in UNSUPPORTED_OPTION_NAMES for line in options.decode("utf-8", "strict").splitlines()):
                    _fail("bootstrap_repository_options_invalid", "Repository path overrides or hooks are unsupported")
            config = _read_regular(conf_fd, "distributions")
            fresh = config is None
            if not fresh:
                _require_restricted_write_file(conf_fd, "distributions")
            if fresh and any(_directory_has_entries(lease, name) for name in ("db", "pool", "dists")):
                _fail("bootstrap_repository_incompatible", "Repository data exists without a distribution configuration")
            with pinned_directory(data_root) as (_data, data_fd):
                recorded = _read_regular(data_fd, RECORD, limit=256)
                if recorded is not None:
                    record_info = os.stat(RECORD, dir_fd=data_fd, follow_symlinks=False)
                    if record_info.st_uid != os.geteuid() or record_info.st_mode & 0o077:
                        _fail("bootstrap_signing_record_invalid", "Signing fingerprint record is not owner-only")
                    try:
                        recorded_fingerprint = recorded.decode("ascii").strip().upper()
                    except UnicodeDecodeError:
                        _fail("bootstrap_signing_record_invalid", "Signing fingerprint record is invalid")
                    if not FINGERPRINT.fullmatch(recorded_fingerprint):
                        _fail("bootstrap_signing_record_invalid", "Signing fingerprint record is invalid")
                else:
                    recorded_fingerprint = ""
                if fresh:
                    if recorded_fingerprint:
                        _fail("bootstrap_repository_incomplete", "Signing record exists without repository configuration")
                    keys = _secret_keys(gpg_home)
                    if not keys:
                        _gpg(gpg_home, "--pinentry-mode", "loopback", "--passphrase", "",
                             "--quick-generate-key", SIGNING_UID, "rsa3072", "sign", "0", timeout=180)
                        keys = _secret_keys(gpg_home)
                    if len(keys) != 1 or SIGNING_UID not in keys[0]["uids"]:
                        _fail("bootstrap_signing_key_ambiguous", "Unconfigured signing home contains unexpected keys")
                    fingerprint = keys[0]["fingerprint"]
                    if not FINGERPRINT.fullmatch(fingerprint):
                        _fail("bootstrap_signing_key_invalid", "Generated key has no full fingerprint")
                    _key_for(gpg_home, fingerprint, fresh=True)
                    arch = _native_architecture()
                    codename = suite
                    content = (f"Origin: DebBuilder\nLabel: DebBuilder\nSuite: {suite}\nCodename: {suite}\n"
                               f"Architectures: {arch}\nComponents: {component}\nSignWith: {fingerprint}\n").encode("ascii")
                    _atomic_file(conf_fd, "distributions", content, 0o600)
                    fingerprint, distribution = _identity_from_config(content.decode("ascii"), suite, component)
                else:
                    try:
                        fingerprint, distribution = _identity_from_config(config.decode("utf-8", "strict"), suite, component)
                    except UnicodeDecodeError as exc:
                        raise LocalRepositoryBootstrapError("bootstrap_distribution_incompatible", "Repository config is not UTF-8") from exc
                    if recorded_fingerprint and fingerprint != recorded_fingerprint:
                        _fail("bootstrap_signing_record_conflict", "Signing record and repository config disagree")
                    _key_for(gpg_home, fingerprint, fresh=False)
                codename = distribution["codename"]
                if not recorded_fingerprint:
                    _atomic_file(data_fd, RECORD, (fingerprint + "\n").encode("ascii"), 0o600)

            public = _gpg(gpg_home, "--export", fingerprint)
            if not public or len(public) > 1024 * 1024:
                _fail("bootstrap_public_key_invalid", "Public signing key export is empty or too large")
            existing_public = _read_regular(lease.root_fd, "repository.gpg")
            if existing_public is None:
                _atomic_file(lease.root_fd, "repository.gpg", public, 0o644)
            elif existing_public != public:
                if _public_fingerprints(gpg_home, existing_public) != _public_fingerprints(gpg_home, public):
                    _fail("bootstrap_public_key_conflict", "Existing public signing key differs from selected identity")
                _atomic_file(lease.root_fd, "repository.gpg", public, 0o644, replace=True)
            _require_restricted_write_file(lease.root_fd, "repository.gpg")

            # A missing initial export is recoverable only when no database/pool state exists.
            try:
                with lease.open_regular(f"dists/{codename}/InRelease") as (fd, _info):
                    inrelease = os.read(fd, 1024 * 1024)
            except FileNotFoundError:
                inrelease = None
            if inrelease is None:
                if _directory_has_entries(lease, "pool"):
                    _fail("bootstrap_repository_incomplete", "Repository indices are missing but package pool is populated")
                lease.pin_standard_layout()
                for name in ("db", "dists", "pool", "lists", "logs", "morgue"):
                    _require_restricted_write_fd(lease.directory_fds[name], name)
                listed = apt_repo.reprepro_list(repository_root, codename, lease=lease)
                if listed["command"].get("status") != "success" or listed["packages"]:
                    _fail("bootstrap_repository_incomplete", "Repository indices are missing and database is not verifiably empty")
                command_root = Path(f"/proc/self/fd/{lease.root_fd}")
                arguments = [*apt_repo._reprepro_layout_arguments(command_root, lease=lease), "export", codename]
                environment = {**os.environ, "LC_ALL": "C", "GNUPGHOME": str(gpg_home)}
                _command(arguments, timeout=120, environment=environment, pass_fds=lease.inherited_fds)
                try:
                    with lease.open_regular(f"dists/{codename}/InRelease") as (fd, _info):
                        inrelease = os.read(fd, 1024 * 1024)
                except FileNotFoundError:
                    inrelease = None
            if not inrelease:
                _fail("bootstrap_repository_unsigned", "Signed repository InRelease is missing")
            if not _directory_has_entries(lease, "db"):
                _fail("bootstrap_repository_incomplete", "Signed repository exists without its database")
            lease.pin_standard_layout()
            for name in ("db", "dists", "pool", "lists", "logs", "morgue"):
                _require_restricted_write_fd(lease.directory_fds[name], name)
            _command(["gpgv", "--keyring", str(repository_root / "repository.gpg"), "-"],
                     timeout=30, input_data=inrelease)
            checksums = _verify_release_identity(inrelease, distribution)
            listed = apt_repo.reprepro_list(repository_root, codename, lease=lease)
            if listed["command"].get("status") != "success" or (_directory_has_entries(lease, "pool") and not listed["packages"]):
                _fail("bootstrap_repository_incomplete", "Repository database does not match available package state")
            _verify_exported_state(lease, distribution, checksums, listed["packages"])
            _render_public_assets(lease.root_fd, public_url=public_url, suite=suite, component=component,
                                  fingerprint=fingerprint)
            return {"repository": str(repository_root), "suite": suite, "component": component,
                    "codename": codename, "fingerprint": fingerprint, "ready": True}
    except RepositoryLockError as exc:
        raise LocalRepositoryBootstrapError(exc.code, str(exc)) from exc
