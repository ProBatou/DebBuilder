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

Settings > Advanced controls cleanup. It is enabled by default and keeps
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

## Recovering from storage pressure / ENOSPC

Use this runbook when Build/Test submission is refused for storage pressure,
queued Runs fail at stage `storage_guard`, or the host reports that the
filesystem containing `DATA/builds` is full. Recovery is progressive: first
observe and allow the managed cleanup to work, then free a small amount of
host space outside DebBuilder state if the filesystem is too full for the
application to make progress. Manual deletion inside DebBuilder's data or
repository trees is not a supported recovery shortcut.

### Confirm the incident

1. Stop submitting new Builds/Tests and pause other optional writers to the
   affected filesystem. Already-running Builds are not covered by a space
   reservation and can continue consuming capacity.
2. In System > Maintenance, check the storage snapshot status, managed storage
   and disposable-workspace totals, repository total, retention policy, the
   execution-recovery card, and the latest recorded workspace cleanup. The
   recovery card concerns execution recovery, not storage pressure. This view
   does not show filesystem capacity, pressure state, or pressure thresholds.
3. Query the authenticated `GET /api/storage` endpoint and inspect
   `storage.filesystems.builds`. Record `measurement_state`, `pressure_state`,
   `available_bytes`, `available_percent`, `effective_start_bytes`, and
   `effective_target_bytes`. Also inspect
   `storage.recent_workspace_cleanups`.
4. Treat `storage.state` separately from filesystem pressure. `collecting`,
   `partial`, `stale`, or `error` describes the cached recursive inventory;
   the Builds filesystem's `measurement_state` and `pressure_state` are the
   capacity/admission signals. Use `storage.diagnostics` for bounded inventory
   diagnostics and `storage.filesystems.builds.diagnostic` for a bounded
   measurement-error diagnostic when present.
5. Confirm the application symptom. A new submission refused before workspace
   creation returns HTTP 503 with `storage_pressure_admission_blocked` (or
   `storage_measurement_unavailable`). A queued Run refused immediately before
   its pipeline starts is persisted as failed at stage `storage_guard`, with
   `storage_pressure_execution_blocked` or `storage_measurement_unavailable`.
   These error details, like exact filesystem pressure, are not displayed as a
   storage-pressure state in System > Maintenance.

The repository filesystem is a separate signal. If
`storage.filesystems.repository.same_as_builds` is true, its capacity mirrors
the Builds filesystem. If it is false, repository pressure must be diagnosed
on that repository filesystem; by itself it neither triggers Builds workspace
cleanup nor blocks Build/Test admission.

### What automatic recovery can remove

The application-owned maintenance worker runs once after service startup,
after every execution attempt, on the implemented Validation
completion/failure paths, and after successful Publication or reconciliation;
it also runs periodically (normally every five minutes). A storage guard that
blocks work requests maintenance asynchronously. Requests may be coalesced
onto the current pass and one follow-up; they do not each guarantee an
immediate independent pass. There is no supported UI button, API endpoint, or
CLI command that forces an immediate maintenance pass.

Automatic cleanup is deliberately limited. Only DebBuilder's guarded cleanup
may remove the entries classified as disposable below. Their classification
is not permission for an operator to remove them manually.

| Classification | Examples | Recovery behavior |
| --- | --- | --- |
| Disposable Run workspace | `source/`, `staging/`, `downloads/`, `source.tar.gz` | Removed by retention and, for safely eligible retained failed/cancelled Runs, oldest-first pressure cleanup. |
| Disposable terminal toolchain | A terminal Run's local `toolchain/` | Removed after terminal completion; the durable identity and shared cache remain. |
| Preserved Run evidence | `run.json`, `recipe.json`, logs/diagnostics, manifests, provenance, Validation and Publication records | Not part of automatic pressure workspace reclamation. |
| Preserved artifacts | Final `.deb` files and artifacts retained for upgrade Validation | Not part of automatic pressure workspace reclamation. |
| Other managed or unknown data | Top-level shared `toolchains/`, Recipes, Packages, APT repository contents, active recovery/containment data, unknown workspace entries | Not part of automatic Builds pressure cleanup. |

