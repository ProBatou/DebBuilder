# ELF inspection boundary

`debbuilder.elf_inspection.inspect_elf()` accepts one already acquired regular
file and returns bounded, distribution-independent metadata. It copies and
hashes the bytes it inspects, then runs `readelf` on that private snapshot.
The upstream file is never executed. `not_elf` is a normal result;
`malformed_elf` means an ELF magic was present but its structure was invalid.
Missing `readelf`, unsafe input, timeout and bound violations are explicit
inspection errors. `readelf` is supplied by Debian's `binutils` package; this
checkpoint does not alter DebBuilder's own package dependencies.

Current limits: 256 MiB input; 2 MiB combined `readelf` output; 15 seconds;
128 `DT_NEEDED` entries; 64 entries per RPATH/RUNPATH; 512 version needs;
1,024 characters per metadata string. The caller may supply the acquired
SHA-256; inspection verifies it against the private snapshot.

The result keeps ELF type separate from linkage: both a PIE executable and a
shared library can be `ET_DYN`, while a shared library may have `DT_NEEDED`
without an interpreter. RPATH/RUNPATH and `$ORIGIN` are recorded, not resolved.
Version needs are metadata, not package-version constraints.
Relocatable ELF objects (`ET_REL`) have linkage `not_applicable` when they have
no dynamic segment.

The approved future automatic Debian resolver scope is Bookworm/amd64.
`planned_resolution_capability()` reports that scope separately from the ELF
inspection data. It does not perform resolution, and no result obtained from
the Trixie host may be used as a Bookworm package dependency.

The boundaries remain distinct:

1. #30B: `readelf` observes ELF metadata.
2. #30C: Debian metadata and `dpkg-shlibdeps` resolve package relationships.
3. #27: existing APT installation and offline lifecycle validation check the
   final `.deb`.
