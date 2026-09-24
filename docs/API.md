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
