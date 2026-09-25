# ELF inspection boundary

`debbuilder.elf_inspection.inspect_elf()` accepts one already acquired regular
file and returns bounded, distribution-independent metadata. It copies and
hashes the bytes it inspects, then runs `readelf` on that private snapshot.
The upstream file is never executed. `not_elf` is a normal result;
`malformed_elf` means an ELF magic was present but its structure was invalid.
Missing `readelf`, unsafe input, timeout and bound violations are explicit
inspection errors. `readelf` is supplied by Debian's `binutils` package, now
an explicit DebBuilder package runtime dependency.

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

`planned_resolution_capability()` preserves the #30B observation contract. It
does not perform resolution. #30C adds the separate
`debbuilder.elf_dependency_resolution.resolve_elf_dependencies()` engine for
Bookworm/amd64 only; another target returns `unsupported` with no proposal.

The #30C caller supplies the **final** staging tree, installed absolute paths
and their #30B inspection results, plus a prepared Bookworm/amd64 environment.
The staging tree must include `DEBIAN/control`. The production adapter accepts
only an existing, running `OwnedContainer` with the admitted Bookworm/amd64
image digest and the existing offline `lifecycle` role. Its read-only
mounts include the staging tree at `/debbuilder-staging`, a small work
directory with `debian/control` at `/debbuilder-elf-work`, and #27's
`bounded_process.py` at `/debbuilder-input/bounded_process.py`. The adapter
uses the existing Podman lifecycle and cleanup; it does not create a second
container manager. No host dpkg database enters the resolver's result.

The admitted Validation image does **not** contain `dpkg-shlibdeps`. #30D
uses #27's explicit APT preparation to supply Bookworm `dpkg-dev` and
`binutils` to an offline owned lifecycle container. Debian's package database
in that container confirms `/usr/bin/dpkg-shlibdeps` belongs to `dpkg-dev`.
The admitted image digest is unchanged. Resolution never downloads packages,
and the Trixie host dpkg database is never consulted.

For each dynamic ELF, the engine runs `dpkg-shlibdeps -v -O -S<staging>
-e<installed-file>` inside the target environment. The work directory holds
`debian/control`, as required by `dpkg-shlibdeps`. Its bounded output supplies
the Debian `shlibs:Depends` relations; debug evidence identifies the selected
library and its `symbols` or `shlibs` metadata. `dpkg-query -S` verifies
package ownership inside the same environment. Debian's version comparison
retains the strongest `>=` constraint when proposals overlap. Only simple
Debian package relations emitted by this tool are accepted; unexpected output,
missing metadata, conflicting paths or package ownership fail closed. The
interpreter is checked against a package already in the proposed dependencies;
no separate dependency is guessed for the loader.

A `DT_NEEDED` library is bundled only if the final staging tree contains a
matching regular ELF reachable through the requester's static RPATH/RUNPATH
and `$ORIGIN`, with a matching SONAME. Payload-local symlinks are followed with
a bounded chain; their targets must remain in the staged payload. Its own
`DT_NEEDED` entries are inspected recursively. The traversal is limited to 16
levels, 128 ELF files and 512
requirements; cycles are tracked. This models only demonstrable private paths,
not `dlopen()`, calculated plugin names, arbitrary runtime variables or the
full dynamic linker. Missing or ambiguous libraries fail closed.

The versioned result contains an aggregate Depends proposal and per
requirement provenance: installed-path requester, SONAME, selected installed
or target-container library path, status (`bundled` or `resolved_external`),
Debian package, relation and metadata source. Errors carry an `unresolved`
requirement when an external lookup fails. Paths in the staging tree are
install-relative; no host staging path is returned. The result is a proposal,
not itself a change to Recipe, `package.runtime_dependencies` or `DEBIAN/control`.

The boundaries remain distinct:

1. #30B: `readelf` observes ELF metadata.
2. #30C: Debian metadata and `dpkg-shlibdeps` produce a bounded Depends proposal.
3. #30D: An opt-in v5 Recipe policy enables detection for amd64 Release-asset
   archive payloads. Only copied payload files are candidate ELF files. The
   final staging includes provisional `DEBIAN/control` for `dpkg-shlibdeps`;
   a deterministic merge of manual and versioned detected relations then
   replaces it. A build with unresolved requirements fails before packaging.
   Explicit SONAME overrides require a reason. `ignore` omits that requirement;
   `manual` supplies an operator relation prepared in Bookworm. To let Debian
   resolve every other requirement, the resolver creates a verified private
   ELF snapshot with the overridden `DT_NEEDED` entries removed. This snapshot
   is read-only in the resolver container and never enters the final `.deb`.
   The Run records a bounded summary; computed relations are not persisted
   back into the Recipe. Existing Recipes remain detection-disabled.
4. #27: existing APT installation and offline lifecycle validation check the
   final `.deb`.
