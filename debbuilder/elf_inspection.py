"""Bounded, distribution-independent inspection of an acquired ELF file.

This module observes ELF metadata. It does not resolve Debian dependencies or
validate that a program will run. The inspected bytes are copied to a private
snapshot before readelf is invoked; the target program is never executed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import selectors
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal


MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024  # stdout and stderr combined
MAX_NEEDED = 128
MAX_SEARCH_PATHS = 64
MAX_VERSION_REQUIREMENTS = 512
MAX_STRING_LENGTH = 1024
READELF_TIMEOUT_SECONDS = 15

ElfKind = Literal["not_elf", "malformed_elf", "elf"]
Linkage = Literal["static", "dynamic", "not_applicable"]


class ElfInspectionError(RuntimeError):
    """An inspection cannot safely complete (distinct from malformed input)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VersionRequirement:
    library: str
    name: str


@dataclass(frozen=True)
class ElfInspectionResult:
    kind: ElfKind
    sha256: str
    size: int
    elf_class: str = ""
    endianness: str = ""
    machine: str = ""
    debian_architecture: str = ""
    elf_type: str = ""
    linkage: Linkage | None = None
    interpreter: str = ""
    soname: str = ""
    needed: tuple[str, ...] = ()
    rpath: tuple[str, ...] = ()
    runpath: tuple[str, ...] = ()
    version_requirements: tuple[VersionRequirement, ...] = ()
    diagnostics: tuple[str, ...] = ()


def planned_resolution_capability(result: ElfInspectionResult, *, distribution: str, architecture: str) -> str:
    """State the approved future resolver scope without resolving anything."""
    if result.kind == "elf" and distribution == "bookworm" and architecture == "amd64" and result.debian_architecture == "amd64":
        return "planned_bookworm_amd64"
    return "unsupported"


def _bounded(value: str) -> str:
    if not value or len(value) > MAX_STRING_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ElfInspectionError("elf_metadata_limit", "ELF metadata contains an invalid or overlong string")
    return value


def _append_bounded(rows: list, value, limit: int) -> None:
    if len(rows) >= limit:
        raise ElfInspectionError("elf_metadata_limit", "ELF metadata entry count exceeds its bound")
    rows.append(value)


def _header(data: bytes) -> tuple[str, str, str, str, str]:
    if len(data) < 20 or data[4] not in (1, 2) or data[5] not in (1, 2) or data[6] != 1:
        raise ValueError("invalid ELF identification")
    elf_class = "ELF32" if data[4] == 1 else "ELF64"
    endianness = "little" if data[5] == 1 else "big"
    prefix = "<" if data[5] == 1 else ">"
    elf_type_num, machine_num = struct.unpack_from(prefix + "HH", data, 16)
    elf_type = {1: "ET_REL", 2: "ET_EXEC", 3: "ET_DYN", 4: "ET_CORE"}.get(elf_type_num, f"ET_{elf_type_num}")
    machine = {62: "EM_X86_64", 183: "EM_AARCH64", 40: "EM_ARM"}.get(machine_num, f"EM_{machine_num}")
    architecture = ""
    if data[4] == 2 and data[5] == 1:
        architecture = {62: "amd64", 183: "arm64"}.get(machine_num, "")
    if machine_num == 40 and data[4] == 1 and data[5] == 1:
        if len(data) >= 40:
            flags = struct.unpack_from(prefix + "I", data, 36)[0]
            if (flags & 0xFF000000) >= 0x05000000 and (flags & 0x400):
                architecture = "armhf"
    return elf_class, endianness, machine, architecture, elf_type


