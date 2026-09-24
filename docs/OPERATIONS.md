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

Persisted `settings.json` and `secrets.json` documents use strict schema version
1. Missing documents use environment-derived defaults in memory. Partial,
unversioned, older, newer, malformed, and unknown-field documents are rejected
without startup write-back. The secret store must be an owner-only (`0600`)
regular file. The literal `masked` is accepted only as a preserve-existing
sentinel during secret mutation, never as a stored secret value.

Persisted OIDC configuration requires its OIDC client secret at startup. The
separately provisioned session-cookie secret cannot replace it.

Build tools are resolved from the effective service `PATH`. Administrators can
extend that path in `/etc/debbuilder/debbuilder.env`; Recipes can provide a
build-specific `PATH`. Manually selected build dependencies remain Debian
packages checked through `dpkg-query`.

## Workspace retention

A Run directory combines persistent history and disposable build data.
Automatic cleanup removes only `source/`, `staging/`, `downloads/`, and
`source.tar.gz`. It retains Run and Recipe metadata, logs, manifests, final
`.deb` artifacts, Validation records, and artifacts retained for upgrade
Validation. Unknown workspace entries are retained.

Settings > Maintenance controls cleanup. It is enabled by default and keeps
the five most recent failed or cancelled disposable workspaces globally.
Successful or prepared Runs become eligible after completion, even if manual
Validation or Publication follows later. There is no automatic age-based
deletion of final artifacts or history.

The application-owned maintenance worker performs cleanup after startup and at
regular intervals, and refreshes the cached storage inventory. `GET
/api/storage` reads that snapshot without walking or mutating the filesystem.

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
