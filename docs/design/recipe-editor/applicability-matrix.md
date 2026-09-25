# Recipe editor applicability and progressive disclosure

These rules are UI projections of **current** Recipe v5 validation and pipeline behavior. An editor may hide a control only after preserving its authored value and showing an inspectable inactive-value notice when a mode change leaves non-default data behind. Hiding is presentation, not deletion. A Run's immutable Recipe snapshot and Test/Build results outrank guesses from form state. See [field inventory](schema-inventory.md) for every path.

## Mode and lifecycle gates

| Condition known from Recipe or Run | Visible normal path | Conditional / Advanced / Expert | Hidden from normal path and reason |
| --- | --- | --- | --- |
| `artifact.mode=source_build` | GitHub source, detected project, commands/no compilation, output, installation and optional service | source changes, build dependencies/environment/timeouts, ensure directories, mappings, identities, limits | archive payload and upstream `.deb` selectors have no effect |
| `artifact.mode=upstream_archive` | GitHub archive/raw asset selector, payload, installation and optional service; no compilation | archive source/format/exact-pattern and include/exclude; post-acquisition archive inspection | build commands, source changes and ensure directories: pipeline skips them; build output is replaced by resolved archive payload |
| `artifact.mode=upstream_deb` | GitHub `.deb` selection and inspected upstream package identity | match-package/version flags, asset pattern | DebBuilder build/install/service editors: pipeline skips those stages and registers the upstream `.deb`; its own contents require artifact inspection |
| `source.tracking=latest_release` | tracking summary | version source/revision | ref input; resolver chooses release |
| `source.tracking=tag` or `manual` | requested ref | version expression when regex | none of the source choices become exact until acquisition/Test |
| `source.version.source=regex` | selected version rule | bounded expression editor | expression when tag/release_name selected |
| `artifact.mode=upstream_archive` and `archive_source=github_source` | source archive | tar.gz/zip choice, payload selector | Release-asset exact/pattern selector |
| `artifact.mode=upstream_archive` and `archive_source=release_asset` | Release asset | exact name **or** pattern, payload selector | GitHub source archive format choice; source schema requires exactly one exact name or pattern |
| `artifact.archive_source=auto` | source choice as Auto, unresolved | explain which candidate Test selected | do not claim a release-asset-only capability based on a possible auto result |
| `artifact.payload.mode=paths` | selected path count/summary | archive tree include/exclude editor; include required | none |
| `artifact.payload.mode=entire_archive` | whole archive summary | exclusions | include selectors are normalized away |
| `install.content.source=build_output` | output → destination mapping | extra config mappings, ownership/modes | none |
| `install.content.source=configured_files` | mapping summary and missing-mapping warning | mapping editor | install destination and automatic-output summary are inapplicable; destination must be empty |
| `service.configured=false` | one “Configure service” entry | nothing else unless inspecting inactive imported fields | all unit controls; `enabled=true` is invalid without name+command |
| `service.configured=true`, `service.enabled=false` | unit configured but not enabled at install | edit name/command/identity/restart, enable toggle, advanced unit controls | do not describe it as absent: package still contains a unit |
| `service.enabled=true` | enabled unit, command, effective user/group if set, restart policy and directory/file references | advanced directives and hooks | nothing service-related hidden purely because it is advanced; all remain discoverable |
| `automation.enabled=false` or `policy=manual` | “Manual” / “Automation off” summary | enable + policy choices | periodic controls until enabled; no automatic stage implied |
| `active=false` | inactive notice and activation action | inspect saved plan | Test/Build actions disabled by current UI; no automation eligibility |
| `management.owner=application` | read-only built-in plan and distinct operational overrides | exact allowlist only | ordinary edit/delete/import replacement, source/install/service changes forbidden by backend |

## Runtime dependency decision tree (#30)

