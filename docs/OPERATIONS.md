# Operations

This document covers configuration, retained data, recovery, and host process
behavior for DebBuilder operators.

## Configuration and state

Packaged deployments use `/etc/debbuilder/debbuilder.env` for non-secret
defaults and `/var/lib/debbuilder` for persistent state. Package installation
creates the environment file only when it is absent, preserving an existing
administrator-owned file.

Source checkouts default to the repository's `data/` directory. Python startup
reads process environment variables and does not load `.env` itself; source
`.env` before running `server.py`.

Persisted `settings.json` documents use strict schema version 2. A complete,
valid version-1 document is accepted through the one supported compatibility
adapter, which adds the filesystem-pressure defaults in memory without startup
write-back; the next authorized Settings mutation persists version 2. Partial,
unversioned, invalid-v1, future, malformed, and unknown-field documents are
rejected. `secrets.json` remains strict schema version 1 and must be an
owner-only (`0600`) regular file. The literal `masked` is accepted only as a
preserve-existing sentinel during secret mutation, never as a stored secret
value.

Persisted OIDC configuration requires its OIDC client secret at startup. The
separately provisioned session-cookie secret cannot replace it.

Build tools are resolved from the effective service `PATH`. Administrators can
extend that path in `/etc/debbuilder/debbuilder.env`; Recipes can provide a
build-specific `PATH`. Manually selected build dependencies remain Debian
packages checked through `dpkg-query`.

## Workspace retention

A Run directory combines persistent history and disposable build data.
Automatic cleanup removes only `source/`, `staging/`, `downloads/`, and
`source.tar.gz` according to failed-workspace retention, and removes the
recreatable Run-local `toolchain/` from every terminal Run. The toolchain's
requested and resolved Node/package-manager identity remains in `run.json`;
the shared immutable cache under the data directory's top-level `toolchains/`
is never a Run cleanup target. Cleanup retains Run and Recipe metadata, logs,
manifests, final `.deb` artifacts, Validation records, and artifacts retained
for upgrade Validation. Unknown workspace entries are retained.

Settings > Maintenance controls cleanup. It is enabled by default and keeps
the five most recent failed or cancelled disposable workspaces globally.
That preference retains their source/staging evidence, not their Run-local
toolchain state. The latter is disposed after terminal completion because it
contains per-Run HOME, npm/pnpm stores, Corepack state, and entry points that
can be recreated from the durable identity and shared cache.
Successful or prepared Runs become eligible after completion, even if manual
Validation or Publication follows later. There is no automatic age-based
deletion of final artifacts or history.

The application-owned maintenance worker performs cleanup after startup and at
regular intervals, and refreshes the cached storage inventory. `GET
/api/storage` reads that snapshot without walking or mutating the filesystem.

The inventory separately measures the filesystem containing `DATA/builds`
with a pinned-descriptor `statvfs` call. Before `builds` exists, its safe `DATA`
parent supplies the capacity measurement. Available capacity uses `f_bavail`,
preserving filesystem-reserved blocks for host operation. The repository is
measured independently and is identified as sharing the Builds filesystem when
both roots have the same device ID. Settings define minimum-free byte/percent
thresholds and higher target byte/percent thresholds. This release reports the
resulting `normal`, `pressure`, or `measurement_error` state. After ordinary
workspace retention and repository-backed pruning, a `pressure` state on the
Builds filesystem may override `failed_workspaces_to_retain`: retained failed
or cancelled Runs are reconsidered oldest-first, and only `source/`,
`staging/`, `downloads/`, and `source.tar.gz` may be removed. Candidates must
have been deliberately retained by the count policy without a blocked or
failed normal cleanup attempt. Each candidate is re-read under the same Run
lease, recovery, containment, process, mount,
symlink, and artifact checks as ordinary cleanup. Capacity is remeasured with
the pinned-descriptor O(1) probe after each effective cleanup, and deletion
stops when the higher target is reached, measurement fails, or safe candidates
are exhausted. Repository-only pressure never triggers workspace cleanup.

Build/Test admission and execution start use the same process-lifetime
filesystem state machine as maintenance. A fresh O(1) Builds capacity measure
is taken immediately before a new Run workspace is created (or an existing
preallocated pending Run is submitted), and again immediately before a queued
Run enters the pipeline. Builds pressure or an unavailable measurement blocks
the boundary fail-closed and requests maintenance asynchronously. A refusal at
admission creates no workspace; a queued Run refused at execution start is
persisted as failed with stage `storage_guard` without source acquisition or
staging. Repository-only pressure does not block Builds. These guards remain
active when automatic cleanup is disabled.

The checks reduce ENOSPC risk but do not reserve space: a normal measurement
authorizes only that instant, and an already-running Build can consume capacity
afterward. Quotas, byte reservations, and future Build-size estimates are not
part of this contract.
Byte thresholds are limited to `9007199254740991` (`2^53 - 1`) so every value
accepted by the backend remains exactly representable through the supported
JSON/JavaScript Settings clients. Changing any pressure threshold rebases the
next capacity refresh from the cold `unknown` state; the Settings mutation
only requests that asynchronous refresh and performs no filesystem scan.
Disabling automatic workspace cleanup also disables pressure cleanup, while
capacity measurement and pressure reporting remain active.

Deleting one Run's log/history or clearing execution history also requests
disposable-workspace cleanup, even when automatic cleanup is disabled. It does
not remove Recipes, managed Packages, or APT contents. Active,
recovery-blocked, or leased Runs return HTTP 409 rather than being deleted.

## Locking and filesystem safety

Build, Validation, Publication, reconciliation, and cleanup share a per-Run
filesystem lock across server processes. Cleanup takes the lock without
waiting, re-reads canonical metadata, and uses descriptor-relative operations
with symlink following disabled. Traversal, mismatched Run identities, unsafe
metadata, top-level symlink or mounted targets, and artifacts recorded inside a
disposable tree block removal. Nested symlinks are unlinked without visiting
their targets.

Before removing a workspace on Linux, DebBuilder checks `/proc` for processes
whose working directory, executable, or open files use that workspace.
Inaccessible process data defers cleanup. Failures are recorded and retried
without changing the Build result.

APT mutation has an additional repository-root lease; see
[APT repository operations](APT_REPOSITORY.md).

## Process containment and recovery

On Linux with a reachable system systemd manager and unified cgroup v2, each
Run command is started directly by PID 1 in its own transient service. Output
is returned through command-scoped file descriptors, and cancellation or
timeout terminates the service cgroup. Capability probing is behavioral; if
that backend is unavailable, DebBuilder uses a weaker dedicated process group.

The strong backend relies on Debian's `python3-dbus`. Containment reduces
accidental orphan leakage but is not a security sandbox: root-executed build
code can escape it.

At startup, interrupted `pending`, `queued`, `running`, and `cancelling` Runs
are reconciled before new execution admission opens. A Run is marked failed
only when workload absence is authoritative. If DebBuilder cannot safely
identify or exclude an old workload, browsing remains available but new
Build/Test submissions return 503. Destructive cleanup also remains disabled
until recovery is resolved. For current-boot process-group fallback records, a
host reboot provides the authoritative process boundary.

## Service privilege

The packaged `debbuilder.service` currently runs as root. Builds write isolated
workspaces, Validation starts privileged systemd containers through Podman,
and Publication needs `reprepro` and signing-key access. A future unprivileged
service would require a deliberately designed rootless-Podman configuration or
privileged helper boundary; the current package does not claim one.
