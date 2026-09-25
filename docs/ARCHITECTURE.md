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
existing HTTP layer. `debbuilder/openapi.py` projects those descriptors into
OpenAPI 3.1. It declares only representation details absent from the registry:
request and success schemas, query parameters, success statuses, and relevant
error codes. Tests require exact operation parity with the registry. The
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
the separate public APT listener do not use this error envelope. OpenAPI uses
one reusable `ApiError` schema and checks every documented error code against
the canonical code inventory in `debbuilder/api_errors.py`. The committed
`openapi.json` is generated offline from these sources and served at the
authenticated read-only `GET /api/openapi.json` route. See [API contract](API.md)
for generation and validation commands.

`debbuilder/system_diagnostics.py` is the canonical, transport-independent
diagnostic service and check-ID inventory. The HTTP handler passes existing
process services through the application facade. Each probe has its own safe
failure boundary; no raw subsystem record or exception is serialized. Settings
validation, repository configuration parsing, cached containment capability,
and admission/scheduler state reuse existing projections. Repository file reads
are size-limited and do not mutate or reconcile state. The resulting snapshot
is a bounded read-only operator view, not a continuous health monitor.

`debbuilder/inspectors.py` owns two independent, versioned allowlist
projections for a single Recipe or Run. The app facade resolves a bounded
document through the existing Recipe/Build stores, consults only persisted
observation and one Validation attempt, then delegates to these pure Python
services. The inspectors do not return the existing broad UI DTO or a raw
durable record. Publication status reuses the canonical proof projector;
automation eligibility reuses the Recipe rule. No inspector triggers
upstream discovery, Validation, publication, recovery or repair.

`debbuilder/support_bundle.py` assembles only those existing projections into
a deterministic, bounded in-memory ZIP. It has no stores, probes, filesystem
reads, network or subprocess access. The authenticated, read-only HTTP route
resolves optional explicit Recipe/Run selections through the app facade before
calling the builder; it never acquires a mutation lease. ZIP metadata and
filenames are fixed and construction failures expose only a canonical error.

The `static/js/pages/system.js` consumer renders fixed, allowlisted diagnostic
and inspector fields with DOM text nodes. It uses the existing view navigation,
cards, badges, dialog and API error helpers. The browser downloads the ZIP as
an opaque blob; only the server assembles it. Recipe and Run inspection fetches
occur on operator action, not during dashboard startup or background polling.

## Build model

Builds run in per-Run workspaces and use structured argument vectors rather
than shell command strings. DebBuilder detects Node.js, Python, Rust, and
static projects, then presents detected dependencies and build actions for
review through a Recipe.

The three source-tree preparation mechanisms have distinct lifecycle ownership:

- Build commands are actual project commands and run through the contained
  command runner.
- Source changes modify acquired source content before Build commands.
- `build.ensure_directories` declaratively makes selected payload directories
  available after successful Build commands and before output resolution.

Ensured directories are explicit, relative to `workspace/source`, and must be
covered by `build.output`. This does not make an ensured directory an output by
itself. Ordinary selected outputs remain fail-closed: if the build did not
produce one and it was not explicitly ensured, the Build fails rather than
materializing an empty replacement.

Python detection recognizes common `pyproject.toml`, setuptools, requirements,
Pipenv, Poetry, and uv metadata without executing project files or translating
PyPI names into Debian package names. Explicit PEP 517 projects receive a
reviewable `python3 -m build` proposal; source applications with no compilation
step can package selected runtime files directly.

Runtime dependency detection is opt-in and leaves historical Recipes
unchanged. For supported prebuilt amd64 Release assets, the packaging path is:

```text
final staging
-> candidate ELF inspection
-> Bookworm/amd64 dependency resolution
-> bundled/external classification
-> manual + detected dependency merge
-> DEBIAN/control
-> package
-> #27 Validation
```

Inspection uses `readelf`, never `ldd`, and never executes upstream ELF files.
The currently supported resolver profile is Debian Bookworm on amd64. See
[ELF inspection and runtime dependencies](ELF_INSPECTION.md) for the detailed
contract, limits, override rules, and offline resolver boundary.

## Boundaries

DebBuilder's workspace, process, and filesystem controls reduce accidental
cross-Run interference and orphaned work. They do not make untrusted build
code safe. The packaged service and its trust boundary are described in
[Operations](OPERATIONS.md), Validation in [Validation](VALIDATION.md), and
repository publication in [APT repository operations](APT_REPOSITORY.md).
