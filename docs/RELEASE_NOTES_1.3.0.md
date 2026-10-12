# DebBuilder v1.3.0

This feature release completes the managed DebBuilder self-build path for its
Python backend and Svelte frontend, and strengthens terminal workspace cleanup
under normal and low-storage operation.

## Python self-builds with an auxiliary Node project

- Python source-build Recipes can opt into one explicit, safely confined
  auxiliary Node project without changing their primary project type.
- The application-managed DebBuilder Recipe uses `frontend/package.json` as
  the authority for Node 24 and exact npm 11 requirements.
- Node and npm are acquired through the existing checksum- and
  integrity-verified per-Run toolchain. Host-global Node/npm are not used as a
  fallback.
- The managed self-build runs exactly one `npm ci` during Dependencies, seals
  its manifest and lockfile inputs, and reuses the same Run-local toolchain for
  the Vite Build with npm offline mode enabled.
- The Debian package contains the Python application and compiled frontend
  assets, but no frontend sources, `node_modules`, npm cache, Node/npm binary,
  or Node/npm runtime dependency.

## Run provenance and operator visibility

- Run details expose bounded primary Python and auxiliary frontend facts,
  requested and resolved Node/npm identities, and the Dependencies installation
  result without exposing cache paths, environment values, or credentials.
- Structured failures remain attached to the correct lifecycle step, including
  acquisition, integrity, manifest, installation, timeout, and cancellation
  failures.
- The OpenAPI contract and administration UI reflect the auxiliary project and
  toolchain evidence while preserving existing Recipe schema version 5.

## Storage and cleanup

- Terminal Runs dispose their Run-local toolchain while retaining durable
  provenance, logs, artifacts, and the shared immutable toolchain cache.
- Storage-pressure admission and cleanup reclaim disposable Run workspace data
  without changing repository publication or deleting unproved active work.
- Cleanup remains fail-closed around recovery blockers, active validation or
  publication work, unsafe paths, mounts, and unresolved command containment.

## Release qualification

- Backend, API, packaging, recovery, cleanup, OpenAPI, frontend, and real
  release-builder suites cover the new lifecycle.
- Independent official release candidates remain byte-identical under the
  qualified Node 24/npm 11.20.0 and Debian packaging toolchain.
- npm offline mode is a package-manager policy and is not described as a
  general network sandbox.
