# Recipe editor UX architecture audit (#24B4)

This documentation is the proposed operator-review architecture for Recipe v5. It is grounded in current code and does not modify or authorize a Recipe editor implementation.

1. [Exhaustive authored/persisted schema inventory](schema-inventory.md): types, defaults, validation, current UI and runtime effects.
2. [Applicability and progressive disclosure](applicability-matrix.md): mode gates, identities, maintainer hooks, #30 ELF, Plan visibility.
3. [Proposed information architecture](information-architecture.md): compared structures, future page, Create/Edit and provenance/Test boundaries.
4. [Structured component and list model](component-model.md): control types and multi-value editing behavior.
5. [Representative walkthroughs](walkthroughs.md): Zoraxy, Pocket-ID, Maintainerr, Seerr, managed DebBuilder with evidence limits.
6. [Migration notes and review questions](migration-notes.md): parity checks, known documentation discrepancies and three genuine operator choices.

The key product separation is **configured Recipe policy**, **detected source facts**, **suggested edits**, **Run-resolved identity**, and **effective Build/Validation evidence**. Low-frequency controls remain reachable without being shown in the ordinary path. Inapplicable values remain inspectable for safe round-tripping.
