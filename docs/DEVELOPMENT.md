# Development guide

This guide covers local startup and tests for contributors. Product internals
are described in [Architecture](ARCHITECTURE.md).

## Repository layout

```text
debbuilder/          Python backend package
static/              Browser UI
tests/               Unit, integration, static UI, and release gates
validation/          Validation image definitions and maintainer notes
packaging/           Packaged environment defaults
examples/            Public examples
server.py            Application entrypoint
```

## Local source run

Copy the example environment, load it into the process environment, and start
the server:

```bash
cp .env.example .env
set -a
. ./.env
set +a
python3 server.py
```

The admin UI defaults to `http://127.0.0.1:8099`; the public repository
listener defaults to `http://127.0.0.1:8081`. Use temporary data and repository
directories when running development instances in parallel.

For an on-demand, authenticated, read-only snapshot of startup prerequisites
and admission state, request `GET /api/system/diagnostics` on the admin
listener. Its canonical check inventory and transport-independent builder live
in `debbuilder/system_diagnostics.py`; see [API contract](API.md) for status
semantics and limitations. The endpoint does not repair local state or replace
monitoring.

## Checks

The broad local checks are:

```bash
python3 -m py_compile server.py debbuilder/*.py
python3 -m unittest discover -s tests -v
for file in $(find static -name '*.js' -type f); do node --check "$file"; done
git diff --check
```

Use narrower test modules while iterating, then run the relevant broader gates
for the change. Release work has additional checks in
[Release process](RELEASE_PROCESS.md).

## Browser tests

Playwright starts a real isolated server on a free loopback port, loads Recipe
fixtures and deterministic showcase Runs, then removes its temporary runtime:

```bash
npm install
npx playwright install chromium
npm run test:ui
```

Desktop, mobile, report, and trace output is written below the ignored
`.ui-artifacts/` directory.

## UI Behavior Lab

List isolated development scenarios with:

```bash
python3 -m tests.ui.behavior_lab --list-scenarios
```

Launch the static showcase with:

```bash
python3 -m tests.ui.behavior_lab --scenario showcase --host 127.0.0.1 --port 8765
```

Binding to `0.0.0.0` makes the development server reachable from the network
and should be a deliberate choice. The Lab removes its disposable data and
repository directories when stopped.

Available scenarios include `showcase`, `cancellation-running`,
`queued-cancellable`, `build-failure`, `prepared-test`, `graceful-shutdown`,
`recovery`, and `recovery-blocked`.
