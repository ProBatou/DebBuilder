# #24C1 migration parity matrix

Status means functional read-only coverage, not visual similarity. `static/` remains production authority. A later checkpoint must close every blocker before cutover. The public landing has a separate deployment path.

| Feature | Legacy implementation | Backend/API authority | Svelte destination | Status | Tests | Cutover blocker |
| --- | --- | --- | --- | --- | --- | --- |
| Auth/session | `http_handler.py`, `static/app.js` | `/api/auth/status`; server OIDC/header/local auth | App bootstrap | Partially migrated | client, Lab browser | OIDC and trusted proxy browser checks |
| Overview | `static/js/pages/dashboard.js` | `/api/dashboard`, diagnostics | Overview | Partially migrated | Lab browser | fuller attention priorities and actions |
| Packages | `static/js/pages/packages.js` | `/api/packages`, `/api/packages/{name}` | Packages | Partially migrated | Lab browser | automation and lifecycle actions later |
| Repository inventory | Packages and repo settings | No exact reprepro inventory route | Packages gap notice | Not started | route audit | approved exact inventory API or revised product decision |
| Recipe CRUD | `static/app.js`, recipe modules | `/api/workflows`, `/api/recipes` | future Recipes | Not started | legacy JS | v5 round-trip and write checkpoint |
| Recipe import/export/JSON | `static/js/recipe/json_editor.js` | Recipe routes and local export | future Recipes | Not started | legacy JS | lossless serializer and validation |
| Test | `static/js/recipe/test_run_modal.js` | `POST /api/run` dry run | future Recipes | Not started | legacy JS | write checkpoint |
| Build | `static/app.js` | `POST /api/run` | future Recipes | Not started | legacy JS | write checkpoint |
| Runs | `static/js/pages/logs.js` | `/api/executions`, `/{run_id}` | Runs | Partially migrated | unit, Lab browser | full lifecycle actions and complete display parity |
| Logs | `static/js/pages/logs.js` | `/{run_id}/logs`, rendered-character cursor | Runs | Partially migrated | client, polling, Lab browser | long-log/follow and failure browser cases |
| Cancel | `static/js/pages/logs.js` | `POST /api/executions/{run_id}/cancel` | future Runs | Not started | legacy JS | mutation checkpoint |
| Recovery | Runs/System legacy views | Run DTO, diagnostics | Runs/System | Partially migrated | Lab browser | blocked scenario and read-only recovery detail |
| Validation | Runs/Packages legacy views | Validation GET/POST routes | future Runs/Packages | Not started | legacy JS | lifecycle action checkpoint |
| Publication | Runs/Packages legacy views | Publication POST routes | future Runs/Packages | Not started | legacy JS | proof and confirmation parity |
| Automation | Recipe/Packages legacy views | Automation GET/POST routes | future Packages/Recipes | Not started | legacy JS | policy/status and mutations |
| Settings | `static/settings.js` | `/api/settings` GET/POST | future Settings | Not started | legacy JS | secrets/save parity |
| Maintenance | `static/settings.js`, System | `/api/storage`, delete routes | System read-only facts | Partially migrated | Lab browser | authorized cleanup controls later |
| System | `static/js/pages/system.js` | `/api/system/diagnostics`, `/api/storage` | System Health | Partially migrated | Lab browser | more deliberate health and maintenance detail |
| Diagnostics | `static/js/pages/system.js` | `/api/system/diagnostics` | System Health | Migrated read-only | Lab browser | none for C1 |
| Inspectors | `static/js/pages/system.js` | Recipe/Run inspect GET | future System Developer | Not started | legacy tests | selection UI |
| Support bundle | `static/js/pages/system.js` | `/api/support-bundle` GET | future System Developer | Not started | legacy tests | download and error handling |
| Public repository landing | `debbuilder/repository_templates/index.html` | separate public listener | separate future template | Not started | prototype reference | separate deployment checkpoint |

The backend `/api/packages` list includes package projections from a local Packages index, but does not expose a separate, exact reprepro inventory contract. It cannot truthfully populate the approved repository inventory subview. System repository diagnostics establish configuration and metadata presence only; they do not prove signature validity or list contents. The UI states that gap rather than inferring inventory or publication health from unrelated fields.
