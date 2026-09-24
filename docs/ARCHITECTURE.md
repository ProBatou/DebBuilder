# Architecture

This document describes DebBuilder's internal product model. The
[README](../README.md) is the installation and first-use entry point.

## Canonical lifecycle

```text
Recipe -> Build Run -> OCI Validation -> APT Publication
```

Every Validation and Publication record belongs to the Build Run that produced
the artifact. A Run admits an immutable Recipe policy and exact upstream
identity, so a later Recipe edit or upstream movement cannot alter work already
in progress.

Recipe automation uses the same lifecycle. `detect` records the exact upstream
identity; `test` creates a dry Run; `build` stops after a successful Build;
`build_validate` continues through Validation; and `full` continues through
Publication. Disabling automation, deactivating a Recipe, or lowering its
policy can stop a stage that has not yet been admitted. An already queued or
running canonical stage reaches its normal terminal state.

## Recipes and persisted contracts

Recipe input uses the canonical Recipe v5 contract. Persisted and imported
Recipes must declare `schema_version: 5`; unsupported, missing, or malformed
versions are rejected rather than silently rewritten. Build Run inventories
are stored in per-Run manifests instead of inline in `run.json`.

Settings and secrets use their own strict schema-v1 documents. Their operational
rules are documented in [Operations](OPERATIONS.md).

## Main components

```text
debbuilder/          Python backend package
static/              Browser UI
static/js/pages/     Page controllers
static/js/recipe/    Recipe-specific browser behavior
static/css/          Page-specific styles
tests/               Unit, integration, static UI, and release gates
examples/            Public examples
data/workflows/      Runtime/user Recipes in source deployments
server.py            Entrypoint
```

Environment parsing and runtime paths live in `debbuilder/runtime.py`. HTTP
routing is separated from package projection, execution, automation,
Validation, Publication, settings, and repository services. The application
module wires those boundaries to the standard-library HTTP server.

`debbuilder/api_routes.py` is the canonical inventory for named admin API
operations. Its immutable descriptors define each method, path template,
stable operation identifier, handler identity, authentication boundary, and
read/mutation effect. `debbuilder/http_handler.py` uses that inventory for
runtime dispatch while keeping response handling and subsystem calls in the
existing HTTP layer. Future API-description generation can consume the same
inventory; no generated or served API description is available yet. The
separate public APT listener and legacy wildcard admin `HEAD` handling are not
named API operations and remain outside the registry.

All non-success JSON responses from the admin API use one machine-readable
envelope:

```json
{"ok": false, "error": {"code": "stable_code", "message": "Safe message", "details": {}}}
```

The lowercase error code is the stable client contract. Messages are bounded
operator-facing summaries rather than raw debugging or exception output, and
`details` contains only explicitly selected structured context. Internal
exceptions are logged server-side and exposed as `internal_error`. Successful
response bodies, browser authentication redirects, static-file responses, and
the separate public APT listener do not use this error envelope. The route
registry identifies `ApiError` as the error schema for future API-description
generation; no OpenAPI endpoint is available yet.

## Build model

Builds run in per-Run workspaces and use structured argument vectors rather
than shell command strings. DebBuilder detects Node.js, Python, Rust, and
static projects, then presents detected dependencies and build actions for
review through a Recipe.

Python detection recognizes common `pyproject.toml`, setuptools, requirements,
Pipenv, Poetry, and uv metadata without executing project files or translating
PyPI names into Debian package names. Explicit PEP 517 projects receive a
reviewable `python3 -m build` proposal; source applications with no compilation
step can package selected runtime files directly.

## Boundaries

DebBuilder's workspace, process, and filesystem controls reduce accidental
cross-Run interference and orphaned work. They do not make untrusted build
code safe. The packaged service and its trust boundary are described in
[Operations](OPERATIONS.md), Validation in [Validation](VALIDATION.md), and
repository publication in [APT repository operations](APT_REPOSITORY.md).