Clear execution history is a separate, explicit destructive action: for the
selected completed Runs it hides the history, clears detailed logs and
Validation command output, and also invokes guarded disposable-workspace
cleanup. Do not use that action as a general pressure-maintenance control.

Every removal still passes the Run lease, terminal-state, recovery,
containment, process, mount, symlink, and artifact checks. A blocked or failed
candidate is left in place. Cleanup markers use reasons such as `retention`,
`terminal_run`, and `storage_pressure`; the API and System view provide the
current last marker per visible Run, not an exhaustive audit history. An
absent marker does not prove that no cleanup happened historically: markers
are replaceable, and history deletion hides the Run from this projection.

Pressure recovery is hysteretic. The Builds filesystem enters `pressure` below
the effective start threshold and stays there until available capacity reaches
the higher effective target threshold. Maintenance remeasures capacity after
each effective pressure cleanup and stops at that target, on a measurement or
deletion/marker error, or when no safe candidate remains.

Guard A takes a fresh bounded capacity measurement before creating or
submitting a Run workspace. Guard B repeats that measurement before a queued
Run enters its pipeline. They remain fail-closed when cleanup is disabled, but
they are not quotas, space reservations, or an absolute ENOSPC guarantee; an
already-running Run may still grow after its admission measurement.

### Level A: application responsive under pressure

1. Keep new Builds/Tests paused and leave the service running.
2. Confirm `storage.filesystems.builds.pressure_state` is `pressure` and note
   the available and target values from `GET /api/storage`.
3. Allow the existing maintenance worker to run. Do not clear execution
   history merely to simulate maintenance, and do not repeatedly restart the
   service to request extra passes.
4. Refresh System > Maintenance or `GET /api/storage` after the normal worker
   interval. Look for `storage_pressure` markers and an increase in available
   capacity. No marker can also mean that all candidates were preserved by
   safety gates or that no eligible disposable workspace remains.
5. Resume normal submissions only after a fresh measurement reports
   `measurement_state: ready`, `pressure_state: normal`, and available capacity
   at or above the effective target. The next legitimate submission should
   then no longer receive a storage-pressure admission error.

### Level B: filesystem nearly full

1. Keep Builds/Tests paused. Use the host's standard capacity and mount tools
   to identify the filesystem containing the configured `DATA/builds` path and
   confirm whether the repository shares that device.
2. If DebBuilder remains responsive, first allow the managed cleanup path from
   Level A to run. It knows which workspace entries are disposable and which
   Runs are safe to touch.
3. If too little space remains for ordinary application progress, free only a
   small amount of clearly understood, host-managed space outside the
   configured DebBuilder data and repository roots, according to the host's
   own retention policy. The goal is to restore enough headroom for the
   service and maintenance worker, not to reach the DebBuilder target by
   manual deletion.
4. Recheck `GET /api/storage`, then let automatic cleanup continue until the
   higher target threshold is reached.

### Level C: application or maintenance blocked by ENOSPC

At zero usable bytes, DebBuilder may be unable to write metadata, logs, or
cleanup markers, complete a safe cleanup, or start successfully. Restore host
headroom first; do not rely on application cleanup while the filesystem is
still completely full.

1. Declare the application degraded and stop new Builds/Tests and other
   optional writers on the affected filesystem.
2. Confirm ENOSPC with the host's standard capacity tools and identify the
   mount containing the configured `DATA/builds` path.
3. Determine whether the repository is on the same device before taking any
   repository-related action.
4. Do not delete anything inside `DATA/builds`, the top-level `toolchains/`
   cache, or the configured repository tree by hand.
5. Recover a minimal amount of space from clearly understood host data outside
   DebBuilder-managed roots, following local host policy.
6. Restore the application through the deployment's normal service supervisor
   if it stopped. The packaged service is named `debbuilder.service`; use the
   deployment's established service-management and logging procedures rather
   than ad-hoc server processes.
