# Recipe editor: proposed information architecture

## What the current references get wrong

The #24B3 prototype is a capability inventory mapped too directly to Recipe v5 keys. Its generic drawer treats a structured account, four maintainer hooks, mappings, ELF overrides and systemd directives as interchangeable text/textarea fields. It marks too many rows as equally important and keeps inapplicable choices visible. The current static page has more purpose-built controls, but still mixes payload ownership, account provisioning and service execution identity; exposes ELF/JSON details too early; and places build, install and service complexity in a long step page. The canonical schema contains useful defaults and distinct lifecycle boundaries that neither UI consistently explains. This audit does **not** authorize changing either UI yet.

## Structures considered

| Structure | Strength | Weakness | Decision |
| --- | --- | --- | --- |
| Four technical steps (current Source/Build/Install/Service) | Aligns with pipeline and current static form | Makes simple prebuilt applications traverse empty Build controls; puts all installation internals up front | Keep as an implementation map, not the primary edit navigation |
| Five Recipe v5 domain groups (#24B3 Advanced) | Exhaustive discoverability | Exposes low-frequency and inapplicable controls equally; follows JSON too closely | Replace for next prototype only after operator approval |
| Single Plan plus progressive Customize/Advanced/Expert | Answers “what will happen?” first, keeps structured controls nearby when relevant | Needs reliable provenance and applicability; mistakes could hide authored values | **Proposed** architecture, with explicit inactive-value inspection and blocker visibility |

## Proposed hierarchy

```text
Recipe: <name>                         [Test] [Build] [More: JSON / Import / Export / Inspect]
Status: Saved / Unsaved · Active / Inactive · user or system-managed

PLAN — resolved/effective facts and unresolved checks
  Source and exact identity (or “not resolved until Test”)
  Project/payload and build action
  Package files → installed destinations and permissions
  Runtime dependencies: manual + detected + effective where available
  Account and service effects; referenced files/directories
  Automation and finite resource policy when configured
  Warnings/blockers and latest Test/Build evidence

CUSTOMIZE — ordinary exceptions
  Source choice and tracking
  Build command(s) and output only for source-build path
  Install destination or files/mappings
  Package summary, architecture, manual Depends when relevant
  Service: configure/enable, command, runtime identity, restart

ADVANCED — less frequent but operational
  Source selection and version expression / archive payload
  Build dependencies, working directory, environment, timeouts,
    source changes and ensured output directories
  File mappings and policies; payload ownership, account provisioning,
    persistent directories and permissions
  Service environment/file references, working directory, basic unit relations
  Automation policy and per-Recipe resource limits

EXPERT — rare or safety-sensitive
  Four maintainer hooks, advanced systemd directives/command hooks
  SONAME dependency override table (only after eligible detection context)
  Runtime APT repository trust declarations
  Raw canonical Recipe JSON, import/export, legacy inactive values
```

`PLAN`, `CUSTOMIZE`, `ADVANCED`, `EXPERT` are presentation levels, not persisted schema fields. A capability may move into the visible Plan when it becomes effective or blocks a Run. **Conditional** governs whether its editor exists in the current mode; **Hidden/Derived** governs whether it should be edited at all. The full mapping is in [schema inventory](schema-inventory.md) and [applicability](applicability-matrix.md).

### Why this hierarchy

A novice can choose a GitHub source, understand the proposed package, adjust a destination or service, and Test without scanning every supported systemd directive. An expert can still inspect and change every authored v5 value through a purpose-built editor or canonical JSON. Contextual links from a Plan row open the relevant control. A detected value never appears as if it were persisted. Warnings, validation errors and manually configured non-default effects stay visible even when their controls live in Expert.

## Complete future page example: documented Zoraxy-style raw asset

The repository has **no persisted Zoraxy Recipe snapshot**. This is a design walkthrough of the [existing documented use case](../information-architecture.md), with unresolved values labeled. It is not a claim that the exact asset, version, service account or ELF linkage has been verified.

```text
Zoraxy                                    Active · user Recipe
PLAN
  Source           GitHub repository and Release asset selector · Configured
  Exact asset      Not yet resolved — Test required
  Payload          Raw Release file proposed; ELF linkage unknown until inspection
  Build            No source compilation for upstream_archive
  Install          One selected file → /usr/local/bin/zoraxy [mapping to confirm]
  Service          zoraxy.service, command and WorkingDirectory /opt/zoraxy
  Runtime Depends  Manual list; the mapped-only raw file is not scanned by
                   current #30 ELF detection. Final Depends unknown until Build
  Warning          WorkingDirectory does not create /opt/zoraxy

CUSTOMIZE
  Release asset selection · install mapping/mode · service command/identity
ADVANCED
  Account provisioning (only if required), file ownership, referenced paths;
  dependency detection entry only if exact eligible mode/architecture
EXPERT
  Lifecycle hooks — None or explicit workaround; ELF overrides only after
  an observed requirement; advanced systemd; raw Recipe
```

A dedicated declarative `install.directories` row for `/opt/zoraxy` would be dishonest: current validation restricts it to package-specific `/etc`, `/var/lib`, `/var/log`. Automatic output destination also excludes `/usr/local/bin`, so that placement requires an explicit mapping. A source-backed file mapping or payload might create a relevant runtime directory; otherwise a reviewed maintainer hook or a separate backend capability is required. The editor must not silently create arbitrary `/opt` directories or claim #30 ELF scanning for a mapping-only raw file.

## Create versus edit

**Create** should guide `package association → GitHub source and artifact mode → Test for exact source/detection → review suggested plan → customize exceptions → Test again/Build`. The existing New Recipe dialog already requires a package, GitHub repository, tracking/ref and version source. Before the first Test, source identity and actual output cannot be asserted. The UI may show a proposed plan and explicitly mark unknowns. Test is an asynchronous Run, not a synchronous form validation or source-preview API.

**Edit** should open the stored Recipe's Plan immediately, alongside its latest *matching* Test/Build evidence and a clear stale-evidence indicator after edits. Customize is ready without replaying onboarding. Existing JSON and import/export remain available. Both flows share the same underlying controls and validator; they differ in entry order and evidence state, not schema. A new Test supersedes previews only for its immutable admitted snapshot; an unrelated older Run cannot certify a newly edited Recipe.

## Provenance and resolution

| Label | Meaning | Persisted where? | Permitted claim |
| --- | --- | --- | --- |
| **Configured** | Authored Recipe selector/value accepted by v5 validation | Recipe document and Run snapshot | “DebBuilder will request/use this value” subject to runtime checks |
| **Detected** | Observation of acquired source, project files/tools, or a built payload | Detection/Run result; some `build.detected_*` fields may store a prior hint | “Observed for this source/Run,” never “current exact result” after source/edit drift |
| **Suggested** | Proposal from detection or UI heuristic requiring operator review | Not persisted until explicitly accepted into a Recipe field | “Could use”; no silent mutation |
| **Resolved** | Exact source identity, selected asset, archive inventory, or other authoritative Test/Build resolution | Immutable Run evidence, not necessarily Recipe | “Resolved by Run X for Recipe hash Y” |
| **Effective** | Final action/metadata after all defaults, global policy, generated steps and detection merge | Run/staging/artifact, sometimes derived view | “Actually attempted/produced by Run X” |
| **Default** | Canonical v5 fallback when an authored field is absent | Applied by normalization, often persisted canonically | “Uses DebBuilder default”; never imply manual choice |
| **Unknown / pending** | Requires acquisition, staging, host capability or Validation | Neither Recipe nor reliable Run evidence yet | State the next proof boundary |

`build.detected_project/files/dependencies/tools` are canonical Recipe fields, but they do not replace a fresh detection result from the acquired source. The Recipe's manual runtime package names are authored; #30's detected relations and merged effective Depends are Build results. `service.configured` is derived from `name` plus `command` and stripped before persistence. Resource limits combine global and Recipe policies at Run admission; the effective limit can be stricter than either displayed Recipe control alone.

### Test and Build proof boundaries

- Before Test: a GitHub selector, tracking rule, output choice and service declaration are configured; exact commit/asset, file inventory, actual project detection, availability of build tools and target lifecycle are unproven.
- Test/dry Run: acquires and pins exact source; detects project; checks dependencies; applies source changes for source builds; prepares/validates a staging preview. It **does not execute build commands or `dpkg-deb --build`**. Missing generated outputs are preview warnings, not produced artifacts. For upstream `.deb`, Test selects but does not download/register the final artifact. For an upstream archive, archive identity/selection can be inspected, but final ELF Depends are not computed.
- Build: executes source commands when applicable, ensures declared output directories, resolves actual output, stages files, optionally runs #30 ELF dependency resolution, generates control/scripts/unit, builds/inspects `.deb` or registers an upstream `.deb`. This is where effective Depends and final file contents become authoritative.
- Validation/Publication: separately prove offline lifecycle and exact-artifact repository publication. Neither is implied by a successful Recipe Test or Build.

The current API has no separate immutable-source preview endpoint. Any proposal to show a definitive source identity *before* Test would require a separately specified backend contract; this architecture does not assume one.
