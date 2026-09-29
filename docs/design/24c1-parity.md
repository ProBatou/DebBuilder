# #24 frontend migration parity matrix

## Current state

The Svelte app under `frontend/` is the cutover-ready admin source. The managed
package build compiles it into the installed `static/` tree. Production
deployment has not happened.
The isolated `prototypes/issue-24` app and its B6 screenshots are historical
design references. Operator-reviewed Svelte behavior takes precedence over
older prototype interactions. "Migrated" below describes the Svelte candidate,
not the deployed admin UI.

| Surface | Current Svelte state | Remaining scope |
| --- | --- | --- |
| Auth/session | Partially migrated: auth/status bootstrap, same-origin requests and 401 sign-in handling | Real OIDC and trusted-proxy browser qualification before cutover |
| Overview | Partially migrated: dashboard, diagnostics, exact Run/package navigation, guarded Build update, Validation and Publication actions | Broader live-state qualification |
| Packages | Partially migrated: managed list/detail, Package creation through canonical Recipe draft and create-only Save, guarded next actions | Additional lifecycle coverage and cutover |
| Repository inventory | Migrated read-only: independent exact reprepro snapshot on demand | None for read behavior |
| Recipes and editor | Migrated for canonical v5 reads, lossless draft, Plan/Customize/Advanced/Expert, guarded Save and managed overrides | Further editor edge-case qualification |
| Recipe Create | Migrated through Package creation; standalone New Recipe entry is intentionally hidden | Standalone creation is a product decision, not a parity blocker |
| Recipe JSON | Partially migrated: deliberate Expert view, copy/export, file import/edit, validate and apply to the local draft | No direct import persistence or overwrite action |
| Recipe Test/Build | Migrated: validate the same frozen draft before admission; `dry_run` distinguishes Test and Build; exact returned Run ID | No implicit Save or automatic retry after ambiguous failure |
| Runs and logs | Partially migrated: selection, curated stages/diagnosis, direct verbosity, rendered-character log cursor, scoped polling | Run cancellation and broader live failure cases |
| Validation lifecycle | Migrated for eligible Run/package/Overview actions with current-state recheck | Further live backend qualification |
| Publication | Migrated for eligible Run/package/Overview actions with exact identity confirmation and current-state recheck | Further live backend qualification |
| System Health | Migrated read-only with curated checks and managed self-build settings | Capability and partial-state qualification |
| System Maintenance | Partially migrated: storage/recovery facts and preview-confirmed execution-history deletion | Other destructive maintenance operations deferred |
| System Developer | Partially migrated: selected Recipe/Run inspectors, support bundle and deliberate raw diagnostics disclosure | Further auth/error qualification |
| Settings | Partially migrated: local Theme/Language and bounded backend settings forms, including secret configured state | Full legacy settings/secret parity and cutover qualification |
| Automation | Partially migrated: authored Recipe policy through guarded Recipe Save and global Settings fields | Dedicated automation actions and observation refresh deferred |
| Public APT landing | Not started in production | Separate public listener/template deployment |
| Production packaging integration | Migrated: managed Recipe and release workflow compile Svelte | Production deployment |
| Production static serving | Migrated: admin handler serves compiled assets with MIME and cache policy | Production deployment |
| Vite dev-server dependency | No | None |
| Runtime Node dependency | No | Build hosts need Node 24/npm 11 |
| Fresh install and upgrade qualification | Passed in disposable Debian container with `dpkg -i`, post-install bootstrap, obsolete-file removal and configuration preservation; packaged handler and API routes passed separately | systemd restart on a supported host |
| Public APT isolation | Passed in focused routing tests | Production deployment |
| Production deployment | Not done | Explicit operator deployment checkpoint |

## Contracts and deferred actions

`/api/packages` is the managed Package projection; `/api/repository/inventory`
is an independent reprepro read. Package and Run Recipe links use exact returned
Recipe IDs. Application-managed Recipes open under System. Test and Build use a
single immutable draft snapshot and navigate to the exact admitted Run ID.
The current public Run DTO does not prove that a prior Run used the current
Recipe revision, so Plan does not claim matching evidence.

The Svelte candidate does not expose Recipe Delete or Rename, Run cancellation,
direct Recipe import persistence, observation refresh, or dedicated automation
mutations. Existing backend support is not itself a frontend migration. The
separate public APT landing remains a reference.

Earlier C1/C2/C3 checkpoints are documented in the Git history and
[C3 visual audit](visual-parity-c3.md); their old feature lists are historical.