7. Confirm the API is responsive. A restarted application requests its normal
   startup maintenance pass automatically; there is no separate force command.
8. Inspect `GET /api/storage` for a successful Builds measurement and allow
   the maintenance worker to remove only eligible disposable data.
9. Wait until `pressure_state` becomes `normal` and available capacity reaches
   the effective target. Reaching only the lower start threshold is not
   recovery from an existing pressure state.
10. Review recent cleanup markers and Run states. Investigate candidates left
    blocked by recovery, containment, active processes, mounts, symlinks, or
    protected artifacts instead of deleting around those gates.
11. Verify preserved Run metadata, logs, manifests, Validation evidence, final
    artifacts, shared toolchain cache, and repository contents before
    resuming work.
12. Resume with one legitimate Build/Test and confirm that admission is no
    longer refused, then restore the normal workload gradually.

If the Builds filesystem reports `measurement_error` instead of `pressure`, do
not lower thresholds to bypass the guard. Check that the configured data path,
its safe parent, mount, and service permissions are available, and investigate
backing-storage health through normal host administration. This state does not
prove any one of those causes. Restore the measurement path, then wait for a
fresh `measurement_state: ready` result before resuming submissions. A
successful measurement may return either `normal` or `pressure`; follow Level
A if capacity is still below policy.

### When automatic workspace cleanup is disabled

Capacity measurement, pressure reporting, and both storage guards remain
active. Automatic retention, terminal Run toolchain disposal, and pressure
cleanup do not occur, so no automatic workspace recovery should be expected.
An authorized administrator may re-enable Workspace cleanup in Settings >
Advanced; that mutation requests an asynchronous refresh but does not itself
scan or delete files. Wait for the normal maintenance worker and verify its
results through System > Maintenance and `GET /api/storage`. Clearing execution
history is a separate destructive operator action, not a supported way to
force maintenance. Otherwise, restore space through ordinary host
administration outside DebBuilder-managed roots or repair/expand the affected
filesystem; do not delete workspaces manually.

### Post-recovery checklist

- The service and System view are accessible; `storage.state` is `ready` or an
  understood non-ready snapshot state.
- Builds measurement is `ready`, pressure is `normal`, and there is no
  `measurement_error`. After a latched pressure event, available capacity must
  have reached the effective target; on a cold normal measurement, the lower
  effective start threshold is sufficient for `normal`.
- Repository state is understood independently when it uses another device.
- Recent cleanup reasons and targets match the expected automatic actions.
- Build/Test admission no longer returns a storage pressure or measurement
  error, and queued work no longer fails at `storage_guard`.
- Active Runs have no unresolved recovery blocker.
- Run/Recipe metadata, logs, manifests, Validation evidence, final artifacts,
  managed Packages, shared toolchains, and repository contents remain intact.
- Any skipped cleanup candidate has been investigated through its recorded
  error and safety condition; protections have not been bypassed.
- No untracked manual deletion was performed inside DebBuilder-managed roots.
- The incident record includes the observed thresholds, capacity, cleanup
  markers, host-space action, and recovery time.

### Never delete manually during recovery

Do not manually remove:

- Run directories with `rm -rf` or an equivalent manual command, or their
  `run.json`, `recipe.json`, logs, diagnostics, manifests, provenance,
  Validation/Publication records, or final artifacts;
- the data directory's top-level shared `toolchains/` cache, confusing it with
  one Run's local `toolchain/`;
- managed Packages, repository pool/index contents, APT metadata, or repository
  generation state;
- lock, recovery, containment, process, mount, or symlink data, or
  `.workspace-cleanup.json` cleanup markers to alter eligibility or evidence;
- unknown entries merely because they consume space.

Do not disable guards, lower thresholds below the operational requirement, or
delete around a safety failure to make a Build start. In particular, do not
remove repository data to address Builds pressure, do not delete data from an
active or unresolved-recovery Run, and do not treat cleanup markers as an
exhaustive history. If managed disposable data and safe host cleanup cannot
reach the target, expand or repair the filesystem and keep Build/Test admission
paused.

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
