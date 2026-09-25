# #24B decision — adopt Svelte for the admin redesign

The operator accepted #24A's **Overview / Packages / Recipes / Runs / System / Settings** structure, system-managed DebBuilder self-build, and visual direction **A** with selective **B** guidance. This checkpoint builds [design system v0](system/principles.md), [desktop/mobile references](references/README.md), and an [isolated Svelte spike](../../prototypes/issue-24/README.md). It does not migrate the production admin UI.

## Prototype evidence

The spike renders all six destinations with fixture-only states. The representative slice is the shell/navigation, `RecipePlan.svelte` (derived blocker, provenance and next action), `RunPoller.svelte`/`fixturePolling.js` (start/stop, abort, no overlapping poll) and Run lifecycle/status/log/validation/publication panels. Shared `StatusChip`, `Provenance` and `Modal` avoid multiple hand-built DOM renderers. The six reference screens deliberately use one visual system. The Run detail shows #30 detected/manual/bundled/override/unresolved/effective dependency categories after Build, with raw ELF detail deferred. The Recipe leaves final runtime dependencies unknown until Build.

| Concern | Observed Svelte result | Structured vanilla alternative |
| --- | --- | --- |
| Recipe state | `blocker` and `canBuild` are derived from scenario/confirmation; `RecipePlan` receives explicit props; status/provenance render from data. No imperative field hiding in the plan slice. | Would require controller, DOM updates and explicit invalidation around the existing Recipe v5 form. Pure adapters can make this viable but are more manual. |
| Run lifecycle/polling | Poller exists only while Runs is mounted, cleans timer/AbortController, suppresses overlap/stale result; stage/log state derives from one scenario. | Existing `logs.js` already implements careful polling; a scoped controller could preserve it. Svelte reduces mount/unmount bookkeeping in the view. |
| Component reuse | Shared status/provenance/modal components appear across screens; Recipe plan and dependency summary are isolated. | Existing `ui_core.js` has partial primitives but repeated HTML strings and imperative event wiring remain. |
| Accessibility | Native labeled controls, `aria-current`, mobile menu state, text+icon statuses, visible focus and native dialog Escape/focus return were browser-checked. | Equally achievable in vanilla; neither stack provides accessibility automatically. Production still needs table/list keyboard and touch-target review. |
| Responsive | One token/CSS system covers six views. Mobile table scrolling was fixed after the first browser pass; 390px pages have no document overflow. | Same CSS effort applies. Components help keep markup consistent. |
| Size/build | Built entry 78,853 B JS (27,995 B gzip), 23,245 B CSS (5,684 B gzip), 421 B HTML. Three runtime assets plus build manifest. Build around 1.3 s Vite work / 4.3 s total npm invocation on this host. | Existing unbundled HTML/JS/CSS is about 382,840 B raw, 83,040 B when concatenated and gzipped. This is not a like-for-like feature comparison: the spike omits much production behavior. Vanilla needs no build step. |
| Packaging | Node v26.9.0/npm 9.7.2; `node_modules` 117 MiB for build/test only. `npm ci` and build would precede Debian packaging. Current built-in Recipe includes `static` in output paths and Python serves HTML/JS/CSS from that tree. | Current package includes source `static` directly. No asset manifest or Node toolchain in the build pipeline. |
| Testability/maintenance | `svelte-check` reports zero errors/warnings; Playwright covers screens, state, focus, polling and mobile overflow. State/markup relationships are explicit. The current `App.svelte` is still too large for a production app and must be split by feature during migration. | Existing JS/Playwright tests are valuable and should remain parity gates. A structured vanilla rewrite would avoid dependencies but still need extensive state decomposition. |
| Migration cost | High: canonical Recipe v5 adapter, all lifecycle/automation/validation/publication behavior, auth/session and deployment entrypoint must be ported deliberately. | Lower toolchain cost but a substantial rewrite of global state, DOM and style hierarchy is still required. |

## Decision

**ADOPT SVELTE** for the future admin frontend migration, subject to the #24B operator review gate before implementation. The spike demonstrates a real advantage in derived Recipe state, lifecycle-scoped polling and consistent shared presentation, while the measured build output and package boundary are manageable. Vue needs no separate prototype. Structured vanilla remains a documented fallback if subsequent parity work uncovers a deployment/toolchain constraint that the spike did not cover.

This decision does not authorize migrating `static/` in #24B. Build artifacts remain ignored in the spike; the reference PNGs are review artifacts. Before any #24C migration, specify the frontend build gate, pinned toolchain, static entry/manifest handling, cache strategy, Debian packaging test, and feature-by-feature parity. Keep the public APT landing separate and read-only.

## Packaging boundary and cache

Vite uses a relative base and content-hashed JS/CSS filenames. A future packaging step can build into a temporary output directory, verify `index.html` and manifest references, then place the final files under `static/` **before** the self-build Recipe stages `static`. Serve the HTML entry with revalidation and hashed assets with immutable caching only after reviewing the existing HTTP static handler: it currently sends `no-cache, must-revalidate` for all static files. The handler’s MIME map covers HTML/JS/CSS; any fonts/images require an explicit MIME extension or a deliberate restriction to those three asset types. No Node executable or `node_modules` enters the `.deb` runtime payload.

## Source-preview boundary

The reference’s exact identity is labeled as coming from a **previous Test fixture**. For a first-time source, the current Test remains the authoritative resolution boundary. The prototype did not establish a need for a separate preview API: default UI can say “identity not yet resolved,” run Test, then show the exact identity and plan. If product review insists on an immutable identity before initiating Test, that is a separate API/Issue decision; do not simulate it as shipped.

## #24B2 operator review refinement

The operator accepted the Svelte choice and Calm operations desk direction, then requested corrections before API integration. [#24B2 references](references/README.md) now show a viewport-anchored collapsible sidebar, six concise top-level headers, a compact Packages repository summary and read-only inventory subview, selectable Package/Recipe/Run lists, contextual actions, compact Overview and Run dependencies, and System/Settings organization. [Theme](system/theme.md), [i18n](system/i18n.md), [responsive rules](system/responsive.md) and the [capability map](system/navigation.md) record the new behavior. The prototype remains isolated and fixture-only. #24C is pending a separate operator review.

## #24B3 operator review refinement

The final fixture review adds a geometric cube mark and semantic SVG navigation icons, locally editable Settings and Recipe Advanced fixtures, a compact translated Run pipeline with original log verbosity choices, in-Run failure diagnosis, and a compact public APT landing with three closed disclosures. The admin inventory remains a reprepro-only secondary view. [References](references/README.md) document the captured states. No API or production cutover is included; #24C still requires separate operator approval.
