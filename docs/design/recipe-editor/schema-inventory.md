# Recipe v5: authored and persisted field inventory

This is an audit of the current contract, not a proposal to change it. Authority: `debbuilder/recipe_schema.py` (`normalize_recipe`, `validate_recipe_metadata`, `recipe_for_storage`, `recipe_document_for_storage`), `debbuilder/archive_payload.py`, `debbuilder/resource_limits.py`, `debbuilder/runtime_apt_repositories.py`, `debbuilder/builtin_recipe.py`, and their tests. The current browser mapping is `static/recipe_serialization.js`, `static/app.js`, `static/index.html`, and `static/js/recipe/*`. Runtime effects were checked against `build_pipeline.py`, `build_executor.py`, `debian_packaging.py`, `elf_toolchain.py`, `systemd_unit.py`, and `dependency_preparation.py`. [Applicability](applicability-matrix.md), [components](component-model.md), and [provenance](information-architecture.md) expand the compact cells below.

**Reading the tables.** Paths begin at `$`. `O` means optional in authored JSON, even when canonical normalization persists a default. `R` means required for an accepted document or under the stated condition. `D` means derived by normalization and not an authored field. `UI` identifies the existing static interface: `form`, `JSON` (JSON editor/import only), `display` (read-only), or `none`. `Effect` is the runtime/build effect or plan value. For every row, an invalid type or unknown field is rejected by canonical validation; `recipe_document_for_storage` rejects unknown nested fields instead of silently dropping them. All defaults below refer to canonical normalization; storage compacts empty `build.ensure_directories`, non-applicable `artifact.payload`, the non-path output `path`, and derived `service.configured`. Imported JSON must declare v5 and a nonempty name. Several semantically irrelevant fields remain canonical even when a mode ignores them; the editor must preserve them while changing modes and clearly label them inactive.

