# #24B5 fixture implementation of the approved Recipe editor architecture

The isolated Svelte prototype at `prototypes/issue-24` now presents Recipe fixtures through **Plan → Customize → Advanced → Expert**. This is an operator review reference, not an API client or a canonical v5 validator. The [schema audit](schema-inventory.md), [applicability matrix](applicability-matrix.md), [information architecture](information-architecture.md), and [component model](component-model.md) remain the implementation contract for a later production migration.

## Operator decisions reflected

- `/opt/<package>` runtime-directory creation remains unsupported by `install.directories`. The Zoraxy Plan warns that `WorkingDirectory=/opt/zoraxy` does not create that path. The directory editor accepts only package-specific paths below `/etc`, `/var/lib`, and `/var/log`; the fixture Save check rejects other paths.
- #30 ELF detection is hidden for ineligible Recipes. An eligible Release asset/amd64/copied-output fixture receives a compact **Advanced → Dependencies** opt-in only after matching Test/Build evidence. It defaults off. Mapping-only Zoraxy and Pocket-ID fixtures never offer this control as a normal option. SONAME overrides remain in Expert and appear only when configured or backed by matching Build context.
- The system-managed DebBuilder self-build is absent from the ordinary Recipe selector. **System → System-managed self-build** shows a read-only managed plan and only the backend-allowlisted fixture overrides: active, package maintainer, build environment, inactivity/maximum timeout, and resource limits.

## Fixture behavior and proof limits

Plan labels Configured, Detected, Resolved, Effective, Default, and Unknown independently. A stored Test/Build fixture is displayed only when its revision matches the saved Recipe. Unsaved edits or a save after that Run mark it stale. A fixture Test refreshes evidence for the current saved Recipe, but does not execute a real Run. Final ELF Depends appear only from a Build fixture. Source modes, service absence/configuration, build controls, mappings, identity, hooks, resources, and automation change the visible sections.

Create starts with source selection, a Test boundary, a suggested Plan, then customization and Build. Edit opens the saved Plan directly. On mobile the operator selects a Recipe, then a presentation level, then an Advanced section with a Back control. Structured list editors support add/edit/remove and ordering where relevant. The Expert JSON control exports a v5-shaped fixture and imports/applies only its represented fields; it does **not** perform backend normalization or canonical validation. Any real replacement must call the established Recipe v5 validation and preserve imported fields not modeled by this fixture.

Native `<select>` controls retain their browser semantics. Closed-state appearance, chevron, focus, disabled, error, and Light/Dark colors are styled in CSS. Chromium Playwright asserts the closed appearance and keyboard focus. `npm run test:webkit:select` is provided for Safari-engine review. A WebKit browser download succeeded, but WebKit launch is unavailable on this development host because required GTK/GStreamer/system libraries are missing; no Safari runtime claim is made.

The prototype uses no production data, API calls, or backend changes. Zoraxy/Pocket-ID/Maintainerr remain documented profile fixtures rather than production snapshots; Seerr follows the checked-in fixture at a representative level. Consult [walkthroughs](walkthroughs.md) for evidence boundaries.
