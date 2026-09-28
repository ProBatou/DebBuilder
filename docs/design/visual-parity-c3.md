# #24C3 visual and API parity audit

The [B6 prototype](../../prototypes/issue-24/) and its [reference images](references/README.md) are the visual and interaction authority. The DebBuilder backend and route registry remain the functional authority. `static/` remains the production frontend. No public APT landing or backend contract was changed in C3.

## Reproduce the comparison

Start the B6 Vite server, a `showcase` Behavior Lab on an isolated port, and the real frontend Vite server proxied to that Lab. Then run `frontend/tests/capture-visual-parity.mjs` with `DEBBUILDER_PROTOTYPE_URL`, `DEBBUILDER_FRONTEND_URL`, and `DEBBUILDER_VISUAL_DIR`. The script captures 40 B6/real screenshots at 1440×900 and 390×844, including Recipe levels, failed Runs, mobile Runs list and detail, the mobile drawer, and representative Dark states. Its JSON manifest records browser errors and page overflow. Capture output belongs in an ignored temporary directory such as `/tmp/debbuilder-c3-visual`; screenshot binaries are not committed.

The B6 prototype uses local fixtures. The real captures use Behavior Lab API data. Compare layout, hierarchy, spacing, and interaction with corresponding objects where possible; status counts, repository diagnostics, and Run histories are deliberately different. The baseline real captures made before C3 are `/tmp/c3-before-{overview,packages,recipes,runs,system}.png` in the implementation environment.

| Surface | B6 reference | Real before | Real after | Intentional difference / remaining drift |
| --- | --- | --- | --- | --- |
| Shell and Overview | `desktop-overview-light-en.png` | `c3-before-overview.png` | `real-desktop-overview-light.png` | Real counts and health come from dashboard/diagnostics. The footer shows actual suite/component/architecture without claiming repository health from a configured URL. |
| Packages and inventory | `desktop-packages-light-en.png`, `desktop-packages-repository-inventory.png` | `c3-before-packages.png` | `real-desktop-packages-light.png`, `real-desktop-repository-inventory-light.png` | Managed Packages and exact reprepro inventory remain separate reads. No public installer instructions appear in admin inventory. |
| Recipe View and Plan | `desktop-recipes-light-en.png`, `desktop-recipes-plan-audited.png` | `c3-before-recipes.png` | `real-desktop-recipes-view-light.png` | Plan reports configured/default values only; the API cannot prove matching current Run evidence. Save and admission are additional real actions. |
| Recipe Edit levels | `desktop-recipes-advanced-editor.png`, `desktop-recipes-seerr-expert.png` | `c3-before-recipes.png` | `real-desktop-recipes-edit-light.png`, `real-desktop-recipes-advanced-light.png`, `real-desktop-recipes-expert-light.png` | Real canonical v5 controls retain current field ownership and structured editors. Fields are grouped by task; managed restrictions and draft state remain live. |
| Runs and failed diagnosis | `b6-desktop-run-failed-light.png`, `desktop-runs-light-en.png` | `c3-before-runs.png` | `real-desktop-runs-failed-light.png`, `real-desktop-runs-normal-light.png` | Real pipeline stages and diagnostic text come from Run DTOs; admission uses exact returned Run ID. |
| System | `desktop-system-light-en.png` | `c3-before-system.png` | `real-desktop-system-light.png` | Every real diagnostic check remains visible; managed self-build is reached from System. Maintenance and Developer mutations remain unavailable. |
| Settings | `b6-desktop-settings.png` | no migrated page | `real-desktop-settings-light.png` | Reads actual settings and presents all seven B6 groups. Write controls remain unavailable until Settings mutation migration. Theme and language are local preferences. |
| Mobile and drawer | `mobile-overview-light-en.png`, `mobile-recipes-light-en.png`, `mobile-runs-light-en.png`, `mobile-settings-light-en.png`, `b6-mobile-sidebar-open.png` | baseline captured during audit | `real-mobile-*.png` | Real Recipe read details start collapsed; mobile list/detail and drawer retain independent scrolling. |
| Dark | `desktop-overview-dark-en-normal.png`, `b6-desktop-run-failed-dark.png` | baseline captured during audit | `real-desktop-*-dark.png` | Technical surfaces follow semantic theme colors. |

