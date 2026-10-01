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
paths. Workspace cleanup removes the Run-local entry points but never removes
the shared cache.

After preparation, validation of the Run-local entry points is local-only.
Missing prepared files fail with `prepared_node_toolchain_missing`; no Node,
npm, pnpm, or Corepack acquisition is attempted by the offline execution path.
Existing command containment, cancellation checkpoints, and workspace cleanup
remain authoritative.
