# #24A — Frontend architecture decision

**#24B update:** the isolated spike supports [adopting Svelte](24b-decision.md) for the later admin migration; this document remains the #24A comparison.

## Current architecture and pressure points

The frontend is one HTML document with ordered classic scripts, global `let` state, page functions and string-template renderers. It has no bundler or client router. That is deployable as static files by the Python server and Debian package. Existing `ui_core.js`, `components.css`, `pages.css`, Recipe modules and page modules show useful factoring, and Playwright plus Node JS tests already cover behavior.

The pressure is specific: Recipe v5 has many dependent fields, managed editable-path restrictions, autosave revisions and JSON round-tripping; Runs combine polling/log offsets, cancellation, validation and publication; Packages duplicate some lifecycle actions. Shared DOM, state and render conventions are implicit. A redesign will add resolved-value provenance, progressive disclosure, compact/mobile composition and reusable dialogs/status views. Those requirements increase the cost of global script order and manual DOM synchronization.

| Criterion | A. Structured vanilla HTML/CSS/JS | B. Svelte | C. Vue |
| --- | --- | --- | --- |
| Components/duplication | Explicit modules and small render functions can unify cards, field groups, statuses and dialogs; manual DOM events remain. | Compiled components/templates make reuse and scoped state concise. | Components/templates/composables are clear but bring a larger runtime/API surface. |
| Recipe state/serialization | Keep canonical Recipe v5 model independent of DOM; add reducer/store or controller and pure transforms. More discipline required. | Reactive derived state suits applicability and resolved summaries; keep serializer as pure adapter. | Computed state and composables suit forms; likely more boilerplate for this size. |
| Runs/polling | Existing timeouts can be extracted into lifecycle-scoped controllers with abort/revision guards. | Component lifecycle naturally scopes polling; service still needs canonical state machine. | Similar through composables; service still needed. |
| Dialogs/accessibility | Native `<dialog>` plus one focus/close helper; explicit review required. | Component abstraction helps consistent dialogs; accessibility remains developer responsibility. | Same; libraries are optional and should not define product UX. |
| Testing/responsive | Existing Node/Playwright suite reusable; pure modules enable focused tests. CSS tooling unchanged. | Component tests possible; browser parity suite still required. CSS can remain token based. | Similar; more test/tool choices. |
| Build/deployment | No build, no new runtime or packaging step. Static cache versions/manual ordering continue unless modules adopted. | Node build at development/package build time; emit hashed JS/CSS and manifest into Python-served static path. No Node at runtime. | Same, with larger runtime bundle. |
| Migration cost | Lower code change, but restructuring global state while redesigning screens is substantial. | Moderate/high: rewrite views once, preserve API/Recipe adapters; avoid two full UIs. | Moderate/high plus a less compelling size/complexity fit here. |
| Small-project maintenance | Minimal dependencies; requires strong conventions to stop regression into string/DOM duplication. | Small compiled output and simple component model; adds toolchain/version maintenance. | Mature ecosystem but greater abstraction/runtime cost than current needs justify. |

## Recommendation for operator review

**Prefer Svelte for #24B if the visual/progressive-disclosure direction requires rebuilding most complex views**, as the Recipe and Run state is already beyond a simple document. This is a conditional recommendation, not permission to migrate now. Prove it with one Recipe section and Run detail prototype against the existing API and parity checks before committing to full migration. Vue is viable but offers no clear project-specific benefit over Svelte. Structured vanilla remains credible if the operator prioritizes a build-free Debian package and a prototype demonstrates clean state isolation.

If vanilla is selected, use ES modules with explicit imports, one canonical Recipe model and pure `toForm/fromForm` adapters, page-owned controllers with `mount/unmount`, shared status/action/dialog renderers, and one API client that preserves structured errors. Remove classic script-order globals. The DOM may still be server-served static HTML; avoid adding a framework-sized homemade abstraction.

If Svelte is selected, keep Python routes and API unchanged. One admin entry mounts into the existing static shell; the public repository template remains independent. Build output is versioned static assets packaged into the Debian artifact during the build (Node only in the build environment). Pin lockfile/toolchain and verify packaged asset paths and cache behavior. Migrate by bounded vertical slices using API adapters and retain old pages only until their replacement passes parity. Do not maintain two complete frontends or duplicate Recipe serialization long term. Vue would use the same migration boundary and packaging discipline.

Before the decision, inspect actual generated bundle size, offline packaging and a maintained component’s complexity; do not use framework popularity as evidence. The decision must preserve Recipe v5, auth/session, Run queue/cancellation/recovery, automation, resource limits, validation, publication proof/locking, diagnostics, inspectors and error contracts.
