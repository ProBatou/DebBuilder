# Design system v0 — principles

Approved direction: **A, Calm operations desk**, with **B, Guided workshop** used for first package creation, source/detection, plan review and blocker resolution. This system is a working reference for #24B, not a final pixel specification. The [isolated prototype](../../../prototypes/issue-24/README.md) implements it without changing `static/`.

1. **Make consequence legible.** Every operational state should say what happened, what it affects, and the next safe action. Exact artifact, Run and publication proof remain distinct.
2. **Calm hierarchy.** One primary action per task area, moderate density, flat surfaces, a clear reading order, restrained borders and color. Critical state sits above metadata and logs.
3. **Provenance before certainty.** `Detected` is an observation; `Suggested` awaits acceptance; `Configured` is an operator choice; `Resolved` comes from an authoritative Test/Build result; `Unknown` has not been checked. Never display fixture or inference as current backend truth.
4. **Guide selectively.** The Recipe path explains source, detection and review. Frequent operations such as Run inspection, publication and Settings remain direct screens, not a global wizard.
5. **Keep safety visible.** Blockers, capability limits, cancellation states and failed proof are textual and persist beyond a toast. Simulated prototype actions are visibly labeled; production must use backend authority.
6. **Work across sizes.** Desktop may show a contextual side panel; mobile follows the same object hierarchy in one column, with table scrolling contained and complete labels.

The three exploratory directions remain in [visual-directions.md](../visual-directions.md); #24B fixes A as the reference and does not use C as the primary language.

## #24B2 refinement

The operator review keeps the Calm operations desk direction and Svelte choice. Top-level navigation stays at six destinations. Packages shows DebBuilder-managed packages and a compact repository summary; its secondary read-only inventory represents what the repository manager contains. System Health represents whether the repository is operating correctly. These three views must not be treated as interchangeable. A desktop rail stays fixed in the viewport while main content scrolls; mobile has its own navigation. Theme and language are browser-local preferences documented in [theme.md](theme.md) and [i18n.md](i18n.md).
