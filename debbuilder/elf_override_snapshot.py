"""Make inspection-only ELF snapshots that omit explicitly ignored DT_NEEDED tags.

Snapshots are never packaged or executed. Their unchanged requirements are
still resolved by Debian's dpkg-shlibdeps in the admitted target environment.
"""
from __future__ import annotations

import struct
import hashlib
from pathlib import Path

from .elf_dependency_resolution import ResolutionError
from .elf_inspection import MAX_FILE_BYTES, ElfInspectionResult, inspect_elf


def _filtered_snapshot(source: Path, destination: Path, inspection: ElfInspectionResult,
                       ignored: set[str]) -> None:
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ResolutionError("elf_size_limit", "ELF snapshot exceeds inspection size bound")
    data = bytearray(source.read_bytes())
    if hashlib.sha256(data).hexdigest() != inspection.sha256:
        raise ResolutionError("elf_identity_mismatch", "ELF changed since inspection")
    if len(data) != inspection.size or data[:6] != b"\x7fELF\x02\x01":
        raise ResolutionError("invalid_dependency_override", "Only verified amd64 ELF64 snapshots can be filtered")
    ph_offset = struct.unpack_from("<Q", data, 32)[0]
    ph_size, ph_count = struct.unpack_from("<HH", data, 54)
    if ph_size < 56 or ph_count > 128 or ph_offset + ph_count * ph_size > len(data):
        raise ResolutionError("invalid_dependency_override", "ELF program headers are invalid")
    load, dynamic = [], None
    for index in range(ph_count):
        offset = ph_offset + index * ph_size
        kind = struct.unpack_from("<I", data, offset)[0]
        file_offset, address, size = struct.unpack_from("<QQQ", data, offset + 8)[0], struct.unpack_from("<Q", data, offset + 16)[0], struct.unpack_from("<Q", data, offset + 32)[0]
        if file_offset + size > len(data):
            raise ResolutionError("invalid_dependency_override", "ELF segment exceeds the file")
        if kind == 1:
            load.append((file_offset, address, size))
        elif kind == 2:
            if dynamic is not None:
                raise ResolutionError("invalid_dependency_override", "ELF has duplicate dynamic segments")
            dynamic = (file_offset, size)
    if dynamic is None or dynamic[1] % 16:
        raise ResolutionError("invalid_dependency_override", "ELF dynamic segment is invalid")
    entries = []
    for offset in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag, value = struct.unpack_from("<QQ", data, offset)
        if tag == 0:
            break
        entries.append((offset, tag, value))
    tables = [value for _, tag, value in entries if tag == 5]
    sizes = [value for _, tag, value in entries if tag == 10]
    if len(tables) != 1 or len(sizes) != 1:
        raise ResolutionError("invalid_dependency_override", "ELF string table is ambiguous")
    locations = [file_offset + tables[0] - address for file_offset, address, size in load
                 if address <= tables[0] and tables[0] + sizes[0] <= address + size]
    if len(locations) != 1:
        raise ResolutionError("invalid_dependency_override", "ELF string table is outside a unique load segment")
    base = locations[0]
    removed = set()
    for offset, tag, value in entries:
        if tag != 1:
            continue
        if value >= sizes[0]:
            raise ResolutionError("invalid_dependency_override", "ELF needed name is outside its string table")
        end = data.find(0, base + value, base + sizes[0])
        if end < 0:
            raise ResolutionError("invalid_dependency_override", "ELF needed name is unterminated")
        name = data[base + value:end].decode("ascii", "strict")
        if name in ignored:
            # DT_DEBUG is ignored by dpkg-shlibdeps. The snapshot is never run.
            struct.pack_into("<Q", data, offset, 21)
            removed.add(name)
    if removed != ignored:
        raise ResolutionError("invalid_dependency_override", "Ignored SONAME does not match this ELF")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    observed = inspect_elf(destination)
    if observed.kind != "elf" or set(observed.needed) != set(inspection.needed) - ignored:
        raise ResolutionError("invalid_dependency_override", "Filtered ELF snapshot did not retain expected requirements")


def filtered_snapshot(source: Path, destination: Path, inspection: ElfInspectionResult,
                      ignored: set[str]) -> None:
    try:
        _filtered_snapshot(source, destination, inspection, ignored)
    except (struct.error, UnicodeDecodeError, ValueError) as exc:
        raise ResolutionError("invalid_dependency_override", "ELF cannot be safely filtered for an explicit override") from exc
