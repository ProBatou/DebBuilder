# DebBuilder

DebBuilder is a self-hosted console for building, validating, and publishing Debian packages from GitHub sources into a personal APT repository.

## Current features

- GitHub release, tag, source-archive, and official release-asset acquisition;
- Node.js, Python, Rust, and static project detection;
- declarative source modifications and source builds;
- upstream Debian artifact validation;
- FHS-aware Debian package generation with per-file ownership and modes;
- service-account, persistent-directory, and advanced systemd unit generation;
- Podman-based validation profiles and toolchains;
- `reprepro` publication and reconciliation;
- Build Run-derived package state and history;
- runtime/user JSON Recipes;
- OIDC/header/disabled authentication modes and notifications.

The canonical lifecycle is:

```text
Recipe → Build Run → OCI validation → APT publication
```

Validation and publication records belong to the Build Run that produced the
artifact. There is no separate package-publication endpoint or parallel legacy
execution model.

## Project layout

```text
debbuilder/          Python backend package
static/              Browser UI
static/js/pages/     Vanilla JavaScript page controllers
static/js/recipe/    Recipe-specific browser behavior
static/css/          Page-specific styles loaded after the shared stylesheet
tests/               Unit and static UI tests
examples/            Public examples
examples/recipes/    Source-controlled sample Recipes
data/workflows/      Runtime/user Recipes (ignored by Git)
data/                Local runtime data (ignored by Git except structural .gitkeep files)
server.py            Entrypoint
```

Backend runtime paths and environment parsing live in `debbuilder/runtime.py`.
HTTP routing stays in `debbuilder/http_handler.py`; package projections,
executions, automation, validation, and publication are separate services. The
application module wires those boundaries together for the stdlib HTTP server.

Recipe input is normalized to the nested Recipe v1 schema. A narrow
`recipe_migrations.py` module translates only `build.timeout`,
`install.config_policy`, and persisted `service.configured`, which still occur
in current Recipes or Build Run snapshots. It also completes the archive source
and asset-selection fields for persisted upstream-archive Recipes that already
select a release asset. New code and API clients must emit the canonical shape.
Build Run inventories are stored in per-run manifests rather than inline in
`run.json`.

## Configuration

Copy `.env.example` and adapt it for your instance:

```bash
cp .env.example .env
```

Main variables:

- `DEBBUILDER_HOST`
- `DEBBUILDER_PORT`
- `DEBBUILDER_DATA_DIR`
- `DEBBUILDER_REPO_ROOT`
- `DEBBUILDER_REPO_URL`
- `DEBBUILDER_SUITE`
- `DEBBUILDER_COMPONENT`
- `DEBBUILDER_AUTH_MODE`
- `DEBBUILDER_OIDC_*`
- `DEBBUILDER_NTFY_TOKEN`
- `GNUPGHOME`

Secrets and local runtime state are stored under `DEBBUILDER_DATA_DIR` (the source-tree `data/` directory by default) and are not intended for Git. Packaged deployments use `/var/lib/debbuilder`; their non-secret defaults are installed from `packaging/debbuilder.env` into `/etc/debbuilder/debbuilder.env` without overwriting an existing administrator-owned file.

Build tools are resolved from the same effective `PATH` used to run build commands. Administrators can extend the DebBuilder service's `PATH` in `/etc/debbuilder/debbuilder.env`, while a Recipe can provide a build-specific `PATH` through its build environment. Tools found there do not need to be owned by a Debian package; manually added build dependencies remain Debian packages checked with `dpkg-query`.

## Execution history and workspace retention

A Run directory contains both persistent history and disposable build data.
Automatic cleanup only removes the fixed entries `source/` (including compiler
outputs), `staging/`, `downloads/` and `source.tar.gz`. It keeps `run.json`, the
Recipe snapshot, logs, manifests, final `.deb` artifacts and validation records
(including the previous artifact copied for upgrade validation). Validation and
publication use the retained artifact and metadata, not the source or staging
trees. Unknown workspace entries are retained.

Settings → Maintenance exposes `workspace_cleanup.enabled` (default `true`) and
`workspace_cleanup.failed_workspaces_to_retain` (default `5`, integer 0–1000).
These are application settings, also available through GET/POST `/api/settings`;
existing settings files receive the defaults without a Recipe migration.