1. Show **manual runtime Depends** as a conditional customization when needed for any DebBuilder-built package. It is a simple package-name list; this is distinct from `build.extra_dependencies` (build tools) and from the versioned resolver relations.
2. The Recipe validator permits **automatic runtime library detection** only when `artifact.mode=upstream_archive`, `artifact.archive_source=release_asset`, and `package.architecture=amd64`. The resolver additionally needs copied output candidates: when `install.content.source=configured_files`, `prepare_staging` supplies an empty `copied` list to the ELF analyzer, so a raw file installed solely by mapping is not scanned. Offer the normal opt-in entry only with the three schema gates **and** `install.content.source=build_output`; otherwise hide it or show an explicit “no scanned payload” warning for an already-enabled imported Recipe. `artifact.architecture` must also be valid, but the validator's opt-in gate specifically tests package architecture. An auto-selected archive is not sufficient. The supported resolver profile is Bookworm/amd64.
3. If eligible but `enabled=false`, show at most a compact optional row or a contextual suggestion after a Release asset is selected. Historical Recipes remain disabled. Never turn it on silently.
4. If enabled, show a **status summary** in the Plan: “Will inspect copied payload files at Build” before Build; “No dynamic ELF requirements”, “effective Depends”, or an actionable failure only from actual staging/Build evidence. Test/dry run does not run the final resolver. `readelf` may observe a selected asset earlier, but that is not final package Depends.
5. The Build inspects only final staged copied payload regular files whose bytes start with ELF magic; `install.config_files` mapping targets are not in that candidate list. It resolves dynamic requirements inside the prepared Bookworm/amd64 environment, classifies bundled vs external, merges manual and detected relations, and writes effective `DEBIAN/control`. Static/non-ELF payloads can legitimately yield zero detected relations. `dlopen`/plugins are outside this bounded detector. A failed unresolved requirement blocks packaging.
6. Reveal **SONAME overrides** only from the detection detail or when existing overrides are present. The Expert row is an editable table of SONAME, action `ignore/manual`, required reason and conditional manual relation. Override count <=32, unique names. A nonmatching override fails when there is no matching dynamic requirement; do not promise that a free-form override fixes all dependency errors. Show the exact warning and Run evidence.
7. With `upstream_deb`, source build, GitHub source archive or non-amd64 package, hide the ELF toggle from normal customization and explain in JSON inspection why an imported enabled value would be invalid. Manual Depends can still be meaningful for DebBuilder-built source/archive packages. An upstream `.deb` owns its existing control metadata; the normal UI should not promise to rewrite it.

The current static UI renders the ELF toggle and raw override JSON even when inapplicable. This is a presentation defect, not evidence that the backend supports those combinations. Primary sources: `recipe_schema.py`, `elf_toolchain.py`, `ELF_INSPECTION.md`, and `build_pipeline.py`.

## Identity, files, and hooks

| Condition | Proposed visibility / reason |
| --- | --- |
| Copied build output | Show “Installed files owned by” separately from service identity. It controls generated payload `chown`, not account creation. |
| Non-root owner or service account that must be created | Offer a distinct “Create account on install” choice with user and group. Show resulting generated `postinst` action and `adduser` dependency in the effective plan. The schema does **not** force owner, account and service user to match. |
| Existing target account chosen | Show “Use existing account”; do not promise that it exists until Validation/target behavior proves it. |
| Service user/group blank | Show “systemd default” rather than inheriting account or payload owner. No backend inheritance occurs for `service.user/group`. |
| `service.working_directory` or `service.environment_files` present | Show file/path references and whether the Recipe declares a source-backed mapping/allowed persistent directory. A reference is not a creator. Do not assert existence before Test/Validation. |
| Persistent directory requested | Use `install.directories` only for package-specific `/etc`, `/var/lib`, `/var/log`. `/opt/<name>` cannot be entered here under current v5 validation. Ordinary payload destination may be under `/opt`, but that is a different concept. |
| `install.config_files` present | Show source → destination/policy and owner/mode summary. `create_if_missing` is source-backed; it is not a generated secret or arbitrary runtime file. |
| No custom maintainer script | One Expert row: “Lifecycle scripts — None.” Do not show four empty editors by default. |
| One or more hooks configured | Expert summary names `preinst/postinst/prerm/postrm` and count; open a four-hook editor with explicit generated-action context. These four strings are distinct and order matters within packaging. |
| `resource_limits` all null | Show inherited/unset summary; no host capability claim. Finite values require admission capability checks; the effective limit is the stricter of global and Recipe settings. |
| Extra runtime APT repositories empty | Hidden from normal creation. When present or a dependency-preparation problem requires one, expose Expert declarations with trust/key review; the current static form has JSON-only preservation. |

## Plan evidence policy

