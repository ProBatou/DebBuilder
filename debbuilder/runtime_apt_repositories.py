"""Canonical declaration contract for public runtime APT repositories.

CP1A validates the durable Recipe declaration only. Cryptographic packet and
fingerprint inspection is deliberately deferred to the dependency-preparation
checkpoint; this module performs no network or process execution.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


MAX_REPOSITORIES = 32
MAX_REPOSITORY_ID_LENGTH = 64
MAX_URI_LENGTH = 2048
MAX_SUITE_LENGTH = 128
MAX_COMPONENTS = 16
MAX_COMPONENT_LENGTH = 64
MAX_ARMORED_KEY_LENGTH = 65_536

REPOSITORY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SUITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
ARMOR_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
ARMOR_END = "-----END PGP PUBLIC KEY BLOCK-----"
PRIVATE_ARMOR = "-----BEGIN PGP PRIVATE KEY BLOCK-----"
BASE64_LINE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
ARMOR_HEADER = re.compile(r"^[A-Za-z0-9-]+: [\x20-\x7e]*$")
DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _required_string(value, what: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{what} must be a non-empty string without surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"{what} exceeds the maximum length of {maximum}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{what} must not contain control characters")
    return value


def normalize_repository_id(value, what: str = "runtime APT repository id") -> str:
    identifier = _required_string(value, what, MAX_REPOSITORY_ID_LENGTH)
    if not REPOSITORY_ID.fullmatch(identifier):
        raise ValueError(f"{what} must match {REPOSITORY_ID.pattern}")
    return identifier


def normalize_repository_uri(value) -> str:
    uri = _required_string(value, "runtime APT repository uri", MAX_URI_LENGTH)
    if "?" in uri:
        raise ValueError("runtime APT repository uri must not contain a query")
    if "#" in uri:
        raise ValueError("runtime APT repository uri must not contain a fragment")
    if any(character.isspace() for character in uri) or "\\" in uri:
        raise ValueError("runtime APT repository uri contains unsafe characters")
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("runtime APT repository uri is malformed") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname:
        raise ValueError("runtime APT repository uri must be an absolute HTTPS URI")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("runtime APT repository uri must not contain credentials")
    if "%" in parsed.netloc:
        raise ValueError("runtime APT repository authority must not be percent-encoded")
    try:
        hostname = parsed.hostname.encode("ascii").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("runtime APT repository hostname must use ASCII form") from exc
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("runtime APT repository hostname is invalid")
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError as exc:
            raise ValueError("runtime APT repository hostname is invalid") from exc
        if not parsed.netloc.startswith("["):
            raise ValueError("runtime APT repository IPv6 hostname must be bracketed")
        authority = f"[{hostname}]"
    else:
        dns_name = hostname[:-1] if hostname.endswith(".") else hostname
        if not dns_name or len(dns_name) > 253 or any(
            not DNS_LABEL.fullmatch(label) for label in dns_name.split(".")
        ):
            raise ValueError("runtime APT repository hostname is invalid")
        authority = hostname
    raw_authority = parsed.netloc
    if raw_authority.endswith(":"):
        raise ValueError("runtime APT repository authority contains an empty port")
    if port is not None:
        authority += f":{port}"
    return urlunsplit(("https", authority, parsed.path, "", ""))


def normalize_suite(value) -> str:
    suite = _required_string(value, "runtime APT repository suite", MAX_SUITE_LENGTH)
    if not SUITE.fullmatch(suite) or suite in {".", ".."}:
        raise ValueError("runtime APT repository suite is invalid")
    return suite


def normalize_components(value) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("runtime APT repository components must be a non-empty list")
    if len(value) > MAX_COMPONENTS:
        raise ValueError(f"runtime APT repository components exceed the maximum count of {MAX_COMPONENTS}")
    normalized: list[str] = []
    seen: set[str] = set()
    for component_value in value:
        component = _required_string(
            component_value, "runtime APT repository component", MAX_COMPONENT_LENGTH,
        )
        if not COMPONENT.fullmatch(component) or component in {".", ".."}:
            raise ValueError("runtime APT repository component is invalid")
        if component in seen:
            raise ValueError(f"duplicate runtime APT repository component: {component}")
        seen.add(component)
        normalized.append(component)
    return normalized


def normalize_armored_public_key(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime APT repository signing_key.armored must be non-empty")
    if len(value) > MAX_ARMORED_KEY_LENGTH:
        raise ValueError(
            f"runtime APT repository signing_key.armored exceeds {MAX_ARMORED_KEY_LENGTH} characters",
        )
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("runtime APT repository signing_key.armored must be ASCII") from exc
    canonical = value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if PRIVATE_ARMOR in canonical:
        raise ValueError("runtime APT repository signing key must not contain private-key armor")
    lines = canonical[:-1].split("\n")
    if (
        len(lines) < 4
        or lines[0] != ARMOR_BEGIN
        or lines[-1] != ARMOR_END
        or lines.count(ARMOR_BEGIN) != 1
        or lines.count(ARMOR_END) != 1
        or any("-----BEGIN " in line for line in lines[1:])
        or any("-----END " in line for line in lines[:-1])
    ):
        raise ValueError("runtime APT repository signing key must be one ASCII-armored PGP public-key block")
    if any((ord(character) < 32 and character != "\n") or ord(character) == 127 for character in canonical):
        raise ValueError("runtime APT repository signing key contains control characters")

    body = lines[1:-1]
    separator = next((index for index, line in enumerate(body) if line == ""), None)
    if separator is None:
        raise ValueError("runtime APT repository signing key armor lacks its header separator")
    if any(not ARMOR_HEADER.fullmatch(line) for line in body[:separator]):
        raise ValueError("runtime APT repository signing key contains an invalid armor header")
    payload_lines = body[separator + 1:]
    if payload_lines and payload_lines[-1].startswith("="):
        checksum = payload_lines.pop()
        if not re.fullmatch(r"=[A-Za-z0-9+/]{4}", checksum):
            raise ValueError("runtime APT repository signing key contains an invalid armor checksum")
    if not payload_lines or any(
        not line or len(line) > 76 or not BASE64_LINE.fullmatch(line)
        for line in payload_lines
    ):
        raise ValueError("runtime APT repository signing key contains invalid armored payload data")
    try:
        decoded = base64.b64decode("".join(payload_lines), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("runtime APT repository signing key contains invalid base64 data") from exc
    if len(decoded) < 16:
        raise ValueError("runtime APT repository signing key payload is too short")
    return canonical


def normalize_runtime_apt_repositories(value) -> list[dict]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError("runtime_apt_repositories must be a list")
    if len(value) > MAX_REPOSITORIES:
        raise ValueError(f"runtime_apt_repositories exceeds the maximum count of {MAX_REPOSITORIES}")
    normalized: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"runtime_apt_repositories[{index}] must be an object")
        expected = {"id", "uri", "suite", "components", "signing_key"}
        if set(row) != expected:
            raise ValueError(f"runtime_apt_repositories[{index}] must contain only {sorted(expected)}")
        identifier = normalize_repository_id(row["id"])
        if identifier in seen:
            raise ValueError(f"duplicate runtime APT repository id: {identifier}")
        signing_key = row["signing_key"]
        if not isinstance(signing_key, dict) or set(signing_key) != {"armored"}:
            raise ValueError(
                f"runtime_apt_repositories[{index}].signing_key must contain only armored",
            )
        seen.add(identifier)
        normalized.append({
            "id": identifier,
            "uri": normalize_repository_uri(row["uri"]),
            "suite": normalize_suite(row["suite"]),
            "components": normalize_components(row["components"]),
            "signing_key": {"armored": normalize_armored_public_key(signing_key["armored"])},
        })
    return normalized
