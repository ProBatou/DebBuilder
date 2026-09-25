# Audit conclusions, parity gates, and operator decisions

This checkpoint produces documentation only. It does not change the Svelte Recipe page, CSS, static frontend, API or backend. The following is a review gate before a future implementation checkpoint, not permission to start one.

## Source hierarchy and limitations

1. Canonical authored/persisted contract: `debbuilder/recipe_schema.py` and its subordinate normalizers for archive payload, resource limits and runtime APT repositories. `tests/test_recipe_schema.py`, `tests/test_recipe_store.py`, `tests/test_builtin_recipe.py`, `tests/test_elf_dependency_resolution.py`, and `tests/test_build_runs.py` pin important boundaries.
2. Current editor/serialization: `static/index.html`, `static/recipe_serialization.js`, `static/app.js`, `static/js/recipe/{source_changes,archive_tree,automation,json_editor}.js` and browser tests. A displayed control is not proof of backend applicability.
3. Runtime effect: `build_pipeline.py`, `build_executor.py`, `debian_packaging.py`, `systemd_unit.py`, `elf_toolchain.py`, `elf_inspection.py`, `elf_dependency_resolution.py`, `dependency_preparation.py`, `inspectors.py`, and `builtin_recipe.py`. `docs/ELF_INSPECTION.md`, `docs/VALIDATION.md`, `docs/ARCHITECTURE.md`, and `docs/API.md` explain their contracts.
4. Scenario evidence: checked-in Seerr and DebBuilder v5 fixtures, Maintainerr-style test construction, and design notes for Zoraxy/Pocket-ID. No real persisted Zoraxy, Pocket-ID or Maintainerr Recipe snapshot exists in this worktree. The prototype's example rows cannot fill that gap.

## Parity checklist for the next, separately authorized implementation

- [ ] Every authored path in [schema inventory](schema-inventory.md) round-trips through create/edit/import/export/JSON, including absent/default/empty/null distinctions and inactive values after mode changes. Preserve JSON size bound, canonical preview/diff, explicit import replacement confirmation, immutable selected ID, managed/shipped refusal, canonical export and ordinary user Recipe deletion confirmation.
- [ ] Canonical v5 validation remains the final authority. Reject unknown fields, unsupported schema versions and managed mutations with the existing structured paths/codes.
- [ ] No unsupported source provider, generic URL, generated secret or `/opt` persistent-directory control appears as shipped functionality.
- [ ] Three artifact modes and their skipped stages are represented correctly; archive exact/pattern/source and payload include/exclude decisions remain available.
- [ ] Package metadata, build vs runtime dependencies, #30 opt-in detection/override, account/owner/service identity, four lifecycle hooks, mappings/policies, output/ensure directories and resource limits retain access.
- [ ] Service `configured` stays derived. A configured-disabled unit remains inspectable; enabling requires both name and command.
- [ ] Test is a Run with immutable source identity and bounded preflight; it does not execute commands/build `.deb` or certify final ELF Depends. Build and Validation have separate evidence.
- [ ] Inspectors are bounded summaries and intentionally omit raw commands, paths and scripts. The editor may read the authenticated Recipe DTO for authored values but must not reconstruct hidden data from an inspector.
- [ ] Managed self-build presents only backend-allowed overrides; its application-owned definition remains read-only and undeletable.
- [ ] Editor lists support add/edit/remove, meaningful ordering, empty state, keyboard access, inline index/path validation, and clear provenance. Secrets and user-provided shell content never leak into public references.
- [ ] The actual preview is tested on source-build, raw asset, source archive, upstream `.deb`, configured-files-only and managed fixtures, plus stale evidence after edits and mobile layout.

## Known discrepancies to resolve explicitly

- Earlier `docs/design/information-architecture.md` and `docs/design/progressive-disclosure.md` overstated `/opt` directory support. Their statements are corrected in this documentation checkpoint: `recipe_schema.py` currently allows only package-specific `/etc`, `/var/lib`, `/var/log` paths. An approved backend extension remains separate from #24B4.
- The current static form has no structured resource-limit or runtime APT repository editor; serialization preserves those fields. A future structured control must meet the existing backend validation/trust contract rather than relying on JSON text serialization alone.
- The #24B3 Svelte Recipe Advanced drawer represents several structured values with one text field/textarea, and shows ELF at the same level as routine options. It should be replaced only after this architecture is accepted.
- The schema normalizes some fields that have no demonstrated runtime consumer, such as `install.content.path`. Preserve them for round-trip compatibility but do not claim an effect or make a prominent control.
- Metadata validation permits an empty maintainer but `debian_packaging.generate_control` rejects it. An editor should surface that Build requirement before submission without pretending that metadata validation already enforces it.

## Genuine operator decisions

1. **`/opt` runtime directory:** keep the current backend and explain a reviewed lifecycle-hook/source-backed-file solution, or commission a separate backend extension for declarative creation of non-payload `/opt/<package>` directories? No UI should imply that extension exists now.
2. **Default exposure of #30 opt-in:** for eligible `amd64` Release assets, should the UI offer a compact opt-in row immediately after Test identifies the payload, or only after an operator opens Runtime dependencies? In either case the policy must remain off unless explicitly enabled.
3. **Managed self-build location:** show it only under System, or also in Recipes as a visibly separated read-only managed entry? The backend allowlist is fixed regardless.

The available evidence does not require a new Recipe schema to realize the proposed information architecture. Pocket-ID generated secrets and broader `/opt` directory provisioning are separate backend product decisions, not UI form options to invent during migration.