## Identity, automation, limits, and external runtime repositories

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.schema_version` | int; `5` | R; exactly 5, bool rejected | contract marker | JSON | parser/storage version |
| `$.name` | string; `recipe` on normalization | R for import; safe `[A-Za-z0-9_.+-]+` | identity | hidden form/JSON | Recipe ID and default package name |
| `$.active` | bool; `true` | O; bool | all Recipes | form | disables Test/Build and automation eligibility when false |
| `$.automation.enabled` | bool; `false` | O; bool | user Recipe; managed policy fixed | form | periodic automation eligibility |
| `$.automation.policy` | enum; `manual` | O; `manual/detect/test/build/build_validate/full` | meaningful when enabled; `manual` remains inert | form | maximum automatic lifecycle stage |
| `$.resource_limits.memory_max_bytes` | int/null; null | O; positive integer, bounded | all Runs; host enforcement required if finite | JSON | per-command memory ceiling; effective minimum with global policy |
| `$.resource_limits.tasks_max` | int/null; null | O; positive integer, bounded | same | JSON | task ceiling |
| `$.resource_limits.cpu_quota_percent` | int/null; null | O; positive integer within exact systemd mapping | same | JSON | CPU quota |
| `$.resource_limits.io_read_bandwidth_max_bytes_per_sec` | int/null; null | O; positive integer, bounded | same | JSON | read bandwidth ceiling |
| `$.resource_limits.io_write_bandwidth_max_bytes_per_sec` | int/null; null | O; positive integer, bounded | same | JSON | write bandwidth ceiling |
| `$.runtime_apt_repositories[]` | list; `[]` | O; max 32, unique IDs | only when target runtime dependencies need an additional admitted APT source | JSON | extra signed repositories during dependency preparation, not a package source |
| `$.runtime_apt_repositories[].id` | string | R per row; lowercase safe ID, max 64 | depends on row | JSON | stable declaration identity |
| `$.runtime_apt_repositories[].uri` | string | R; HTTPS URI, no credentials/query/fragment | depends on row | JSON | target APT source URI |
| `$.runtime_apt_repositories[].suite` | string | R; bounded safe suite | depends on row | JSON | target suite |
| `$.runtime_apt_repositories[].components[]` | string list | R; bounded safe components | depends on row | JSON | target components |
| `$.runtime_apt_repositories[].signing_key.armored` | string | R; bounded public armored key, no private key | depends on row | JSON | repository trust anchor in preparation |

## Package and runtime dependencies

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.package.name` | string; lowercase Recipe name | O; Debian package-name pattern | all modes | hidden form/JSON | `Package` metadata and installation path constraints |
| `$.package.version_revision` | string; `1` | O; `[A-Za-z0-9.+~]+` | all modes; distinct from upstream version | form | Debian revision |
| `$.package.architecture` | enum; `amd64` | O; `all/amd64/arm64/armhf` | all modes | form | package architecture; gates ELF opt-in |
| `$.package.section` | string; `misc` | O; Debian section pattern | all modes | form | control metadata |
| `$.package.priority` | enum; `optional` | O; required/important/standard/optional/extra | all modes | form | control metadata |
| `$.package.maintainer` | string; empty | O in schema; single line; packaging needs nonempty | all modes; managed override allowed | form | control metadata / Build readiness |
| `$.package.description` | string; package name | O; packaging needs nonempty | all modes | form | short and long Debian description |
| `$.package.runtime_dependencies[]` | package-name list; `[]` | O; simple names only | all modes; not versioned manual relations | form single input | manual Depends; may gain generated `adduser` at packaging |
| `$.package.runtime_dependency_detection.enabled` | bool; `false` | O; true only for `upstream_archive` + `release_asset` + package `amd64` | conditional #30D opt-in | form toggle always rendered | invokes staged ELF inspection and Bookworm/amd64 resolver on Build |
| `$.package.runtime_dependency_detection.overrides[]` | list; `[]` | O; max 32, unique SONAME | useful only with enabled detection and observed dynamic ELF requirement | form raw JSON | explicit resolver decisions; no automatic Recipe mutation |
| `...overrides[].soname` | string | R per row; bounded SONAME syntax | depends on override | raw JSON | identifies one `DT_NEEDED` requirement |
| `...overrides[].action` | enum | R; `ignore/manual` | depends on override | raw JSON | omit or manually resolve requirement |
| `...overrides[].reason` | string | R; nonempty, max 256, no controls | depends on override | raw JSON | audited human justification |
| `...overrides[].relation` | string; empty for ignore | R for manual, simple Debian relation; must be empty for ignore | depends on action | raw JSON | versioned manual relation prepared in Bookworm |

