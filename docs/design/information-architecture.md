# #24A — Information architecture and normal path

## Product objects and navigation

Keep the backend chain **GitHub Source → Recipe → Run → Package artifact → Validation → Publication → APT repository**. Show the chain as a package journey with explicit links and version/ref/proof, not seven permanent top-level pages. A Source is a resolved immutable identity for a particular Run; a Recipe is a reusable plan; a Run is an immutable execution snapshot; a Package is the managed name/projection plus artifacts. Validation belongs to an artifact/Run, publication belongs to an exact validated artifact. Never imply Build success means publishable.

Proposed admin navigation:

1. **Overview**: needs attention, active work/queue, recent packages, repository health. Primary “Create package” entry.
2. **Packages**: package list and detail with lifecycle timeline, versions, source/Recipe/Run links, validation and publication actions. Repository inventory as a tab or linked view, not an unrelated dashboard fragment.
3. **Recipes**: list and editor. A new package flow can create the package record and draft Recipe in sequence using existing APIs; do not claim atomic creation. Source, Build, Installation and Service become task sections with a persistent resolved-plan summary and one primary next action.
4. **Runs**: replaces “Logs”; list, queue, Test/Build detail, stages, diagnostics, logs, validation/publication history. Preserve direct links from Package and Recipe.
5. **System**: health/capabilities, diagnostics, support bundle, Recipe/Run inspectors, raw OpenAPI link under a Developer disclosure. System is clearer for the top-level operational purpose; Developer is a subsection.
6. **Settings**: integration/auth/defaults. Move Storage & maintenance to System > Maintenance, preserving settings-backed controls and confirmation.

This is a conceptual grouping, not a new backend resource model. Keep repository public landing outside admin navigation/auth. A future source provider could enter the Source step after it exists; no generic Git or arbitrary URL choice appears now.

## Built-in DebBuilder Recipe

Present it as **System-managed self-build** in System (with links to its Runs/artifact), not as an ordinary editable Recipe row. The current picker shows a “Built-in” badge and disables most fields; that still suggests a normal Recipe with mysterious restrictions. Keep the underlying Recipe v5 and managed editable-path rules unchanged. A read-only plan plus clearly named operational overrides (active, maintainer, build environment/timeouts) is more honest. If it remains discoverable from Recipes, place it in a separate “Managed by DebBuilder” section.

## Normal package journey

1. **Choose GitHub source**: repository, release/tag or source archive/Release asset; ask only for `owner/repo` and the choice needed for that source. Label raw single-file assets and upstream `.deb` distinctly. Tracking policy can default to latest release when appropriate but show the chosen rule.
2. **Resolve exact source**: show tag/ref, immutable commit or asset identity and version proposal. Existing resolver/Test output is authoritative; do not imply an unimplemented preview endpoint. If a fresh resolver preview is needed before Test, propose a separate bounded API change.
3. **Detect and propose**: use existing project detection for Python/Node/Rust and source inspection. Explain detected project, build tools, proposed command, output strategy. Mark `Detected`, `Suggested`, `User override`, and `Resolved` separately; suggestions are not silently saved.
4. **Review the plan**: plain-language summary of source, build/no compilation, package files and destinations, account/service/runtime directories, runtime Depends and host capability. Explicit unknowns and Test-only checks remain visible. Keep advanced controls reachable.
5. **Test**: async queued/running/prepared/failed/cancelling/cancelled; preflight prepares and checks but does not execute build commands or `dpkg-deb`. The UI must state that boundary. Closing the modal does not cancel the Run.
6. **Build**: submit once, show queue and Run progress, preserve cancellation/recovery and immutable snapshot. Show output artifact identity and provenance.
7. **Validate**: explicit per-artifact attempt, with dependency/environment readiness, cancellability and result. Do not infer from Build.
8. **Publish**: explicit eligibility and confirmation against exact artifact/proof; show lock/contention and retryable failure. Then link to Repository and public install instructions.

A summary sidebar on desktop and compact sticky review/next-action control on mobile can reduce context switching. It must never hide warnings, resolved values or fail-closed gates.

## Real package examples

**Zoraxy:** GitHub Release raw binary → exact asset → one executable mapped to `/usr/local/bin/zoraxy` → service command and WorkingDirectory `/opt/zoraxy`. Current `install.directories` can declare `/opt/zoraxy` with owner/group/mode; the UI can suggest it from WorkingDirectory after the user confirms, avoiding a trivial `preinst`. Do not silently create arbitrary external paths. Existing `service.working_directory` alone does not create a directory.

**Pocket-ID:** prebuilt executable plus service account. Existing `install.account`, `install.directories` and `install.config_files` with `create_if_missing` support account, persistent `/opt/pocket-id/data`, and source-backed config file behavior. `service.environment_files` references a file but does not generate it. A new generated secret/value and write-once environment-file model requires a bounded backend contract with ownership, mode, persistence, upgrades, purge, validation and preview rules. Do not present it as shipped. The operator supplies app-specific values such as URL and policy flags. Maintainer scripts remain an advanced escape hatch until such a contract is approved.

**#30 runtime libraries:** the Recipe enables amd64 Release-asset ELF detection and holds manual Depends/overrides; the Build’s staging result supplies detected, manual, bundled, overridden, unresolved and effective Depends. Present a compact “Runtime libraries: ready / review needed” summary, a reviewed effective Depends list, and separate detected/manual/bundled/override rows. Zero requirements should read “No external runtime libraries detected.” Put SONAME, `readelf`, `dpkg-shlibdeps`, resolver profile and raw override JSON in Advanced/diagnostics. Unresolved requirements block the happy path and explain the safe next action; do not promise automatic resolution beyond the actual backend.

## Public APT page

Share the DebBuilder name, type scale and status clarity but use a lean independent public stylesheet/template. Show suite/component, key fingerprint if provided by public metadata, signed metadata link and concise install instructions. Consider a generated read-only package/version list only if it can be safely derived from published metadata and users actually need discovery; avoid an admin API dependency, live controls or authenticated state.
