"""Cryptographic repository-key inspection and deterministic APT configuration."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path

from .command_runner import run_command
from .runtime_apt_repositories import normalize_runtime_apt_repositories


MAX_GPG_OUTPUT_BYTES = 256 * 1024
FINGERPRINT = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
INVALID_VALIDITY = frozenset({"r", "e", "d", "i"})


class RepositoryTrustError(ValueError):
    def __init__(self, code: str, message: str, *, repository_id: str = ""):
        super().__init__(message)
        self.code = code
        self.repository_id = repository_id


def _command(arguments: list[str], *, workspace: Path, runner, timeout: float = 30) -> dict:
    bounded_arguments = [
        sys.executable, str(Path(__file__).with_name("bounded_process.py")),
        "--limit", str(MAX_GPG_OUTPUT_BYTES), "--", *arguments,
    ]
    result = runner(
        " ".join(shlex.quote(str(value)) for value in bounded_arguments),
        workspace=workspace, working_directory=".", environment={"LC_ALL": "C"},
        timeout=timeout, inactivity_timeout=timeout,
    )
    if result.get("status") != "success":
        raise RepositoryTrustError(
            "repository_signing_key_invalid",
            str(result.get("stderr") or "OpenPGP key inspection failed")[:1000],
        )
    output_size = len(str(result.get("stdout") or "").encode()) + len(str(result.get("stderr") or "").encode())
    if output_size > MAX_GPG_OUTPUT_BYTES:
        raise RepositoryTrustError("repository_signing_key_invalid", "OpenPGP key inspection output exceeded its bound")
    return result


def _unescape_colon(value: str) -> str:
    return re.sub(r"\\x([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), value)


def _usable_signing_key(row: list[str], *, now: int) -> bool:
    validity = row[1].lower() if len(row) > 1 else ""
    # GnuPG uses lowercase for this key packet's capability and uppercase for
    # aggregate usable capabilities inherited from subkeys.
    capabilities = row[11] if len(row) > 11 else ""
    try:
        expiry = int(row[6]) if len(row) > 6 and row[6] else 0
    except ValueError:
        return False
    return validity not in INVALID_VALIDITY and "s" in capabilities and (expiry == 0 or expiry > now)


def _valid_certificate(row: list[str], *, now: int) -> bool:
    validity = row[1].lower() if len(row) > 1 else ""
    try:
        expiry = int(row[6]) if len(row) > 6 and row[6] else 0
    except ValueError:
        return False
    return validity not in INVALID_VALIDITY and (expiry == 0 or expiry > now)


def inspect_public_key(
    armored: str,
    *,
    workspace: str | Path,
    output_keyring: str | Path,
    runner=run_command,
    now: int | None = None,
) -> dict:
    """Inspect one public certificate without touching operator keyrings."""
    workspace = Path(workspace).resolve()
    output_keyring = Path(output_keyring).resolve()
    output_keyring.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="debbuilder-key-", dir=workspace) as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        key_path = root / "repository.asc"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            os.write(descriptor, armored.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        result = _command([
            "gpg", "--batch", "--no-options", "--homedir", str(root), "--with-colons",
            "--import-options", "show-only", "--dry-run", "--import", str(key_path),
        ], workspace=workspace, runner=runner)
        rows = [line.split(":") for line in str(result.get("stdout") or "").splitlines() if line]
        if any(row[0] in {"sec", "ssb"} for row in rows):
            raise RepositoryTrustError("repository_private_key_rejected", "Repository signing material contains a private key")
        primary_indexes = [index for index, row in enumerate(rows) if row[0] == "pub"]
        if len(primary_indexes) != 1:
            raise RepositoryTrustError(
                "repository_signing_certificate_ambiguous",
                "Repository signing key must contain exactly one primary public certificate",
            )
        primary_index = primary_indexes[0]
        epoch = int(time.time()) if now is None else now
        if not _valid_certificate(rows[primary_index], now=epoch):
            raise RepositoryTrustError("repository_signing_key_invalid", "Primary OpenPGP certificate is revoked, disabled, invalid, or expired")
        primary_fingerprint = ""
        signing_fingerprints: list[str] = []
        active_kind = ""
        active_usable = False
        for row in rows[primary_index:]:
            kind = row[0]
            if kind == "pub":
                active_kind, active_usable = "pub", _usable_signing_key(row, now=epoch)
            elif kind == "sub":
                active_kind, active_usable = "sub", _usable_signing_key(row, now=epoch)
            elif kind == "fpr" and active_kind:
                candidate = (row[9] if len(row) > 9 else "").upper()
                if not FINGERPRINT.fullmatch(candidate):
                    raise RepositoryTrustError("repository_signing_key_invalid", "OpenPGP key fingerprint is malformed")
                if active_kind == "pub" and not primary_fingerprint:
                    primary_fingerprint = candidate
                if active_usable and candidate not in signing_fingerprints:
                    signing_fingerprints.append(candidate)
                active_kind = ""
        if not primary_fingerprint:
            raise RepositoryTrustError("repository_signing_key_invalid", "Primary OpenPGP fingerprint is absent")
        if not signing_fingerprints:
            raise RepositoryTrustError("repository_signing_key_invalid", "Repository certificate has no currently valid signing key")

        output_keyring.unlink(missing_ok=True)
        _command([
            "gpg", "--batch", "--yes", "--no-options", "--homedir", str(root),
            "--dearmor", "--output", str(output_keyring), str(key_path),
        ], workspace=workspace, runner=runner)
    info = output_keyring.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0 or info.st_size > 1024 * 1024:
        output_keyring.unlink(missing_ok=True)
        raise RepositoryTrustError("repository_signing_key_invalid", "Generated repository keyring is not a bounded regular file")
    output_keyring.chmod(0o400)
    return {
        "content_sha256": hashlib.sha256(armored.encode("ascii")).hexdigest(),
        "primary_fingerprint": primary_fingerprint,
        "signing_fingerprints": signing_fingerprints,
    }


def deb822_source(repository: dict, *, signed_by: str) -> str:
    """Render only normalized values; callers choose an attempt-private key path."""
    normalized = normalize_runtime_apt_repositories([repository])[0]
    if not signed_by.startswith("/etc/apt/keyrings/debbuilder-") or not signed_by.endswith(".gpg"):
        raise ValueError("Signed-By path must be an attempt-private DebBuilder keyring")
    return "\n".join((
        "Types: deb",
        f"URIs: {normalized['uri']}",
        f"Suites: {normalized['suite']}",
        f"Components: {' '.join(normalized['components'])}",
        f"Signed-By: {signed_by}",
        "",
    ))


def apt_configuration(*, ca_info: str | None = None) -> str:
    """Attempt-local APT state with an explicit no-redirect acquisition policy."""
    if ca_info not in {None, "/debbuilder-input/test-ca.crt"}:
        raise ValueError("APT CA override is reserved for the controlled integration fixture")
    rows = [
        'Acquire::http::AllowRedirect "false";',
        'Acquire::https::AllowRedirect "false";',
        'Acquire::AllowInsecureRepositories "false";',
        'Acquire::AllowDowngradeToInsecureRepositories "false";',
        'APT::Get::Assume-Yes "true";',
        'APT::Get::AllowUnauthenticated "false";',
        'APT::Sandbox::User "root";',
        'Dir::Etc::Parts "/debbuilder-empty-apt-conf";',
        'Dir::State::lists "/prep/lists";',
        'Dir::Cache::archives "/prep/archives";',
        'Dir::Log "/prep/logs";',
    ]
    if ca_info:
        rows.append(f'Acquire::https::CaInfo "{ca_info}";')
    rows.append('')
    return "\n".join(rows)
