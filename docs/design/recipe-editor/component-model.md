# Recipe editor component and list model

This is a component recommendation for a future implementation, **not** a modification of the #24B3 prototype. Persisted values must round-trip through canonical Recipe v5; controls must not invent new fields. Use `recipe_document_for_storage` validation for JSON/import and the existing Recipe update boundary for authored edits. Preserve stable identity, focus, dirty state and contextual error paths.

## Control by capability

| Capability | Presentation / control | Level and condition |
| --- | --- | --- |
| Recipe name, package association, active | Read-only identity where already created; explicit active toggle | Plan / Customize; built-in name immutable |
| GitHub repository, tracking, ref, version source/revision | Repository input, tracking select, conditional ref/version expression, bounded revision field | Customize; no generic Git URL/provider |
| Artifact mode and source | Three plain-language choices (build source, upstream `.deb`, archive/raw asset), then mode-specific candidate selector | Customize; exact/pattern/format/payload conditional |
| Archive payload | Tree of inspected canonical relative paths, include/exclude controls, whole-archive toggle, count | Advanced/conditional; stale inspection marked after selector change |
| Project detection | Read-only detected source, tools, missing dependencies and proposed action with timestamp/Run link | Plan; never an editable “detected” free text control |
| Build commands | Ordered command rows with command input and explicit reorder | Customize for source build; no compilation summary otherwise |
| Build environment | Key/value table with nonempty unique keys, value input and suitable masking warning for sensitive values | Advanced/source build |
| Working directory and timeouts | Safe relative path input and numeric inputs with clear “disabled/unlimited” states; distinguish inactivity from maximum | Advanced/source build |
| Source changes | Ordered operation cards with operation select, relative target path, conditional search/content editor and preview of operation | Advanced/source build |
| Build dependencies | Package/tool chips with add/remove and detected/manual provenance; never merge with runtime Depends UI | Advanced/source build |
| Ensure output directories | Relative path rows linked to selected output; show coverage validation | Advanced/source build |
| Output selection | Source/single/multiple radio or select; file/path rows and explicitly accepted detection suggestions | Customize/source build |
| Package metadata | Description with short/long preview; architecture, revision, section, priority, maintainer as labeled controls | Customize for summary/architecture; Advanced for section/priority/maintainer |
| Manual runtime Depends | Debian package-name chip list, distinct from versioned ELF override relation | Conditional Customize / Advanced |
| Automatic ELF resolution | Eligibility/status card and opt-in toggle; details of observed ELF requirements only when evidence exists | Conditional Advanced; #30 gate applies |
| SONAME overrides | Table: SONAME, ignore/manual, required reason, conditional Debian relation; preview affected requirement | Expert only after eligible context or existing override |
| Install content/destination | Build output vs explicit mappings; validated FHS destination path | Customize when DebBuilder repackages |
| Config mappings | Source→destination/policy table with per-row owner/group/mode disclosure | Advanced; entire installed content in configured-files mode |
| Payload owner | Paired user/group fields with explicit “installed files owned by” label | Advanced; distinct from account creation |
| Account provisioning | User/group editor with separate create-user/create-group toggles and generated action preview; “use existing” choice | Conditional Advanced when non-root identity required |
| Persistent directories | Table of path, owner, group, mode with allowed-root helper | Advanced; only package-scoped `/etc`, `/var/lib`, `/var/log` |
| General file/directory modes | Enumerated permission selects with explanation of installed payload effect | Advanced |
| Service basics | Configure/remove service entry, name, enable-at-install toggle, command, unit user/group, restart policy | Customize when requested |
| Service working directory/environment files | Absolute path row and existing-file reference list with unresolved-path warning | Advanced when service configured |
| Service environment | Key/value table | Advanced when service configured |
| systemd relations and timers | Separate typed lists for After/Wants/Requires/Conflicts, structured duration inputs | Advanced/Expert when service configured |
| Other systemd directives | Selects for type, KillMode, KillSignal, logging; numeric/string fields for LimitNOFILE, SyslogIdentifier; capability list | Expert when service configured |
| systemd command hooks | Three ordered command lists: ExecStartPre/ExecStartPost/ExecStop | Expert when service configured |
| Maintainer scripts | Four individually labeled hook editors with count/summary and generated-script context | Expert when any hook exists or operator opens “Lifecycle scripts” |
| Resource limits | Optional Memory/CPU/Tasks/IO fields with units, inherited/global/effective preview and host admission result | Advanced; finite limit has fail-closed capability check |
| Automation | Enable toggle and exact policy select with plain stage consequences, last observation/Run | Advanced; “manual” default in Plan |
| Runtime APT repositories | Structured id/HTTPS URI/suite/components/public-key editor; fingerprint/trust review from actual validation evidence | Expert; current static UI only preserves through JSON |
| Canonical JSON, import/export | View/edit/validate/preview/apply JSON dialog and explicit collision/managed restrictions; export exact canonical document | Expert and More menu; not an alternative “save” bypass |
| Delete Recipe | Explicit destructive confirmation for an ordinary user Recipe; managed/shipped Recipes stay protected | More menu for editable user Recipes only |

