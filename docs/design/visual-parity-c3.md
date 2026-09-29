# #24C3 API audit and visual approximation

This document records C3/C3.1/C3.2 history. Subsequent operator page reviews
changed the real Svelte frontend, which is now the visual and interaction
authority for the production candidate. The B6 prototype remains a reference,
not a strict parity target. Current functional coverage is in the
[migration parity matrix](24c1-parity.md).

C3 completed the route/HTTP audit and preserved the C1→C2D functional behavior. Its visual work was an approximation made with CSS overrides on the earlier frontend trees. The C3 screenshot harness captured stable rendering and checked errors and overflow, but did **not** compare B6 and real geometry. C3 therefore did not establish B6 visual parity. #24C3.1 ports B6 presentation structures into the real frontend and checks their geometry against the reference.

## #24C3.2 operator review corrections

C3.2 keeps the B6 shell and measured columns while applying the operator's post-B6 decisions:

- Standalone **New Recipe** actions are removed from Overview, Packages and the Recipe selector. Canonical draft projection and `create_only` remain available to the existing regression path; Recipe creation will be surfaced through a future Package creation and association workflow.
- Run logs expose verbosity directly. Run and System Health show curated explanations and facts instead of recursive API objects; raw diagnostic payloads require an explicit action in System → Developer.
- Settings keeps local Theme and Language controls. Backend settings appear as read-only facts, including configured/not-configured secret state, without implying that Settings writes work.
- Recipe Plan omits raw ELF overrides. Customize, Advanced and Expert group editable fields by task; Advanced groups disclose on demand, Expert collection actions are compact, and canonical JSON is collapsed under Expert. Inspection and Automation show operator summaries, and the duplicate raw Advanced configuration tree is removed.
- One shared status mapping reserves **✓** for completed success. Ready and prepared states use a positive dot; warnings, active states and failures have distinct symbols. The backend and route contracts remain unchanged.

The operator corrections to standalone creation and log Options intentionally supersede those two B6 interactions. `frontend/tests/capture-operator-c32.mjs` produces the review states and an error/overflow manifest.

## #24C3.1 measured presentation port

The real shell now uses the B6 sidebar head, mark, navigation icon wrappers, footer, mobile header and drawer composition. Overview uses compact B6 panels; Packages uses B6 list and detail facts; Recipes uses B6 selector, editor head, levels, plan, save bar and next action panel around the existing draft logic; Runs uses the B6 list, summary, numbered stages and logs composition; System limits its initial diagnostics to four and discloses additional real checks; Settings retains all seven B6 groups and read-only real settings. The backend and B6 visual prototype are unchanged. `frontend/src/styles.css` imports `styles/b6.css` for the directly ported B6 tokens, primitives and page rules, then `styles/real.css` for the real API/editor controls absent from the prototype. The prior pre-C3 and C3 override layers were removed.

Run `frontend/tests/compare-b6-geometry.mjs` against the B6 and real Vite servers. It measures 83 `getBoundingClientRect()` coordinates across ten desktop/mobile surfaces and writes `geometry.json` beside the screenshots. Shell and column tolerances are 2–16 px; Recipe level and plan vertical tolerances are 30–50 px; Runs stage top permits 150 px because API steps and failure content differ. Log horizontal geometry is checked, while its vertical position is not asserted because stage counts differ. The final C3.1 capture directory is `/tmp/debbuilder-c31-visual-final`; the C3 before-correction directory remains `/tmp/debbuilder-c3-visual-final`. The final run passed all 83 assertions; 40 B6/real screenshots had zero browser errors and zero horizontal overflow.

| C3.1 surface at 1440×900 | B6 geometry | Real geometry | Difference | Result |
| --- | --- | --- | --- | --- |
| Sidebar width / content start | 230 / 230 px | 230 / 230 px | 0 / 0 px | Pass |
| Overview main / secondary columns | 762.3 / 355.7 px | 762.3 / 355.7 px | 0 / 0 px | Pass |
| Overview first / right panel top | 169.1 / 169.1 px | 169.1 / 169.1 px | 0 / 0 px | Pass |
| Recipe selector / editor width | 220 / 898 px | 220 / 898 px | 0 / 0 px | Pass |
| Recipe levels / plan top | 216.1 / 274.1 px | 215.1 / 273.1 px | −1 / −1 px | Pass |
| Runs selector / detail width | 350 / 768 px | 350 / 768 px | 0 / 0 px | Pass |
| Runs stages top | 610.1 px | 669.1 px | +59 px | Pass: real API failure summary differs |
| Runs log width | 726 px | 726 px | 0 px | Pass |

All measured Package, System and Settings desktop column coordinates also matched; the four measured mobile surfaces passed their width and overflow checks. Heights of lists, stages and cards depend on real API records and remain content driven. The B6 component markup and classes, rather than CSS overrides on the old frontend tree, now set the measured layout.

