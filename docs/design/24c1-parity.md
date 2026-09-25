# #24C2A migration parity matrix

Status means functional read-only coverage, not visual similarity. `static/` remains production authority. A later checkpoint must close every blocker before cutover. The public landing has a separate deployment path.

| Feature | Legacy implementation | Backend/API authority | Svelte destination | Status | Tests | Cutover blocker |
| --- | --- | --- | --- | --- | --- | --- |
| Auth/session | `http_handler.py`, `static/app.js` | `/api/auth/status`; server OIDC/header/local auth | App bootstrap | Partially migrated | client, Lab browser | OIDC and trusted proxy browser checks |
| Overview | `static/js/pages/dashboard.js` | `/api/dashboard`, diagnostics | Overview | Partially migrated | Lab browser | fuller attention priorities and actions |
| Packages | `static/js/pages/packages.js` | `/api/packages`, `/api/packages/{name}` | Packages | Partially migrated | Lab browser | automation and lifecycle actions later |
| Repository inventory | Packages and repo settings | `/api/repository/inventory` exact reprepro snapshot | Packages secondary view | Migrated read-only | parser, real reprepro, Lab browser | none for read-only view |
| Recipe CRUD | `static/app.js`, recipe modules | `/api/recipes` list, `/api/workflows/{id}` canonical detail | Recipes list and Plan | Reads migrated; writes not started | Lab browser | v5 round-trip and write checkpoint |
| Recipe import/export/JSON | `static/js/recipe/json_editor.js` | canonical Recipe GET | canonical JSON disclosure | Read-only partial | Lab browser | lossless serializer, import, validation |
| Package → Recipe | legacy Package detail | Package `recipe` ID and canonical Recipe GET | exact Recipe or System managed route | Migrated read-only | Lab browser | none for read-only navigation |
| Managed self-build | legacy built-in Recipe view | management metadata and canonical Recipe GET | System managed self-build | Migrated read-only | Lab browser | operator override writes later |
| Test | `static/js/recipe/test_run_modal.js` | `POST /api/run` dry run | future Recipes | Not started | legacy JS | write checkpoint |
| Build | `static/app.js` | `POST /api/run` | future Recipes | Not started | legacy JS | write checkpoint |
| Runs | `static/js/pages/logs.js` | `/api/executions`, `/{run_id}` | Runs | Partially migrated | unit, Lab browser | full lifecycle actions and complete display parity |
| Run → Recipe | legacy Run context | Run `recipe_id` and canonical Recipe GET | exact Recipe or System managed route | Migrated read-only | Lab browser | immutable Run ↔ Recipe revision evidence for Plan |
| Logs | `static/js/pages/logs.js` | `/{run_id}/logs`, rendered-character cursor | Runs | Partially migrated | client, polling, Lab browser | long-log/follow and failure browser cases |
| Cancel | `static/js/pages/logs.js` | `POST /api/executions/{run_id}/cancel` | future Runs | Not started | legacy JS | mutation checkpoint |
| Recovery | Runs/System legacy views | Run DTO, diagnostics | Runs/System | Partially migrated | Lab browser | blocked scenario and read-only recovery detail |
| Validation | Runs/Packages legacy views | Validation GET/POST routes | future Runs/Packages | Not started | legacy JS | lifecycle action checkpoint |
| Publication | Runs/Packages legacy views | Publication POST routes | future Runs/Packages | Not started | legacy JS | proof and confirmation parity |
| Automation | Recipe/Packages legacy views | Automation GET/POST routes | Recipe read-only status | Read partial | Lab browser | policy controls and mutations |
| Settings | `static/settings.js` | `/api/settings` GET/POST | future Settings | Not started | legacy JS | secrets/save parity |
| Maintenance | `static/settings.js`, System | `/api/storage`, delete routes | System read-only facts | Partially migrated | Lab browser | authorized cleanup controls later |
| System | `static/js/pages/system.js` | diagnostics, storage, canonical managed Recipe | System Health and managed self-build | Partially migrated | Lab browser | more deliberate health and maintenance detail |
| Diagnostics | `static/js/pages/system.js` | `/api/system/diagnostics` | System Health | Migrated read-only | Lab browser | none for C1 |
| Inspectors | `static/js/pages/system.js` | Recipe/Run inspect GET | future System Developer | Not started | legacy tests | selection UI |
| Support bundle | `static/js/pages/system.js` | `/api/support-bundle` GET | future System Developer | Not started | legacy tests | download and error handling |
| Public repository landing | `debbuilder/repository_templates/index.html` | separate public listener | separate future template | Not started | prototype reference | separate deployment checkpoint |

`/api/packages` remains the DebBuilder-managed Packages projection. Repository inventory is an independent exact reprepro read. Package → Recipe and Run → Recipe navigation use the returned Recipe ID; application-managed Recipes route to System using management metadata. No current public Run DTO proves an exact Recipe revision, so the Plan reports no matching current evidence. Recipe writes, Test/Build, lifecycle actions, and production cutover remain blockers.
