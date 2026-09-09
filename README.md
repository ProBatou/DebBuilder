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

Build/dry-run completion and queued cancellation request cleanup from the
application-owned maintenance worker; execution does not synchronously scan all
historical Runs before dequeuing the next one. The worker also refreshes a
read-only storage inventory at startup, every five minutes and after relevant
lifecycle changes. Broader startup/periodic destructive retention remains
deferred to the dedicated cleanup-lifecycle work; the observer's periodic timer
does not itself request deletion. Completed successful/prepared runs are eligible
when cleanup is requested, even if manual validation or publication will happen
later. The five most recent failed/cancelled workspaces are kept globally across
all Recipes, ordered by the latest lifecycle completion time; older failures are
cleaned. Failed dry-runs follow the same rule. Already cleaned workspaces do not
consume retention slots. There is no age limit or automatic deletion of final
artifacts/history in this policy.

GET `/api/storage` returns only the maintenance worker's cached snapshot; it does
not walk or mutate the filesystem. Settings → Maintenance shows compact totals
for managed data, Runs, disposable workspace data, final artifacts and the APT
repository. Partial, stale, collecting and error states are explicit. Repository
storage nested under the data root is counted once in the managed total.

“Delete log/history” and “Clear execution history” remove completed execution
history and detailed output, and also reclaim disposable workspace data even
when automatic cleanup is disabled. Recipes, managed Packages and APT contents
are unaffected. A separate `.execution-history-deleted.json` tombstone keeps the
Run absent from Logs despite later metadata rewrites/restarts. List APIs omit
deleted history, detail/log APIs return 404, and repeated deletion is idempotent.
DELETE returns the `workspace_cleanup` result alongside the history deletion.
Active, recovery-blocked or leased executions return HTTP 409
(`execution_active`); clear-all
excludes them and reports per-execution failures if a state changes after its
preview. Deletion never cancels a build, validation or publication.

APT publication and exact reconciliation additionally use a fail-fast,
repository-root-scoped filesystem lease. The canonical acquisition order is
application `MutationGate`, then Run workspace lock, then repository lease; a
Run lock cannot be acquired while the repository lease is held. The lease pins
the repository directory and is inherited by the `reprepro` child, so a second
thread or process cannot enter a publication while that child is alive.

A publication becomes successful only after one exact database entry, one
exported `Packages` entry, and its safely opened `pool/` file agree with the
retained source artifact on distribution, component, package, version,
architecture, size, and SHA-256. That bounded result is stored as a versioned
publication proof. Historical success records without such a proof remain
unverified until explicit reconciliation succeeds. DebBuilder supports the
standard repository layout under the configured root and rejects path
redirections that prevent safe proof; administrator-managed reprepro signing
and hook configuration remains trusted configuration, not a sandbox boundary.
Public APT downloads remain lock-free and stream a pinned, no-follow file
descriptor rather than holding the mutation lease for a client connection.

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
also defers cleanup. A Run with unresolved recovery is preserved even when its
top-level status looks terminal, and any unresolved global startup-recovery
blocker disables all destructive cleanup while leaving storage observation
available. Runs still marked active after a crash are preserved until their state
is resolved. On Linux hosts with a reachable system systemd manager
and unified cgroup v2, each Run command is spawned directly by PID 1 in a unique
transient service. DebBuilder receives stdout/stderr through command-scoped file
descriptors and terminates the complete service cgroup on cancellation or
timeout. Capability probing is behavioral; unavailable strong containment uses
the explicitly weaker dedicated-process-group fallback. The strong backend uses
Debian's `python3-dbus`, which packaged deployment Recipes should declare in
their `package.runtime_dependencies`; its absence safely selects the fallback.
This containment prevents accidental orphan leakage, but root build code can
escape it and it is not a security sandbox.

At server startup, persisted `pending`, `queued`, `running`, and `cancelling`
Runs are reconciled before the execution worker opens admission. Interrupted
Runs are marked failed only after workload absence is authoritative. If an old
workload cannot be safely identified or proven absent, DebBuilder remains
available for browsing and history inspection but returns a deterministic 503
for new Build/Test submissions until a later startup can resolve the blocker.
Current-boot process-group fallback records remain blocked because escaped
descendants cannot be excluded; a host reboot provides an authoritative boot
boundary.

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

### Visual UI tests

The Playwright workflow starts the real DebBuilder server on a free loopback
port with a fresh temporary `DEBBUILDER_DATA_DIR` and repository directory. It
loads the existing Recipe fixtures plus deterministic showcase Build Runs, and
removes that isolated runtime after the tests.

```bash
npm install
npx playwright install chromium
npm run test:ui
```

Desktop and mobile captures are written to `.ui-artifacts/desktop/` and
`.ui-artifacts/mobile/`. The local HTML report and failure traces are kept below
`.ui-artifacts/` as well; the whole directory is ignored by Git.

### DEV Behavior Lab

List the available isolated DEV scenarios:

```bash
python3 -m tests.ui.behavior_lab --list-scenarios
```

Launch the static UI showcase on a LAN-accessible DEV port:

```bash
python3 -m tests.ui.behavior_lab --scenario showcase --host 0.0.0.0 --port 8765
```

The default bind is the safer `127.0.0.1`; when using `0.0.0.0`, open the Repo
VM hostname/IP from another machine. The Lab uses disposable temporary data and
repository directories, and removes them when stopped. Press Ctrl-C to stop the
Behavior Lab.

- `showcase` — static Runs, packages, and Recipes for UI review.
- `cancellation-running` — a live local process tree that can be cancelled.
- `queued-cancellable` — a Run queued behind a local blocker; cancel it before start.
- `build-failure` — a harmless failing command with stdout and stderr.
- `prepared-test` — static prepared Test and staging-preview state.
- `graceful-shutdown` — active and queued Runs; Ctrl-C exercises normal shutdown.
- `recovery` — an old-boot interrupted Run is terminalized at startup.
- `recovery-blocked` — unresolved recovery keeps admission fail-closed.

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

The current release is DebBuilder 0.1.11.
