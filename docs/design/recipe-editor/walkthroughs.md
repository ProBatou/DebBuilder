# Representative Recipe walkthroughs

Evidence levels matter. `tests/fixtures/recipes/seerr.json` and `debbuilder/builtin_recipes/debbuilder.json` are available v5 documents. `tests/test_build_runs.py` contains a Maintainerr-style *test construction*, not a complete persisted Maintainerr Recipe. Zoraxy and Pocket-ID have documented product scenarios in `docs/design/information-architecture.md` and `docs/design/progressive-disclosure.md`, but **no real persisted snapshots in this worktree**. The Svelte prototype fixture is not functional authority. No production data was read.

## Zoraxy — documented raw Release-asset scenario, not a snapshot

| Level | What to show |
| --- | --- |
| PLAN | GitHub Release asset selector; exact asset/version “awaiting Test” until a matching Run. Raw file/no source compilation; selected file → `/usr/local/bin/zoraxy` only after mapping is configured; service name/command and `/opt/zoraxy` WorkingDirectory when authored. Show any missing WorkingDirectory provision as a warning. Runtime Depends strategy and final effective relations remain pending Build. |
| CUSTOMIZE | Release asset exact/pattern selector, install file mapping and mode, service command/enable state; manual runtime package names only if needed. |
| ADVANCED | Payload owner vs provisioned account vs unit user, extra file mappings, service file references. Offer #30 detection only for an explicit `amd64` Release-asset archive **copied as build output**; a mapping-only install does not feed the current ELF analyzer. |
| EXPERT | Four lifecycle hooks as a compact None/count row; SONAME override table only after eligible context/observed need; advanced unit directives; JSON. |
| HIDDEN | Source-build commands, source changes, ensured build-output directories, GitHub-source archive format; routine ELF controls when detection is disabled/ineligible; resource fields when all unset except a compact inherited summary; automation controls until enabled. |

The current validator **rejects `/opt/zoraxy` in `install.directories`** and `/usr/local/bin` as an automatic `install.destination`; the documented `/usr/local/bin/zoraxy` placement must use an explicit source-backed mapping. Such mapping-only content is **not** scanned by the current #30 analyzer because its copied-payload candidate list is empty. A service WorkingDirectory is a reference only. A reviewed source-backed mapping/payload, a maintainer hook, or a separately specified backend extension must ensure `/opt/zoraxy`; the design must not suggest current declarative support. The raw asset's actual ELF status is unknown without inspection, so do not pre-enable #30.

## Pocket-ID — documented prebuilt scenario, not a snapshot

| Level | What to show |
| --- | --- |
| PLAN | Chosen GitHub asset and pending exact identity; prebuilt payload and destination; service command, account and referenced environment file; data directory declaration if valid; explicit warning if referenced file or generated secret is not actually provisioned. |
| CUSTOMIZE | Asset selector, installed executable/mapping, service enable/command/user/group, application-specific values supplied by operator. |
| ADVANCED | `install.account` creation flags; distinct payload owner; source-backed config-file mapping with `create_if_missing`; allowed package-specific persistent directories; EnvironmentFile reference. |
| EXPERT | Hook editor for exceptional install-time setup; advanced systemd, raw Recipe; ELF override only if eligible and supported Build evidence requires it. |
| HIDDEN | Source-build controls for a prebuilt archive; automatic secret generation UI because no v5 capability exists; ELF controls when mode/architecture is ineligible. |

`service.environment_files` **does not create** a file. `create_if_missing` needs a source file in the acquired source. v5 cannot declaratively generate/store a secret. The [separate proposal](../generated-environment-proposal.md) remains future work. Current `install.directories` cannot declare `/opt/pocket-id/data`; only package-specific `/etc`, `/var/lib`, `/var/log` paths are accepted. A normal UI must not present the broader earlier design note as shipped behavior.

## Maintainerr — source-build test profile

`tests/test_build_runs.py::test_maintainerr_style_empty_directories_are_ensured_logged_staged_and_packaged` acquires `Maintainerr/Maintainerr` at `v3.29.0`, runs a constructed Python command that creates `node_modules` and `apps/server/dist/app.js`, ensures `packages/contracts/node_modules` and `apps/server/node_modules`, and selects four output paths. This proves the post-build empty-directory mechanism, not Maintainerr's actual production build command or service settings.

