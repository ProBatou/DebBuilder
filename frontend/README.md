# Production admin frontend source (#24C1)

This is the future authenticated admin UI source. The shipped `static/` tree is still the production entry. The separate public APT landing remains `debbuilder/repository_templates/index.html` and is outside this build.

## Build contract

Use Node 24 LTS and npm 11 (the checked build used Node 24.21.0 and npm 11.20.0). Svelte 5.57.1, Vite 8.3.1 and all build tools are pinned in `package-lock.json`; they are build dependencies only. From `frontend/`:

```sh
npm ci
npm run check
npm test
npm run build
```

`src/app` owns the shell and bootstrap, `src/api` owns read-only HTTP requests, `src/features` owns polling, `src/pages` owns view-local state, and `src/navigation`, `src/theme`, `src/i18n` own small browser preferences. No Recipe serializer or mutation adapter exists. The common semantic tokens and layout are in `src/styles.css`; the fixture prototype remains under `prototypes/issue-24` for design reference only.

## API and auth

The client calls existing GET routes only. `GET /api/auth/status` bootstraps the configured server auth mode; requests use same-origin credentials, so the server remains responsible for OIDC, trusted proxy headers, and local mode. 401, 403 and structured backend errors retain status, code, message and details. Network errors, aborts and 20-second timeouts are distinct. No token or secret is stored in browser storage. The auth bootstrap link on 401 points to the server root. For an isolated Vite/OIDC run, set `VITE_DEBBUILDER_AUTH_ORIGIN` to the isolated backend admin origin before starting Vite; sign in there, then return to the Vite URL. The Vite proxy preserves `/api` same-origin requests; it does not implement OIDC itself. A trusted proxy must still supply its configured identity header on the proxied API path.

Runs list polling is view-scoped and uses 5-second intervals. Selected Run detail/log polling uses 1.5 seconds, retries after 5 seconds, and stops on terminal state or navigation. The poller serializes requests, aborts on teardown and ignores stale results. Logs use the backend's rendered-character `after` offset separately for compact/normal/verbose/raw; changing mode resets the cursor. Pause stops automatic scroll while log collection continues. Previously rendered data remains visible after a transient error.

Theme (`System`, `Light`, `Dark`) and locale (`EN`, `FR`, `DE`, `ES`) are local browser preferences. English is the fallback. Browser `Intl` formats dates and numbers. IDs, package names, versions, technical values, errors, logs and backend-authored prose remain untranslated.

## Isolated development and browser check

In one terminal from the repository root, run `python3 -m tests.ui.behavior_lab --scenario showcase --host 127.0.0.1 --port 8765`. In another, from `frontend/`, run `npm run dev -- --port 5174`; Vite proxies `/api` to the Lab. Set `DEBBUILDER_DEV_API` to another isolated backend origin if needed. `npm run test:browser` checks real Lab data at 1440px and 390px while both servers run. The Lab creates and removes disposable data; never target the production `/opt/debbuilder` installation.

## Future package integration

Vite builds `dist/index.html`, `dist/assets/index-<hash>.js`, `dist/assets/index-<hash>.css`, and `.vite/manifest.json`. HTML refers to assets relatively, so a future reviewed integration can place the compiled tree at a dedicated path below the Python static root before the official Recipe stages `static`. The current handler serves HTML/JS/CSS with suitable MIME types and `no-cache, must-revalidate` for all files. Immutable caching would require a separate reviewed handler change; the current policy is safe though less efficient. Do not copy `node_modules`, Node, npm, Vite or Svelte into the runtime package. The official Recipe and release builder remain unchanged in C1.

The C1 build produced 353 B HTML (about 260 B gzip), 81,750 B JS (28.88 kB gzip), 6,944 B CSS (2.24 kB gzip), and a 185 B manifest. Two successive builds from the same lockfile/source produced identical file hashes. A disposable `/tmp/debbuilder-24c1-stage.*` tree was populated with only those compiled files under `opt/debbuilder/admin-candidate/`; manifest references resolved and no Node toolchain or `node_modules` was staged.

The exact feature state and cutover blockers are in [the parity matrix](../docs/design/24c1-parity.md).