## Route and mutation matrix

All routes below were checked against `debbuilder/api_routes.py`. Source functions are in the real frontend; identifiers in braces come from exact API fields or the URL selected from an exact API row.

| UI surface | Method and route | Source | Identity / operation | Class |
| --- | --- | --- | --- | --- |
| Auth bootstrap | `GET /api/auth/status`, `GET /api/status` | `api/client.js`, `app/App.svelte` | Session and configured repository defaults | Read |
| Overview | `GET /api/dashboard`, `GET /api/system/diagnostics` | `pages/Overview.svelte` | Package `name`, Run `id`, diagnostic `id` | Read |
| Packages | `GET /api/packages`, `GET /api/packages/{name}` | `pages/Packages.svelte` | Exact package `name` from list | Read |
| Repository inventory | `GET /api/repository/inventory` | `pages/Packages.svelte` | Independent exact inventory; no Package fallback | Read |
| Recipes list | `GET /api/recipes`, `GET /api/workflows` | `pages/Recipes.svelte` | Exact Recipe `id`, writability metadata | Read |
| Recipe detail | `GET /api/workflows/{id}`, `GET /api/recipes/{id}/inspect`, `GET /api/recipes/{id}/automation` | `features/recipes/persistence.js`, `pages/Recipes.svelte` | Exact Recipe ID; workflow ETag retained | Read |
| Recipe validation | `POST /api/recipes/validate` | `features/recipes/persistence.js` | Current frozen or copied draft in `{recipe}` | Ephemeral |
| Recipe Create draft | `POST /api/recipes/draft` | `features/recipes/persistence.js` | New exact name and GitHub repository | Ephemeral |
| Recipe Save/Create | `POST /api/workflows/{id}` | `features/recipes/persistence.js` | Existing: `expected_revision`; new: `create_only`; canonical workflow body | Durable Recipe mutation |
| Test/Build | `POST /api/run` | `features/recipes/admission.js` | Frozen current draft; `dry_run: true/false`; response `run_id` | Run admission |
| Runs | `GET /api/executions`, `GET /api/executions/{run_id}`, `GET /api/executions/{run_id}/logs` | `pages/Runs.svelte` | Exact Run `id`; rendered-character log cursor and verbosity | Read/poll |
| System | `GET /api/system/diagnostics`, `GET /api/storage`, `GET /api/workflows`, managed `GET /api/workflows/{id}`, Recipe inspect/automation | `pages/System.svelte` | Managed entry from workflow metadata | Read |
| Settings | `GET /api/settings` | `pages/Settings.svelte` | Backend settings projection | Read |
| Developer link | `GET /api/openapi.json` | `pages/System.svelte` | No identifier | Read |

Package → Recipe uses `package.recipe` and checks canonical management metadata before routing. Run → Recipe uses `execution.recipe_id` (or the backend's exact `execution.recipe` alias). Recipe → Run uses the `POST /api/run` response `run_id`. Managed Recipe navigation uses application ownership metadata. Repository inventory entries are displayed without attempting to infer managed Package identities.

No incorrect frontend route or HTTP method was found. Active non-GET requests are limited to Recipe validation, canonical draft projection, guarded Recipe Save/Create, and Run admission. Delete, import persistence, cancellation, validation, publication, automation writes, observation refresh, Settings writes, and public landing cutover remain inactive in the Svelte frontend.

Browser network assertions cover GET-only read journeys, validation-only editing, guarded Save/Create, and Test/Build allowing only validation and Run admission. Existing parser, ETag, draft isolation, polling, exact navigation, and API tests remain the functional guardrails.