def _readelf(snapshot: BinaryIO, *, executable: str) -> tuple[str, str, int]:
    # The caller owns a private, unlinked snapshot. Only its descriptor is
    # inherited; argv never contains an upstream-controlled path or option.
    descriptor = snapshot.fileno()
    arguments = [executable, "-W", "-h", "-l", "-d", "-V", f"/proc/self/fd/{descriptor}"]
    try:
        process = subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(descriptor,), env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, close_fds=True,
        )
    except OSError as exc:
        raise ElfInspectionError("readelf_unavailable", "readelf could not be started") from exc
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        assert process.stdout is not None and process.stderr is not None
        for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline = time.monotonic() + READELF_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ElfInspectionError("readelf_timeout", "readelf exceeded its time limit")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                output[key.data].extend(chunk)
                if sum(map(len, output.values())) > MAX_OUTPUT_BYTES:
                    raise ElfInspectionError("readelf_output_limit", "readelf output exceeded its size limit")
        try:
            exit_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ElfInspectionError("readelf_timeout", "readelf exceeded its time limit") from exc
        return output["stdout"].decode("utf-8", "replace"), output["stderr"].decode("utf-8", "replace"), exit_code
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def _parse(output: str, sha256: str, size: int, header: tuple[str, str, str, str, str]) -> ElfInspectionResult:
    elf_class, endianness, machine, architecture, elf_type = header
    if not output.startswith("ELF Header:\n") or not re.search(r"^  Class:\s+ELF(?:32|64)$", output, re.M) or not re.search(r"^  Machine:\s+.+$", output, re.M):
        raise ValueError("readelf did not return a complete ELF header")
    if not re.search(r"^  Type:\s+\S+", output, re.M):
        raise ValueError("readelf did not return an ELF type")
    has_dynamic_segment = bool(re.search(r"^\s+DYNAMIC\s+", output, re.M))
    interpreter_match = re.search(r"^\s+\[Requesting program interpreter: (.+)\]$", output, re.M)
    interpreter = _bounded(interpreter_match.group(1)) if interpreter_match else ""
    if re.search(r"^\s+INTERP\s+", output, re.M) and not interpreter:
        raise ValueError("interpreter segment has no readable path")
    needed: list[str] = []
    rpath: list[str] = []
    runpath: list[str] = []
    soname = ""
    for line in output.splitlines():
        found = re.match(r"^\s*0x[0-9a-fA-F]+ \((NEEDED|SONAME|RPATH|RUNPATH)\)\s+[^\n]*?\[([^\]\n]*)\]$", line)
        if not found:
            if re.search(r"\((?:NEEDED|SONAME|RPATH|RUNPATH)\)", line):
                raise ValueError("dynamic entry could not be parsed")
            continue
        tag, value = found.groups()
        if tag == "NEEDED":
            _append_bounded(needed, _bounded(value), MAX_NEEDED)
        elif tag == "SONAME":
            if soname:
                raise ValueError("duplicate SONAME entry")
            soname = _bounded(value)
        else:
            paths = rpath if tag == "RPATH" else runpath
            for component in value.split(":"):
                _append_bounded(paths, _bounded(component), MAX_SEARCH_PATHS)
    requirements: list[VersionRequirement] = []
    in_needs = False
    library = ""
    for line in output.splitlines():
        if line.startswith("Version needs section "):
            in_needs = True
            continue
        if not in_needs:
            continue
        if line and not line[0].isspace():
            in_needs = False
            continue
        found_file = re.search(r"\bFile: (\S+)", line)
        if found_file:
            library = _bounded(found_file.group(1))
        found_name = re.search(r"\bName: (\S+)", line)
        if found_name:
            if not library:
                raise ValueError("version need has no library")
            _append_bounded(requirements, VersionRequirement(library, _bounded(found_name.group(1))), MAX_VERSION_REQUIREMENTS)
    linkage: Linkage = (
        "dynamic" if interpreter or needed or has_dynamic_segment
        else "not_applicable" if elf_type not in {"ET_EXEC", "ET_DYN"}
        else "static"
    )
    return ElfInspectionResult(
        kind="elf", sha256=sha256, size=size, elf_class=elf_class,
        endianness=endianness, machine=machine, debian_architecture=architecture,
        elf_type=elf_type, linkage=linkage,
        interpreter=interpreter, soname=soname, needed=tuple(needed),
        rpath=tuple(rpath), runpath=tuple(runpath), version_requirements=tuple(requirements),
    )


def inspect_elf(path: str | Path, *, expected_sha256: str | None = None) -> ElfInspectionResult:
    """Inspect one acquired regular file and optionally verify its known digest."""
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be a SHA-256 hex digest")
    executable = shutil.which("readelf", path="/usr/bin:/bin")
    target = Path(path)
    if target.is_symlink():
        raise ElfInspectionError("unsafe_elf_file", "ELF inspection requires a regular non-symlink file")
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ElfInspectionError("unsafe_elf_file", "ELF inspection could not open a regular file") from exc
    with os.fdopen(descriptor, "rb") as source:
        initial = os.fstat(source.fileno())
        if not stat.S_ISREG(initial.st_mode):
            raise ElfInspectionError("unsafe_elf_file", "ELF inspection requires a regular file")
        if initial.st_size > MAX_FILE_BYTES:
            raise ElfInspectionError("elf_file_limit", "ELF file exceeds the inspection size limit")
        digest = hashlib.sha256()
        with tempfile.TemporaryFile(prefix="debbuilder-elf-") as snapshot:
            first = b""
            copied = 0
            while True:
                chunk = source.read(min(1024 * 1024, MAX_FILE_BYTES + 1 - copied))
                if not chunk:
                    break
                if not first:
                    first = chunk[:64]
                copied += len(chunk)
                if copied > MAX_FILE_BYTES:
                    raise ElfInspectionError("elf_file_limit", "ELF file exceeds the inspection size limit")
                digest.update(chunk)
                snapshot.write(chunk)
            final = os.fstat(source.fileno())
            if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns
            ) or copied != initial.st_size:
                raise ElfInspectionError("elf_file_changed", "ELF source changed during inspection")
            sha256 = digest.hexdigest()
            if expected_sha256 is not None and not hmac.compare_digest(sha256, expected_sha256.lower()):
                raise ElfInspectionError("elf_identity_mismatch", "ELF bytes differ from the expected acquired identity")
            if not first.startswith(b"\x7fELF"):
                return ElfInspectionResult("not_elf", sha256, copied)
            try:
                header = _header(first)
            except ValueError as exc:
                return ElfInspectionResult("malformed_elf", sha256, copied, diagnostics=(str(exc),))
            if not executable:
                raise ElfInspectionError("readelf_unavailable", "readelf (binutils) is required for ELF inspection")
            snapshot.flush()
            output, stderr, code = _readelf(snapshot, executable=executable)
            if code != 0 or stderr.strip():
                return ElfInspectionResult("malformed_elf", sha256, copied, diagnostics=(stderr.strip()[:MAX_STRING_LENGTH] or "readelf rejected ELF structure",))
            try:
                return _parse(output, sha256, copied, header)
            except ValueError as exc:
                return ElfInspectionResult("malformed_elf", sha256, copied, diagnostics=(str(exc),))