The Plan always shows the action path, configured source selector, package identity, output/installation strategy, service state, account and runtime dependency strategy, plus blockers. It must mark each value as `Configured`, `Detected`, `Suggested`, `Resolved`, `Effective`, `Default`, `Unknown`, or `Not applicable` according to [the provenance model](information-architecture.md#provenance-and-resolution). Hide low-frequency controls while keeping their non-default effects summarized. Inapplicable values imported from JSON remain inspectable and never silently discarded.

## Field-family placement index

The base tier below applies to **each listed leaf path** in the [inventory](schema-inventory.md); nested list-row fields inherit the tier of their list unless named separately. The condition column is an additional visibility gate, so an Expert control can also be conditional. The Plan always summarizes a configured non-default effect or blocker even if its editor is in Advanced/Expert.

| Fields | Base tier | Visibility gate / reason |
| --- | --- | --- |
| `schema_version`, `source.provider`, `service.configured`, `management.owner/builtin_id/definition_version`, `install.content.path` | HIDDEN / DERIVED | contract marker, single supported provider, derived unit state, managed metadata, or no demonstrated runtime consumer |
| `name`, `package.name`, `active`, `package.version_revision`, `package.architecture`, `package.description` | CUSTOMIZE | identity is selected during Create; immutable/read-only where current flow fixes it; active and package facts remain in Plan |
| `source.repository`, `source.tracking`, `source.ref`, `artifact.mode` | CUSTOMIZE | `ref` conditional on tag/manual; exact result requires Test |
| `source.version.source`, `source.version.expression` | ADVANCED | expression only for regex |
| `artifact.type`, `artifact.architecture` | HIDDEN / DERIVED | show their resolved meaning in Plan; architecture mismatch is a blocker |
| `artifact.name_pattern`, `artifact.asset_name`, `artifact.archive_source`, `artifact.asset_selection` | CONDITIONAL | only relevant mode/source/selection from mode table |
| `artifact.archive_format`, `artifact.payload.mode/include/exclude` | ADVANCED | only for upstream archive; include conditional on paths mode |
| `artifact.match_package`, `artifact.match_version` | EXPERT | upstream `.deb` only; changing inspection safety deserves explicit review |
| `build.detected_project/files/dependencies/tools` | PLAN | read-only previous hint; fresh Run detection has stronger provenance |
| `build.commands`, `build.output.mode/path/paths` | CUSTOMIZE | source build only; output path(s) conditional on mode |
| `build.extra_dependencies`, `build.source_changes[].operation/path/search/content`, `build.ensure_directories`, `build.inactivity_timeout`, `build.maximum_runtime`, `build.environment`, `build.working_directory` | ADVANCED | source build only; search/content conditional on change operation |
| `package.section/priority/maintainer`, `package.runtime_dependencies` | ADVANCED | Depends entry can be promoted to contextual Customize; maintainer required by packaging |
| `package.runtime_dependency_detection.enabled` | CONDITIONAL | optional only for explicit amd64 Release-asset archive; off by default |
| `package.runtime_dependency_detection.overrides[].soname/action/reason/relation` | EXPERT | only with eligible detection and observed/existing need; relation conditional on manual action |
| `install.content.source`, `install.destination` | CUSTOMIZE | DebBuilder repackaging; destination hidden for configured-files mode |
| `install.owner.user/group/create_user/create_group`, `install.account.user/group/create_user/create_group`, `install.directory_mode/file_mode` | ADVANCED | owner/account controls separated; provisioning shown when non-root account needed |
| `install.directories[].path/owner/group/mode`, `install.config_files[].source/destination/policy/owner/group/mode` | ADVANCED | DebBuilder repackaging; mappings promoted to Customize for configured-files mode |
| `install.maintainer_scripts.preinst/postinst/prerm/postrm` | EXPERT | four distinct hooks, collapsed None/count row |
| `service.enabled/name/command/user/group/restart` | CUSTOMIZE | only when service configured/requested; unit execution identity explicit |
| `service.description/working_directory/environment_files/environment/after/wants/requires` | ADVANCED | only configured service; referenced paths are not generated |
| `service.type/conflicts/restart_sec/timeout_start_sec/timeout_stop_sec/kill_signal/kill_mode/limit_nofile/syslog_identifier/ambient_capabilities/exec_start_pre/exec_start_post/exec_stop/standard_output/standard_error` | EXPERT | only configured service; raw unit behavior requires review |
| `automation.enabled/policy` | ADVANCED | policy choices shown when enabled; effect always summarized in Plan |
| `resource_limits.memory_max_bytes/tasks_max/cpu_quota_percent/io_read_bandwidth_max_bytes_per_sec/io_write_bandwidth_max_bytes_per_sec` | ADVANCED | null means inherited; finite values need host admission evidence |
| `runtime_apt_repositories[].id/uri/suite/components/signing_key.armored` | EXPERT | only when explicitly configured/needed for runtime dependency preparation |
| `management.operator_overrides.active/package.maintainer/build.environment/build.inactivity_timeout/build.maximum_runtime/resource_limits.<field>` | CUSTOMIZE for active/maintainer; ADVANCED for build/limits | managed self-build only; exact backend allowlist, no other authored changes |