Cleanup runs after a build/dry-run request and its configured automation finish,
and in a background sweep at server startup and every five minutes. Completed
successful/prepared runs are eligible immediately, even if manual validation or
publication will happen later. The five most recent failed/cancelled workspaces
are kept globally across all Recipes, ordered by the latest lifecycle completion
time; older failures are cleaned. Failed dry-runs follow the same rule. Already
cleaned workspaces do not consume retention slots. There is no age limit or
automatic deletion of final artifacts/history in this policy.

“Delete log/history” and “Clear execution history” remove completed execution
history and detailed output, and also reclaim disposable workspace data even
when automatic cleanup is disabled. Recipes, managed Packages and APT contents
are unaffected. A separate `.execution-history-deleted.json` tombstone keeps the
Run absent from Logs despite later metadata rewrites/restarts. List APIs omit
deleted history, detail/log APIs return 404, and repeated deletion is idempotent.
DELETE returns the `workspace_cleanup` result alongside the history deletion.
Active or leased executions return HTTP 409 (`execution_active`); clear-all
excludes them and reports per-execution failures if a state changes after its
preview. Deletion never cancels a build, validation or publication.

Build, validation, publication, reconciliation and cleanup share a per-Run
filesystem lock, including across server processes. Cleanup takes this lock
without waiting and re-reads canonical metadata before deleting. It opens the
builds root and Run using directory descriptors with symlink following disabled,
rejects traversal, mismatched Run/workspace identities, unsafe metadata and
top-level symlink/mounted targets, and uses descriptor-relative, symlink-safe
recursive deletion. Nested symlinks are unlinked without visiting their targets.
An artifact recorded inside a disposable tree blocks cleanup. Missing disposable
entries are safe to retry; `.workspace-cleanup.json` records successful removal.
Cleanup errors are reported and retried by subsequent sweeps without changing
the build/lifecycle result. Before removal, Linux `/proc` is checked for processes
whose working directory, executable or open descriptors use the Run workspace;
such a Run is kept even if its metadata says failed. Inaccessible process data
also defers cleanup. Runs still marked active after a crash are preserved until
their state is resolved. Commands run in dedicated process sessions; inactivity
and optional maximum-runtime expiry terminate the complete process group before
the runner returns, so cleanup remains a final safety check rather than process
management.

## Python projects

Python detection recognizes `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `requirements-*.txt`, `Pipfile`, `poetry.lock`, and `uv.lock`. It parses declared build-system, interpreter, dependency, and entry-point metadata without executing project files and without translating PyPI names into Debian package names.

Projects with an explicit PEP 517 build system receive `python3 -m build` as a reviewable proposal. Source applications such as DebBuilder have no compilation step: the selected runtime files are packaged directly. A lone helper `.py` file is not a strong Python marker.

## Packaged service privileges

The current package runs `debbuilder.service` as root. Real builds write isolated workspaces, OCI validation starts privileged systemd containers through Podman, and APT publication needs access to reprepro and its signing keyring. A dedicated unprivileged service account would require a separately designed rootless-Podman setup (including subordinate IDs and runtime directories) or privileged helpers; the package does not pretend that such a boundary already exists.

## Run locally

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:8099
```

## Tests

```bash
python3 -m py_compile server.py debbuilder/*.py
python3 -m unittest discover -s tests -v
for file in $(find static -name '*.js' -type f); do node --check "$file"; done
git diff --check
```

## Repository access command

The sidebar displays an install command derived from `DEBBUILDER_REPO_URL`:

```bash
curl -fsSL https://repo.example.invalid/install.sh | sudo bash
```

Clicking the command copies it to the clipboard.

## Safety notes

DebBuilder is meant to be self-hosted and operated by trusted administrators.

- A real build requires an explicit Build action and confirmation; Test creates
  a distinct dry-run Build Run.
- Build commands execute only through the central runner with `shell=False` and
  a confined Build Run workspace.
- Administrative API routes can be protected with OIDC or a trusted reverse-proxy header.
- Public APT files under `/dists/*`, `/pool/*`, `/repository.gpg` and `/install.sh` stay accessible without authentication.

## Version

The current release is DebBuilder 0.1.6.