## Multi-value editors

Every editor below needs Add, Edit, Remove, a meaningful empty state, keyboard-accessible row actions, and inline validation at the same index/path returned by backend errors. “Order” refers to runtime meaning, not merely screen order.

| Field/list | Summary and empty state | Row actions / validation | Order |
| --- | --- | --- | --- |
| `build.commands[]` | first command + count / “No compilation” | command input, Add, Edit, Remove, Move; nonempty strings, warn shell operators are unsupported | **Yes**, executed in order |
| `build.environment{}` | key count, redact sensitive values / “Inherited environment” | key/value add/edit/remove; unique nonempty key, string value | No semantic order |
| `build.extra_dependencies[]` | detected/manual counts / “No extra build dependencies” | package/tool row, add/remove; nonempty string, check availability in Run | Set-like, preserve serialization order |
| `build.source_changes[]` | operation+path count / “No source changes” | operation-specific dialog, add/edit/remove/reorder; safe relative path, exact single search match at execution | **Yes**, sequential mutations |
| `build.ensure_directories[]` | paths count / “No post-build directories” | relative path add/edit/remove; <=256, canonical, unique, covered by output | Order not semantically required |
| `build.output.paths[]` | selected path chips/tree / “Select at least one output” | add/edit/remove; safe relative, nonempty | Keep authored order |
| `artifact.payload.include[]/exclude[]` | selected/excluded count / “Choose paths” or “Entire archive” | tree select/deselect, edit selectors; canonical relative path, conflict/overlap handling | Canonicalizer sorts/deduplicates |
| `package.runtime_dependencies[]` | package names / “No manual runtime Depends” | add/edit/remove; simple Debian package names only | Set-like; effective merge later |
| `package.runtime_dependency_detection.overrides[]` | affected requirement count / “No overrides” | SONAME/action/reason/relation table; max 32, unique SONAME, conditional relation | No semantic order; each SONAME unique |
| `install.config_files[]` | source→destination/policy / “No mappings” | add/edit/remove, advanced owner/group/mode; safe paths, valid policy; preview collision and source availability at Test/Build | Retain authored order for review |
| `install.directories[]` | path+owner+mode / “No persistent directories” | add/edit/remove; package-specific allowed roots and modes | Retain authored order |
| `service.environment_files[]` | file paths / “No referenced environment files” | add/edit/remove; nonempty single-line unit value; warn if not declared/generated | Retain authored order |
| `service.environment{}` | key count / “No service variables” | key/value add/edit/remove; no duplicate keys, single-line unit value | No semantic order |
| `service.after[]/wants[]/requires[]/conflicts[]` | separate relation counts / “No unit relations” | add/edit/remove per relation; Conflicts requires `.service`; unit generation rejects newline | Retain authored order |
| `service.ambient_capabilities[]` | capability count / “None” | add/edit/remove; `CAP_[A-Z0-9_]+` | Set-like |
| `service.exec_start_pre[]/exec_start_post[]/exec_stop[]` | command count per hook / “No commands” | add/edit/remove/reorder within each hook; nonempty single-line values | **Yes**, repeated systemd directives |
| `runtime_apt_repositories[]` | repository IDs and suites / “No extra runtime APT repositories” | add/edit/remove; <=32, unique IDs, HTTPS, components, public armored signing key | Preserve declaration order |
| `install.maintainer_scripts` | “None” or hook names/count | open **four distinct** hook editors; add/clear each string, script preview and security warning | Hook names fixed; script content ordered within each hook |

`build.detected_files/dependencies/tools` are lists in the canonical Recipe but should be read-only provenance, not generic add/remove editors. `install.owner` and `install.account` are objects, not lists. The current `static/recipe_serialization.js` uses line splitting and pipe-delimited rows for several lists; those are compatibility details, not a reason to flatten the future component model.

## Interaction contract

- The Plan summarizes actual values without dumping raw JSON. A row opens the relevant editor and retains context; modal/drawer controls have title, focus entry/return, Escape, dirty/Save/Cancel, and a non-color error state.
- Conditional editors mount only for the active mode. When switching modes, preview what will stop taking effect; do not silently clear stored data. If the backend rejects an invalid enabled combination, surface its exact path and remediation.
- For create, suggestions remain opt-in. For edit, preserve authored values and compare them to evidence from a Run with matching Recipe revision/hash.
- A structural editor should serialize one canonical field family, validate locally for fast feedback, and still rely on backend validation. An Expert JSON edit round-trips through the same canonical contract, including managed Recipe restrictions.
- Current JSON semantics to preserve: file import is size-bounded, validated and canonically previewed; replacing a colliding **user** Recipe requires explicit confirmation, while shipped/managed Recipes cannot be replaced. Editing the selected Recipe's JSON cannot change its ID; applying a diff requires confirmation. Export downloads the validated canonical document, and the managed Recipe's JSON is view/export only. Ordinary user Recipe deletion remains separately confirmed.
- Controls for fields currently available only via JSON (resource limits, runtime APT repositories, upstream `.deb` matching flags) are a **future UI design recommendation**, not evidence that they already have a static form.
