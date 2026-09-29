# API-backed admin frontend candidate

`frontend/` contains the Svelte admin UI under review for Issue #24. The
shipped admin UI remains `static/`. The public APT landing is separate from
both admin frontends. See the [current parity matrix](../docs/design/24c1-parity.md)
for migrated and deferred behavior.

## Build and test

Use Node 24.x and npm 11.x. Svelte, Vite and their build dependencies are
pinned in `package-lock.json` and do not enter the runtime package.

```sh
cd frontend
npm ci
npm run check
npm test
npm run build
```

`src/app` owns the shell and authentication bootstrap; `src/api` owns HTTP
requests; `src/features` owns polling and canonical Recipe drafts; `src/pages`
owns view-local state. Navigation, theme and locale have small dedicated
modules. `styles.css` imports the B6-derived base and real-application rules.
The separate fixture prototype remains a design reference.

## Current API behavior

The client uses same-origin credentials and the server's local, trusted-proxy
or OIDC authentication mode. HTTP errors retain backend status, code, message
and details. Abort, timeout and network failures are distinct. Theme and
language are stored locally; no auth token or secret is stored in browser
storage. A browser error on `/api/settings` does not disable local appearance
controls.

Packages lists DebBuilder-managed packages. Repository inventory is an
independent on-demand read from reprepro. Package creation obtains a canonical
Recipe v5 draft from the backend, validates it, then uses create-only Recipe
Save. Existing Recipe Save uses its strong ETag revision. A 409 conflict keeps
the local draft and cannot force an overwrite. Managed Recipe overrides remain
backend-controlled. Expert JSON import/edit applies only to the draft after
backend validation; Save is separate. Standalone New Recipe is intentionally
hidden in the current operator UI.

Test and Build validate the exact same frozen current draft submitted for Run
admission. Test sends `dry_run: true`; Build sends `dry_run: false`. Neither
implicitly saves the Recipe. The returned `run_id` is the navigation target.
An ambiguous network outcome is not automatically retried. Run selection owns
detail/log polling; requests do not overlap, stale selection responses are
ignored, the log cursor is per verbosity, and terminal detail polling stops.
The list continues to refresh while Runs is open.

Eligible Run, Package and Overview actions recheck current state before
Validation, Publication or Build update. Publication requires confirmation of
the current package/version identity. System includes curated Health,
preview-confirmed execution-history deletion, managed self-build settings and
Developer inspectors/support bundle. Settings can write bounded backend
fields; secrets display only configured state and accept replacement values.
Run cancellation, Recipe Delete/Rename, direct import persistence and
production cutover remain deferred.

The public Run DTO does not establish an immutable match to the current Recipe
revision, so the Recipe Plan does not present prior Runs as matching proof.

## Isolated browser checks

Run the Behavior Lab on an isolated data directory and port, then run Vite
with `DEBBUILDER_DEV_API` pointing at that Lab. For example:

```sh
python3 -m tests.ui.behavior_lab --scenario showcase --host 127.0.0.1 --port 8765
cd frontend
npm run dev -- --port 5174
```

`npm run test:browser` checks representative API-backed desktop/mobile paths.
Other focused `tests/browser-*.mjs` scripts cover admission, guarded Recipe
Save/conflicts, actions, Settings, System, polling and error states. Some
scripts intercept `/api` and only require Vite; others require an isolated
Behavior Lab scenario. Each script names its scenario and URL environment
variables. Never direct development tests at the production installation.

## Package integration boundary

Vite emits `dist/index.html`, hashed assets and a manifest. HTML references
assets relatively. A separate reviewed cutover must place only the compiled
tree at its intended static path, verify asset references and HTTP MIME/cache
behavior, and qualify authentication and routing. Do not copy Node, npm, Vite,
Svelte or `node_modules` into the runtime package.