## GitHub source and artifact selection

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.source.provider` | enum; `github` | O; only `github` | all modes; no generic provider today | constant/JSON | acquisition provider |
| `$.source.repository` | string; empty | R for usable source; `owner/name` when present | all modes | form | GitHub repository |
| `$.source.tracking` | enum; `latest_release` | O; `latest_release/tag/manual` | all modes | form | resolution rule |
| `$.source.ref` | string; empty | R for `tag/manual`; <=200, no whitespace | conditional tracking | form conditional | requested tag/ref |
| `$.source.version.source` | enum; `tag` | O; `tag/release_name/regex` | all modes | form | upstream-to-Debian version extraction rule |
| `$.source.version.expression` | string; empty | R for regex; <=200, compilable regex | conditional version source | form conditional | version extraction expression |
| `$.artifact.mode` | enum; `source_build` | O; `source_build/upstream_deb/upstream_archive` | primary path choice | form | source build, direct `.deb`, or repackaged archive/raw asset |
| `$.artifact.type` | enum; `deb` or `archive` for archive mode | O; `deb` for first two modes; archive type set for archive mode | conditional mode | derived form/JSON | upstream artifact interpretation |
| `$.artifact.architecture` | enum; package architecture | O; supported architecture set | all modes, especially upstream selection | derived form/JSON | upstream artifact architecture |
| `$.artifact.name_pattern` | string; empty | O; <=200, single line; required for release-asset pattern, used for upstream `.deb` selection | conditional mode/selection | form | asset pattern |
| `$.artifact.match_package` | bool; `true` | O; bool | upstream `.deb` inspection | JSON | require upstream package name match |
| `$.artifact.match_version` | bool; `true` | O; bool | upstream `.deb` inspection | JSON | require upstream package version match |
| `$.artifact.asset_name` | string; empty | O; <=200, no path separators; required for exact Release asset | conditional selection | form | exact upstream asset |
| `$.artifact.archive_source` | enum; `auto` | O; `auto/github_source/release_asset` | upstream archive only | form conditional | archive candidate source |
| `$.artifact.asset_selection` | enum; `pattern`, or `exact` if asset name supplied | O; `pattern/exact` | Release asset only | form conditional | selection strategy |
| `$.artifact.archive_format` | enum; `tar.gz` | O; `tar.gz/zip` | GitHub source archive | form conditional | acquisition format |
| `$.artifact.payload.mode` | enum; `paths` | O; `paths/entire_archive` | upstream archive only; removed from other persisted modes | form archive tree | selected paths vs whole extracted payload |
| `$.artifact.payload.include[]` | path-selector list; `[]` | R nonempty for archive `paths`; canonical relative file/dir selectors | upstream archive `paths` | form archive tree | copied payload selection |
| `$.artifact.payload.exclude[]` | path-selector list; `[]` | O; canonical relative selectors | upstream archive | form archive tree | payload exclusions |

`upstream_deb` bypasses DebBuilder's build, install, service and generated package path: the upstream `.deb` is registered after inspection. Those authored fields can still exist in a canonical document but do not become that Run's actions. For `upstream_archive`, source-change and build-command stages are skipped. Exact asset identity, candidate type and archive tree are resolved during source acquisition/inspection, not by the persisted selector alone.

## Build and output

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.build.detected_project` | enum/null; null | O; `nodejs/python/rust/static` | stored observation, not proof of a fresh source scan | display dataset/JSON | prior detection hint; Run detection is authoritative |
| `$.build.detected_files[]` | strings; `[]` | O; nonempty strings | stored observation | display/JSON | detection provenance, not command input |
| `$.build.detected_dependencies[]` | strings; `[]` | O; nonempty strings | stored observation | display/JSON | prior detected system dependencies |
| `$.build.detected_tools[]` | strings; `[]` | O; nonempty strings | stored observation | display/JSON | prior detected tools |
| `$.build.extra_dependencies[]` | strings; `[]` | O; nonempty strings | source build dependency check | form chips | manually requested build dependencies, not Debian runtime Depends |
| `$.build.source_changes[]` | ordered objects; `[]` | O; supported operation and safe relative path | source build only; upstream archive skips stage | form list/dialog | applied to acquired source before commands |
| `...source_changes[].operation` | enum | R per row; replace/insert_before/insert_after/remove/create_file/remove_file | operation governs other fields | form dialog | mutation operation |
| `...source_changes[].path` | string | R; safe relative path, no traversal/symlink at execution | depends on row | form dialog | target source file |
| `...source_changes[].search` | string | R nonempty for text edits; not used by create/remove file | conditional operation | form dialog | exact single-match anchor |
| `...source_changes[].content` | string | used for replace/insert/create; absent/empty removes text for remove | conditional operation | form dialog | new text/file content |
| `$.build.commands[]` | ordered nonempty strings; `[]` | O; command runner accepts structured command parsing, no shell operators | source build only | form multiline text | contained project commands in order; Test does not run them |
| `$.build.ensure_directories[]` | relative path list; `[]` | O; <=256, canonical POSIX paths <=1024, unique; covered by selected output unless output mode is whole source | source build only | form multiline text | creates missing source-tree dirs after commands, before output resolution |
| `$.build.inactivity_timeout` | int/null; `300` | O; 1–86400 or null | source build commands | form number | per-command stdout/stderr inactivity bound; null disables |
| `$.build.maximum_runtime` | int/null; null | O; 1–604800 or null | source build commands | form number | absolute command runtime bound |
| `$.build.environment{}` | string map; `{}` | O; nonempty string keys, string values | source build commands | form multiline text | build process environment |
| `$.build.working_directory` | relative path; `.` | O; safe relative path or `.` | source build/detection | form text | source-tree working directory |
| `$.build.output.mode` | enum; `source` (or `path` with legacy `build.output`) | O; `source/path/paths` | source build; upstream archive has separately resolved payload | form select | output selection strategy |
| `$.build.output.path` | relative path; empty or `dist` for path mode | R safe nonempty in path mode; removed from other persisted modes | conditional `path` | form input | one selected output |
| `$.build.output.paths[]` | relative path list; absent by default | R nonempty safe paths in paths mode | conditional `paths` | form row list | selected outputs in order |