| Level | What to show |
| --- | --- |
| PLAN | Source/ref from the test fixture; source-build path; exact source identity and detected project only once Run evidence exists; commands, output paths and ensured-directory count. Test cannot certify generated `dist/app.js` because it does not execute commands; Build can. |
| CUSTOMIZE | Ordered build commands and selected output paths. |
| ADVANCED | Environment and safe relative working directory if authored; manually added build dependencies; `ensure_directories` path rows, visibly tied to selected output coverage. |
| EXPERT | Source changes, systemd and lifecycle hooks only if a complete Recipe actually uses them; raw JSON. |
| HIDDEN | Archive/Release payload selectors and #30 ELF detection. A service section is a compact “Configure service” entry unless the Recipe has name+command. |

Do not copy the test's Python construction as a recommended Maintainerr command. Its purpose is to prove ordering and coverage rules: commands → ensure directories → resolve selected outputs → package.

## Seerr — checked-in v5 fixture

`tests/fixtures/recipes/seerr.json` declares `example/seerr` latest release, Node.js, `pnpm build`, seven selected output paths, `/opt/seerr`, owner/account creation flags, a `postinst` shell hook creating `/var/lib/seerr` and `/var/lib/seerr/db`, and an enabled `seerr.service` running `/usr/bin/node /opt/seerr/dist/index.js` as `seerr`.

| Level | What to show |
| --- | --- |
| PLAN | Fixture source selector; Node.js as **stored detection hint**, with fresh Run proof separate; one build command and seven outputs; `/opt/seerr` payload, account generation, service command/user/group; one custom `postinst` hook indicator. Source asset/version, actual output existence and Validation remain pending matching Runs. |
| CUSTOMIZE | `pnpm build`, output selection, install destination, service enable/command/identity. |
| ADVANCED | Owner/account creation flags and source-build environment/working directory if needed; output paths editor; optional persistent directories only for validator-accepted paths. |
| EXPERT | `postinst` hook editor with generated-account/service action order; advanced systemd/JSON. |
| HIDDEN | Upstream archive selection and #30 Release-asset ELF detection; other maintainer hooks remain collapsed, but the configured `postinst` count must remain visible. |

The fixture uses a hook for SQLite directories; the checked-in test asserts those exact lines. The UI must not silently convert it to `install.directories` without a reviewed semantic migration, even though `/var/lib/seerr` paths fit current validation.

## DebBuilder self-build — managed v5 definition

`debbuilder/builtin_recipes/debbuilder.json` is application-owned (`definition_version` 8). It builds a package from `ProBatou/DebBuilder`, with `Architecture: all`, four source output paths, `/opt/debbuilder`, root ownership, a source-backed `/etc/debbuilder/debbuilder.env` mapping, explicit preinst/postinst hooks and an enabled root systemd service. The definition is reconciled by `debbuilder/builtin_recipe.py`.

| Level | What to show |
| --- | --- |
| PLAN | Distinct “System-managed self-build” identity, read-only source/build/output/install/service and lifecycle summary; links to its Runs/artifact. Inspect the generated unit and hooks as managed facts. |
| CUSTOMIZE | Only approved operational controls: active and package maintainer. |
| ADVANCED | Only approved build environment, inactivity timeout, maximum runtime and resource limits; display effective global/Recipe limit origin. |
| EXPERT | Read-only canonical JSON/export, managed definition version and allowed override summary. No raw edit of managed areas. |
| HIDDEN | New Recipe/Delete, source/artifact selection, install layout/hooks, service editing, automation policy and dependency detection controls. These are application-managed, not missing capabilities. |

The backend permits exactly the allowlisted overrides named above. Current `static/app.js` exposes active, maintainer and build environment/timeouts; `resource_limits` is JSON-preserved but lacks a dedicated form control. The future UI may present the allowed resource override but must use the same backend restriction. The built-in should be discoverable under System or a clearly separated managed section, not mixed into ordinary editable Recipes.