| C3.1 comparison | B6 screenshot | Real screenshot | Intentional real difference |
| --- | --- | --- | --- |
| Overview | `b6-desktop-overview-light.png` | `real-desktop-overview-light.png` | Actual dashboard counts, attention entries and repository diagnosis; primary action creates a Recipe. |
| Packages / inventory | `b6-desktop-packages-light.png` | `real-desktop-packages-light.png` | Exact managed Package and reprepro inventory identities remain separate. |
| Recipes | `b6-desktop-recipes-view-light.png` | `real-desktop-recipes-view-light.png` | Current Plan reports configured values and absence of matching Run evidence; Save/Test/Build controls remain live. |
| Runs | `b6-desktop-runs-failed-light.png` | `real-desktop-runs-failed-light.png` | Actual pipeline stages, failure, log text and Run ID. |
| System / Settings | `b6-desktop-system-light.png`, `b6-desktop-settings-light.png` | `real-desktop-system-light.png`, `real-desktop-settings-light.png` | Real checks and read-only backend settings. |
| Mobile / Dark | `b6-mobile-*.png`, `b6-desktop-*-dark.png` | `real-mobile-*.png`, `real-desktop-*-dark.png` | Actual records, locale strings and permitted actions. |

B6 labels are retained only where they match current product meaning. The real primary action creates a Recipe, not a Package; Repository health comes from diagnostics, not the B6 fixture; managed Recipe remains under System. Settings writes, Delete, Import, Cancel, Validation, Publication, Automation writes and cutover remain outside this checkpoint.


At C3, the [B6 prototype](../../prototypes/issue-24/) and its [reference images](references/README.md) were the visual and interaction reference. Later operator page reviews supersede specific B6 choices. The DebBuilder backend and route registry remain the functional authority. `static/` remains the production frontend. No public APT landing or backend contract was changed in C3.

## Reproduce the comparison

Start the B6 Vite server, a `showcase` Behavior Lab on an isolated port, and the real frontend Vite server proxied to that Lab. Then run `frontend/tests/capture-visual-parity.mjs` with `DEBBUILDER_PROTOTYPE_URL`, `DEBBUILDER_FRONTEND_URL`, and `DEBBUILDER_VISUAL_DIR`. The script captures 40 B6/real screenshots at 1440×900 and 390×844, including Recipe levels, failed Runs, mobile Runs list and detail, the mobile drawer, and representative Dark states. Its JSON manifest records browser errors and page overflow. Capture output belongs in an ignored temporary directory such as `/tmp/debbuilder-c3-visual`; screenshot binaries are not committed.

The B6 prototype uses local fixtures. The real captures use Behavior Lab API data. Compare layout, hierarchy, spacing, and interaction with corresponding objects where possible; status counts, repository diagnostics, and Run histories are deliberately different. The baseline real captures made before C3 are `/tmp/c3-before-{overview,packages,recipes,runs,system}.png` in the implementation environment.

| Surface | B6 reference | Real before | Real C3 approximation | Intentional difference / remaining drift |
| --- | --- | --- | --- | --- |
| Shell and Overview | `desktop-overview-light-en.png` | `c3-before-overview.png` | `real-desktop-overview-light.png` | Real counts and health come from dashboard/diagnostics. The footer shows actual suite/component/architecture without claiming repository health from a configured URL. |
| Packages and inventory | `desktop-packages-light-en.png`, `desktop-packages-repository-inventory.png` | `c3-before-packages.png` | `real-desktop-packages-light.png`, `real-desktop-repository-inventory-light.png` | Managed Packages and exact reprepro inventory remain separate reads. No public installer instructions appear in admin inventory. |
| Recipe View and Plan | `desktop-recipes-light-en.png`, `desktop-recipes-plan-audited.png` | `c3-before-recipes.png` | `real-desktop-recipes-view-light.png` | Plan reports configured/default values only; the API cannot prove matching current Run evidence. Save and admission are additional real actions. |
| Recipe Edit levels | `desktop-recipes-advanced-editor.png`, `desktop-recipes-seerr-expert.png` | `c3-before-recipes.png` | `real-desktop-recipes-edit-light.png`, `real-desktop-recipes-advanced-light.png`, `real-desktop-recipes-expert-light.png` | Real canonical v5 controls retain current field ownership and structured editors. Fields are grouped by task; managed restrictions and draft state remain live. |
| Runs and failed diagnosis | `b6-desktop-run-failed-light.png`, `desktop-runs-light-en.png` | `c3-before-runs.png` | `real-desktop-runs-failed-light.png`, `real-desktop-runs-normal-light.png` | Real pipeline stages and diagnostic text come from Run DTOs; admission uses exact returned Run ID. |
| System | `desktop-system-light-en.png` | `c3-before-system.png` | `real-desktop-system-light.png` | Every real diagnostic check remains accessible through progressive disclosure; managed self-build is reached from System. Maintenance and Developer mutations remain unavailable. |
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