## Debian installation, identities and lifecycle hooks

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.install.content.source` | enum; `build_output` | O; `build_output/configured_files` | repackaging, not upstream `.deb` | form select | copy selected output or only explicit mappings |
| `$.install.content.path` | string; empty | O; normalized/persisted but no consumer found in current packaging path | hidden/JSON | no demonstrated runtime effect; preserve, do not advertise |
| `$.install.destination` | string; `/opt/<package>` or empty for mappings-only | R supported `/opt/<path>`, `/usr/bin`, `/usr/sbin`, `/usr/lib/<package>` or `/usr/share/<package>` path when copying output; must be empty for configured_files | conditional content source | form conditional | selected output installation root; `/usr/local/bin` is not accepted here |
| `$.install.owner.user` | string; package name | O; safe account name | when build output copied | form | chown payload after install; does not itself create user |
| `$.install.owner.group` | string; package name | O; safe group name | when build output copied | form | chown payload group |
| `$.install.owner.create_user` | bool; false (forced false for root) | O; bool | legacy/convenience fallback for account defaults; provisioning uses `install.account` | JSON via account selector | not a separate guaranteed account creation action |
| `$.install.owner.create_group` | bool; false (forced false for root) | O; bool | same | JSON via account selector | not a separate guaranteed group creation action |
| `$.install.account.user` | string; owner user | O; safe account name | if account must exist on target | form | identity for generated account provisioning |
| `$.install.account.group` | string; owner group | O; safe group name | if account must exist on target | form | group for provisioning |
| `$.install.account.create_user` | bool; owner flag or false | O; bool; root never generated by packaging | conditional operator choice | form combined selector | generated `postinst` adduser action; adds `adduser` Depends |
| `$.install.account.create_group` | bool; owner flag or false | O; bool; root never generated by packaging | conditional operator choice | form combined selector | generated `postinst` addgroup action |
| `$.install.directory_mode` | enum; `0755` | O; `0755/0750/0700` | copied output | form select | package payload directory modes |
| `$.install.file_mode` | enum; `0644` | O; `0644/0640/0600` | copied output; mapping may override | form select | package payload file modes |
| `$.install.directories[]` | object list; `[]` | O; package-specific `/etc`, `/var/lib`, `/var/log` paths only | persistent target dirs | form pipe-delimited text | staged directory plus `postinst install -d` |
| `...directories[].path` | string | R; under `/etc|/var/lib|/var/log/<package>`; no `..` | per row | text row | target directory |
| `...directories[].owner` | string; `root` | O; safe name | per row | text row | target owner |
| `...directories[].group` | string; `root` | O; safe name | per row | text row | target group |
| `...directories[].mode` | enum; `0755` | O; `0755/0750/0700` | per row | text row | target permissions |
| `$.install.config_files[]` | mapping list; `[]` | O; each needs source/destination | optional extra mappings; complete payload when configured_files | form table | stage source-backed target files |
| `...config_files[].source` | string | R; safe relative source path | per row | form table | acquired source file |
| `...config_files[].destination` | string | R; safe absolute path, no `..` | per row | form table | target path |
| `...config_files[].policy` | enum; `dpkg_conffile` | O; `dpkg_conffile/replace/create_if_missing` | per row | form table | Debian conffile, overwrite, or guarded install-time creation |
| `...config_files[].owner` | string; inherited payload owner | O; safe name | per row | form table | per-file ownership override |
| `...config_files[].group` | string; inherited payload group | O; safe name | per row | form table | per-file group override |
| `...config_files[].mode` | octal string; inherited file_mode | O; `0[0-7]{3}` | per row | form table | per-file mode override |
| `$.install.maintainer_scripts.preinst` | string; empty | O | Expert only | form textarea | custom pre-install shell hook, merged with generated actions |
| `$.install.maintainer_scripts.postinst` | string; empty | O | Expert only | form textarea | custom post-install hook, after generated account actions, before service reload |
| `$.install.maintainer_scripts.prerm` | string; empty | O | Expert only | form textarea | custom pre-remove hook, merged with generated service stop |
| `$.install.maintainer_scripts.postrm` | string; empty | O | Expert only | form textarea | custom post-remove hook, merged with generated reload |

`install.owner` (payload ownership), `install.account` (provisioning) and `service.user/group` (unit execution identity) are distinct. Schema defaults can make them equal, but no cross-field equality is enforced. `service.working_directory` neither creates a directory nor an environment file. An earlier [design note](../information-architecture.md) proposed declarative `/opt/zoraxy` via `install.directories`; #24B4 corrected that note because the current validator rejects the path. Do not present it as supported without a separate backend contract.

The #30 ELF analyzer receives the `copied` payload list from `prepare_staging`. When `install.content.source=configured_files`, automatic output copying is disabled and this list is empty; a raw binary installed solely by `install.config_files` is therefore not a candidate for current automatic dependency detection, even if the schema's three-field opt-in gate passes. This is a meaningful applicability condition beyond metadata validation.

## systemd service

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.service.configured` | bool; false | D; **never authored/persisted** | true iff name and command nonempty | display | unit generated even when disabled; removed on storage |
| `$.service.enabled` | bool; false | O; true requires configured | service entry | form toggle | enable/restart at install; disabled configured service still has unit |
| `$.service.name` | string; empty | O; `.service` suffix if set | required to configure | form | unit name |
| `$.service.description` | string; package name when requested, else empty | O | configured/requested service | form advanced | unit description |
| `$.service.type` | enum; `simple` when requested, else empty | O; supported simple/exec/forking/oneshot/notify/dbus when configured | configured service | form select | `Type=` |
| `$.service.user` | string; empty | O; safe name if configured | configured service | form | `User=`; blank means systemd default, not account auto-selection |
| `$.service.group` | string; empty | O; safe name if configured | configured service | form | `Group=` |
| `$.service.restart` | enum; `on-failure` when requested, else empty | O; supported restart policies | configured service | form select | `Restart=` |
| `$.service.command` | string; empty | O; required to configure | form | form textarea | `ExecStart=` |
| `$.service.environment_files[]` | string list; `[]` | O; nonempty strings; unit generation enforces single line | configured service | form input | references existing files; does not generate them |
| `$.service.environment{}` | string map; `{}` | O; nonempty keys/string values; unit generation enforces single line | configured service | form textarea | unit `Environment=` |
| `$.service.after[]` | string list; `[]` | O; nonempty strings, unit generation single line | configured service | form input | `After=` order relation |
| `$.service.wants[]` | string list; `[]` | O; same | configured service | form input | `Wants=` weak relation |
| `$.service.requires[]` | string list; `[]` | O; same | configured service | form input | `Requires=` strong relation |
| `$.service.conflicts[]` | `.service` list; `[]` | O; unit-name pattern | configured service | form input | `Conflicts=` |
| `$.service.restart_sec` | string; empty | O; unit generator single line | configured service | form input | `RestartSec=` |
| `$.service.timeout_start_sec` | string; empty | O; same | configured service | form input | `TimeoutStartSec=` |
| `$.service.timeout_stop_sec` | string; empty | O; same | configured service | form input | `TimeoutStopSec=` |
| `$.service.kill_signal` | string; empty | O; same | configured service | form select | `KillSignal=` |
| `$.service.exec_start_pre[]` | string list; `[]` | O; nonempty strings/single line | configured service | form textarea | ordered `ExecStartPre=` commands |
| `$.service.exec_start_post[]` | string list; `[]` | O; same | configured service | form textarea | ordered `ExecStartPost=` commands |
| `$.service.exec_stop[]` | string list; `[]` | O; same | configured service | form textarea | ordered `ExecStop=` commands |
| `$.service.standard_output` | string; empty | O; unit generator single line | configured service | form input | `StandardOutput=` |
| `$.service.standard_error` | string; empty | O; same | configured service | form input | `StandardError=` |
| `$.service.working_directory` | safe absolute path; empty | O; checked if configured | configured service | form input | `WorkingDirectory=`; path must already exist |
| `$.service.limit_nofile` | digits/`infinity`; empty | O | configured service | form input | `LimitNOFILE=` |
| `$.service.kill_mode` | enum; empty | O; control-group/mixed/process/none | configured service | form select | `KillMode=` |
| `$.service.syslog_identifier` | safe string; empty | O | configured service | form input | `SyslogIdentifier=` |
| `$.service.ambient_capabilities[]` | `CAP_*` list; `[]` | O; capability-name pattern | configured service | form input | `AmbientCapabilities=` |

