# #24C2C migration parity matrix

Status means functional read-only coverage, not visual similarity. `static/` remains production authority. A later checkpoint must close every blocker before cutover. The public landing has a separate deployment path.

| Feature | Legacy implementation | Backend/API authority | Svelte destination | Status | Tests | Cutover blocker |
| --- | --- | --- | --- | --- | --- | --- |
| Auth/session | `http_handler.py`, `static/app.js` | `/api/auth/status`; server OIDC/header/local auth | App bootstrap | Partially migrated | client, Lab browser | OIDC and trusted proxy browser checks |
| Overview | `static/js/pages/dashboard.js` | `/api/dashboard`, diagnostics | Overview | Partially migrated | Lab browser | fuller attention priorities and actions |
| Packages | `static/js/pages/packages.js` | `/api/packages`, `/api/packages/{name}` | Packages | Partially migrated | Lab browser | automation and lifecycle actions later |
| Repository inventory | Packages and repo settings | `/api/repository/inventory` exact reprepro snapshot | Packages secondary view | Migrated read-only | parser, real reprepro, Lab browser | none for read-only view |
| Recipe read | `static/app.js`, recipe modules | `/api/recipes` list, `/api/workflows/{id}` canonical detail | Recipes list and Plan | Migrated | Lab browser | none for read |
| Recipe editor presentation | `static/app.js`, recipe modules | Recipe v5 | Plan/Customize/Advanced/Expert | Migrated locally | editor Lab browser | persistence inactive |
| Recipe draft/serialization | `static/recipe_serialization.js` | `recipe_document_for_storage` | baseline/draft model | Qualified locally | exact canonical corpus and isolation tests | future save wiring |
| Recipe validation | legacy form/API | `POST /api/recipes/validate` | current draft validation | Migrated | Lab browser and API | no durable save |
| Recipe existing Save | legacy save routes | guarded workflow Save + ETag | Recipes | Migrated | API, Lab browser | none |
| Optimistic concurrency | none | exact-byte revision check | Recipes conflict review | Migrated | store, API, Lab browser | none |
| Recipe Create | legacy save routes | canonical draft + create-only workflow Save | Recipes | Migrated | store, API, Lab browser | none |
| Recipe Rename | legacy previous_id | unguarded legacy rename only | read-only ID/name | Deferred post-v1 | editor browser | atomic multi-object design |
| Recipe Delete | legacy delete route | workflow Delete | future Recipes | Not started | legacy tests | dependency and concurrency review |
| Recipe import/export/JSON | `static/js/recipe/json_editor.js` | canonical Recipe GET | baseline/draft JSON review | Read-only partial | Lab browser | import mutation inactive |
| Package → Recipe | legacy Package detail | Package `recipe` ID and canonical Recipe GET | exact Recipe or System managed route | Migrated read-only | Lab browser | none for read-only navigation |
| Managed self-build | legacy built-in Recipe view | management metadata and guarded Save | System managed self-build | Migrated | API, Lab browser | none |
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

`/api/packages` remains the DebBuilder-managed Packages projection. Repository inventory is an independent exact reprepro read. Package → Recipe and Run → Recipe navigation use the returned Recipe ID; application-managed Recipes route to System using management metadata. No current public Run DTO proves an exact Recipe revision, so the Plan reports no matching current evidence. Recipe Delete, Test/Build, lifecycle actions, and production cutover remain blockers. See [the C2B edit contract](recipe-editor/edit-contract.md).
