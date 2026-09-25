# #24A — Progressive disclosure and backend boundary

A default screen should state the **effective plan** and provenance of each value: `Detected` (observation), `Suggested` (editable proposal), `Configured` (operator choice), `Resolved` (authoritative Test/Build result), or `Unknown` (not checked). Never use “Resolved” for a guess. Advanced is a stable, discoverable section per domain, not a hidden JSON-only escape hatch. Warnings and blockers remain visible in default view.

| Domain | Simple/default | Advanced and what can be inferred |
| --- | --- | --- |
| Source | GitHub repository and release/tag/source archive/asset choice; exact resolved identity/version before action when available. | Tracking/ref, asset pattern/exact name, archive format/tree selection, version expression. Resolve commit/asset and inspect archive through existing source/Test paths; do not offer #25 providers. |
| Build | Detected project, tools, proposed command or “No compilation required,” output summary, missing tool action. | Commands, environment, working directory, timeouts, source changes, `ensure_directories`, output paths. Project detection proposes Python/Node/Rust/static models; confirmation stays explicit. |
| Package | Name, architecture, summary, effective install destination and contents. | Section, priority, maintainer, revision, fine permissions/mapping policy. Defaults come from Recipe v5; label them as defaults. |
| Runtime dependencies | Readiness, detected/manual/bundled/unresolved counts and effective Depends after Build. | ELF toggle, manual Depends and override reasons, exact SONAME/probe/resolver detail. #30 is scoped to supported amd64 Release-asset payloads; Test cannot claim final ELF Depends until staging/Build has produced them. |
| Installation/files | “Install binary here” and preview of actual files; persistent directory needs as named choices. | Custom mappings, owner/group/mode, `create_if_missing` config mapping, explicit directories and post-build empty output dirs. Infer safe candidate runtime directory from service WorkingDirectory; operator confirms. Distinguish build output directory from installed runtime directory. |
| Account | Service account name and “Create if needed” with preview. | Separate payload owner, group, account creation flags. Existing account provisioning is present; show host/target effects precisely. |
| systemd service | Enable service, command, account, restart on failure, WorkingDirectory if relevant. | Type, environment/files, `After`/`Wants`/`Requires`, `KillMode`-related settings, stop/start timeouts, `ExecStartPre/Post`, logging. Existing unit generation does not materialize environment files. |
| Resource limits | Inherited/default state and optional Memory/CPU/Tasks/I/O ceilings, plus host capability and impact. | Precise cgroup/systemd enforcement, units and fallback behavior. Display diagnostics before accepting a finite limit; preserve backend fail-closed admission. |
| Validation | Eligibility, environment readiness, Run/artifact identity, start/retry/cancel and result. | Container/runtime details, raw diagnostics, dependency preparation and attempt IDs. Availability must be reported by backend, not guessed. |
| Publication | Exact artifact/proof, target repository, action and resulting version. | Distribution/component, lock/contention, proof diagnostics, metadata. Preserve confirmation and exact-artifact semantics. |
| Automation | Off/manual default, policy summary, last/next check and detected update. | Schedule/retry/generation/revision and per-stage policy. Never disguise an automatic Publish policy as merely “check for updates.” |

## Capability classification for Zoraxy/Pocket-ID

| Need | Classification | Design implication |
| --- | --- | --- |
| Raw single binary and explicit destination/mode | Backend already supports | Offer a simple “single executable” install path backed by Release asset + mapping; no schema change. |
| Service WorkingDirectory and environment-file reference | Backend already supports | Show in resolved plan, with warning if path/file not provisioned. |
| Runtime/persistent directory with owner/group/mode | Backend already supports (`install.directories`) | UX can expose existing capability better and suggest a package-owned path. Validate ownership/order through existing Test. |
| Account/group provisioning | Backend already supports (`install.account`) | UX can expose existing capability better; keep creation flags inspectable. |
| Source-backed file installed only when absent | Backend already supports (`install.config_files` `create_if_missing`) | UI should explain upgrade/purge behavior and file source requirement. |
| Create an environment file from operator-entered values without a source template | Bounded backend capability would be required | Specify separate lifecycle/security contract before UI promise. |
| Generate and persist a secret/value safely | Bounded backend capability would be required | Must define generation timing, idempotency, backup/upgrade/purge, permissions, preview redaction and validation. |
| Auto-infer every application-specific URL/policy flag | Future/not justified | Ask operator for application values; do not guess Pocket-ID policy. |
| Generic Git/Archive URL choices | Future/not justified (#25 deferred) | Do not render as available. |

Current directory creation is declarative but not automatic inference. The Zoraxy workaround can be removed with better UI over `install.directories`; Pocket-ID’s generated secret is a genuine gap. Existing `build.ensure_directories` solves a different post-build output case and must not be presented as runtime provisioning. Raw maintainer scripts remain supported for exceptional lifecycle logic.