Some service strings are not fully validated by Recipe metadata; `systemd_unit.generate_unit` rejects control/newline values at packaging. A future editor should not promise that metadata validation alone proves the generated unit is valid. Raw command/directive strings remain Expert controls, never generic textareas for an entire service object.

## Built-in management metadata

Only `debbuilder` may carry this object. It is maintained by application reconciliation, not a normal user authoring area. The [managed Recipe](walkthroughs.md#debbuilder-self-build) is inspectable but only the allowlisted operator overrides are editable.

| JSON path | Type; default | Required / validation | Derived / applicability | Current UI | Runtime effect / plan |
| --- | --- | --- | --- | --- | --- |
| `$.management.owner` | literal `application` | R for managed | managed only | JSON view | ownership marker |
| `$.management.builtin_id` | literal `debbuilder` | R for managed | managed only | JSON view | reserved built-in identity |
| `$.management.definition_version` | positive int | R for managed | managed only | JSON view | reconciliation version |
| `$.management.operator_overrides.active` | bool | O; bool | managed only | form | activation override |
| `$.management.operator_overrides.package.maintainer` | single-line nonempty string | O | managed only | form | maintainer override |
| `$.management.operator_overrides.build.environment{}` | string map | O | managed only | form | build environment override |
| `$.management.operator_overrides.build.inactivity_timeout` | int/null | O; 1–86400 or null | managed only | form | inactivity override |
| `$.management.operator_overrides.build.maximum_runtime` | int/null | O; 1–604800 or null | managed only | form | runtime override |
| `$.management.operator_overrides.resource_limits.memory_max_bytes` | int/null | O; positive bounded integer or null | managed only | JSON | memory override |
| `$.management.operator_overrides.resource_limits.tasks_max` | int/null | O; positive bounded integer or null | managed only | JSON | task override |
| `$.management.operator_overrides.resource_limits.cpu_quota_percent` | int/null | O; positive exactly mappable integer or null | managed only | JSON | CPU override |
| `$.management.operator_overrides.resource_limits.io_read_bandwidth_max_bytes_per_sec` | int/null | O; positive bounded integer or null | managed only | JSON | read I/O override |
| `$.management.operator_overrides.resource_limits.io_write_bandwidth_max_bytes_per_sec` | int/null | O; positive bounded integer or null | managed only | JSON | write I/O override |

The built-in allows exactly `active`, `package.maintainer`, `build.environment`, `build.inactivity_timeout`, `build.maximum_runtime`, and `resource_limits` as effective editable paths. The metadata object records those overrides but is itself application-owned. `static/app.js` currently exposes the first five as controls; resource limits are preserved in serialization but have no dedicated form control.
