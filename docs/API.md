# Admin API contract

Fetch `GET /api/openapi.json` on the admin listener using the configured admin
authentication mode. The same OpenAPI 3.1 document is committed as
[`openapi.json`](../openapi.json) for offline client generation. DebBuilder
does not bundle Swagger UI, ReDoc, or another HTML API viewer.

Regenerate the file from the repository root:

```sh
python3 -m debbuilder.openapi > openapi.json
```

Validate the committed artifact, operation parity, error codes, schema
references, determinism, and HTTP/auth behavior:

```sh
python3 -m unittest -v tests.test_openapi tests.test_api_routes tests.test_api_errors
```

The route registry owns methods, paths, operation IDs, summaries,
authentication policy, and read/mutation effects. `debbuilder/api_errors.py`
owns the error envelope and stable code inventory. `debbuilder/openapi.py`
declares only request/response representation details not available in those
runtime contracts; tests reject missing or extra operation descriptions and
unknown documented codes. The checked-in JSON is derived, not edited by hand.

The API uses configured local (`none`), trusted reverse-proxy header, or OIDC
session authentication. The document describes all three without embedding
the current deployment settings. Its `security` alternatives mean only the
mode currently configured by the server applies. An omitted POST body is
parsed as `{}` by the current server; operations that need fields will then
return their documented error envelope.

Recipe v5 fields and selected major response objects are documented in the
schema. The full nested Recipe rules remain enforced by `recipe_schema.py`;
the OpenAPI schema is intentionally a client guide rather than a second
validator. Likewise, execution, automation, and storage projections have
documented stable fields plus subsystem-dependent fields. Tests pin the
important request fields and real response wrappers to the runtime.

## System diagnostics

`GET /api/system/diagnostics` is a configured-admin, read-only snapshot for
troubleshooting. It returns diagnostic schema version 1, an overall status,
and nine checks with stable IDs from `debbuilder/system_diagnostics.py`.
Check statuses are `ok`, `warning`, `failed`, or `unknown`. Overall status is
`failed` if any check failed, otherwise `warning` if any check is warning or
unknown, otherwise `ok`. A degraded probe is still HTTP 200; HTTP errors use
the canonical `ApiError` contract and normally mean authentication is
unavailable/denied or the response itself could not be constructed.

Details are a fixed allowlist of bounded scalar fields. The endpoint omits
paths, environment values, credentials, command output, Run/Recipe contents,
and raw exception text. Repository metadata presence does **not** verify its
signature or prove publication readiness. OCI reports only local Podman binary
presence, not runtime functionality or qualified-image state. No bootstrap,
repair, container, image pull, network probe, or repository publication occurs.
This is an on-demand explanation of prerequisites and current admission, not
historical monitoring or alerting. The support bundle below reuses this same
Python projection; this endpoint itself neither creates nor exports a bundle.

## Recipe and Run inspectors

`GET /api/recipes/{recipe_id}/inspect` and
`GET /api/executions/{run_id}/inspect` are authenticated, read-only operator
projections. Both responses wrap an `inspection` at schema version 1. They
explain one selected Recipe or Run, unlike the existing resource GETs that
return richer UI DTOs. The inspectors intentionally omit raw Recipe snapshots,
commands, build environment, maintainer scripts, source-change content, logs,
stderr, tracebacks, private paths and credentials.

Recipe inspection summarizes identity, source selection, build/artifact and
installation shape, service, automation eligibility and the last *locally
stored* observation classification. It does not refresh upstream state.
Repository/ref strings and observation display text are omitted because they
can contain operator-authored or upstream-controlled data. Run inspection
summarizes its fixed pipeline steps, artifact identity, one selected Validation
attempt, publication proof state, recovery and a classified error code. It
does not load logs, OCI output or the complete Validation attempt history.

One Recipe is limited to 1 MiB and one Run document to 8 MiB for this view.
Validation inventory is capped at 512 directory entries; only the most
recently allocated attempt ID is loaded. Counts and truncation flags make
omitted history explicit. `cancellable` is true only when the current process
manager still owns the queued or active Run at snapshot time.
Unsafe or unreadable persisted inputs return canonical HTTP 409 errors;
invalid IDs return 400 and absent resources return 404. The corresponding
Python services are `inspect_recipe(...)` and `inspect_run(...)` in
`debbuilder/inspectors.py`; the support bundle reuses these same allowlists.

## Support bundle

`GET /api/support-bundle` downloads a read-only ZIP under configured admin
authentication. Optional, single `recipe_id` and `run_id` query parameters add
one inspection of each type, independently; neither selects the other. Unknown
or repeated parameters return 400. Invalid IDs return 400, missing selections
404, and unsafe/unreadable inspections 409, all with canonical JSON errors.
Assembly failures return the generic `support_bundle_unavailable` JSON error.

The archive contains `manifest.json` and `system-diagnostics.json`, plus
`recipe-inspection.json` and/or `run-inspection.json` when explicitly selected.
The versioned manifest lists **all** archive entries, including itself, the
application version, and Boolean selection flags; it has no clock, host or
local path. Entry order, JSON encoding and ZIP metadata are fixed, so equal
projections yield byte-identical archives. Entries are at most 512 KiB each,
total uncompressed JSON at most 2 MiB, and final ZIP at most 2 MiB + 4 KiB.
There are no arbitrary entry names or filesystem traversal. Only the existing
sanitized diagnostics and inspectors are serialized: no raw Settings, Secrets,
Recipes, Runs, logs, command output, OCI data or repository files. Treat the
download as operator data nevertheless and share it deliberately.

## Operator UI consumers

The existing admin sidebar has one **System** view. It displays the runtime
fields and nine checks from `/api/system/diagnostics`, with an explicit legend
for OK, Warning, Failed and Unknown; Refresh makes a new on-demand request.
The page downloads the server-generated support ZIP and links to the raw
`/api/openapi.json` contract. The Recipes toolbar and selected Run actions
open the respective `/inspect` projections on demand and can download a
bundle scoped to that one selected ID. The UI neither inspects ZIP contents
nor reconstructs diagnostics from raw Recipe/Run DTOs. No logs, authored
commands, secrets, private paths or raw records are shown in these views.
