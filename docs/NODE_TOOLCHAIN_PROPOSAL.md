# Per-Run Node build toolchains

Node projects are detected from `package.json`. The detector records
`engines.node`, the lockfile-selected package manager, and the version declared
by `packageManager` or `engines.npm`. A missing Node range, unsupported range,
or unsatisfied range fails closed; the host Node installation is not a
fallback. npm without a declared version uses the npm version published in the
selected Node release metadata.

During the network-enabled dependencies step, DebBuilder reads the official
Node release index, selects the highest stable compatible binary for the host
platform and architecture, and verifies the archive against that release's
official `SHASUMS256.txt`. npm and pnpm versions are resolved from abbreviated
npm registry metadata and their archives are verified using the published SRI
identity. Corepack is not enabled or modified.

Verified files are promoted under the data directory's `toolchains` cache.
The cache key includes Node version/platform/architecture or package-manager
name/version/integrity. Cross-process file locks, private staging directories,
and atomic rename prevent concurrent partial entries. Cache manifests and
critical executable hashes are checked before reuse. Extracted cache files are
read-only; the cache remains an optimization because current upstream metadata
and integrity identities are consulted before every preparation.

Each Run receives `toolchain/bin` entry points that refer to the selected
immutable cache entries. Its absolute bin path is prepended only to that Run's
controlled command environment, so dependency checks, npm/pnpm scripts, and
build subprocesses all resolve the same Node binary. HOME, npm's cache, and any
Corepack state also point inside the Run workspace. The Run manifest records
the requested range, exact versions, platform/architecture, distribution
SHA-256, package-manager SRI, and durable source URLs without exposing cache
paths. After a Run is terminal, workspace cleanup removes the entire Run-local
`toolchain/` tree, including its HOME, npm/pnpm caches, Corepack state, and
entry points. The durable identity in `run.json` remains available for history
and diagnostics, and cleanup never removes the shared cache.

After preparation, validation of the Run-local entry points is local-only.
Missing prepared files fail with `prepared_node_toolchain_missing`; no Node,
npm, pnpm, or Corepack acquisition is attempted by the offline execution path.
Existing command containment, cancellation checkpoints, and workspace cleanup
remain authoritative.

## Explicit auxiliary Node project for a Python source build

A Recipe v5 may add `build.node_project_directory` to a `source_build` whose
detected primary application is Python. The value is optional and names one
canonical relative POSIX directory confined below the acquired source root;
for DebBuilder itself it is `frontend`. Omission preserves historical Recipe
behavior. DebBuilder does not search recursively, infer this directory, or
support multiple auxiliary projects.

The auxiliary directory must contain `package.json`. Its `engines.node`,
`engines.npm`, and exact npm `packageManager` declaration provide the
requirements; npm is the only supported auxiliary package manager. Detection
keeps `project_type: python` and records the Node facts separately as
`auxiliary_node_project`. The Dependencies stage resolves and prepares the
same per-Run toolchain described above. Dependency checks and Build commands
receive its Run-local `PATH`, HOME, npm cache, and Corepack state. Build and
Packaging never acquire a replacement and never fall back to host Node/npm.

Installation remains a second, explicit opt-in:
`build.auxiliary_node_install: npm_ci`. It accepts only npm and requires a
regular non-symlink `package-lock.json` at lockfile version 2 or 3. DebBuilder
rejects project `.npmrc`, `npm-shrinkwrap.json`, workspaces, local `file:` or
`link:` graphs, and registry entries without recorded integrity. It seals the
relative paths, sizes, and SHA-256 values of `package.json` and the lockfile,
plus the absence of both configuration alternatives, immediately before and
after `npm ci`, after source changes, and after every Build command. A changed
input fails the responsible stage; DebBuilder neither reinstalls nor silently
adopts a new graph.

The Dependencies command uses the prepared Run-local PATH, HOME and cache,
empty Run-local user/global npm configuration, neutralizes inherited
`npm_config_*` names case-insensitively, and disables audit, fund and update
notices. It may reach the registry during Dependencies. Build receives the
same toolchain with `npm_config_offline=true` and never runs a second install;
this is an npm policy, not a general network sandbox. Dry-run seals and
validates the inputs without executing `npm ci`. Cancellation, timeouts,
descendant termination, durable command diagnostics, restart recovery, and
#50 cleanup retain their existing Run semantics; cleanup removes Run-local
state while preserving the shared verified toolchain cache.

The managed DebBuilder definition is version 11. Its v10 migration adds the
explicit install field and removes `npm --prefix frontend ci` from Build while
preserving `active`, maintainer, Build environment/timeouts, and resource
limit overrides. Both auxiliary fields are application-managed and are not
operator overrides. The Build compiles the frontend into `static/` while the
primary package remains Python. Neither Node/npm nor `node_modules` enters the
Debian payload or runtime dependencies.

`run.json` durably retains primary detection, auxiliary manifest requirements,
resolved Node/npm identity and integrity, stage status, and diagnostics. The
public Run projection exposes the bounded relative auxiliary facts and the
existing toolchain identity without workspace/cache paths. Cancellation and
restart recovery retain the normal Run semantics. Terminal cleanup removes
Run-local toolchain state, while the shared verified cache and durable
provenance remain.

This contract does not add implicit configuration, recursive discovery,
Docker execution, alternate Node package managers, or more than one auxiliary
project. The official Release builder remains a separate host-qualified path:
its caller provisions compatible Node/npm before invoking the same frontend
commands; it is not migrated to Run-local acquisition.
