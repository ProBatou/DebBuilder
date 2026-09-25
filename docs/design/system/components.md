# Design system — components (#24B2)

The approved six destinations are Overview, Packages, Recipes, Runs, System and Settings. Repository inventory is a secondary read-only Packages view, not a top-level destination or permanent tab.

| Component | Behavior |
| --- | --- |
| Shell | Desktop rail stays in the viewport, expands/collapses with a local preference, and anchors repository state and version at the bottom. Mobile uses a separate overlay menu. Top-level pages have one title and one subtitle; breadcrumbs appear only in object detail. |
| Attention | Compact healthy state on Overview; a prominent notice appears for blocker, failure or recovery. The first five priority actions appear as whole-row buttons with a View all link when more exist. |
| List and detail | Packages, Recipes and Runs have searchable selectors. Packages and Runs filter by state. Native button rows support pointer, Enter and Space; `aria-current` and visible focus expose selection. Desktop details are content height; mobile opens detail with Back. |
| Repository summary and inventory | Packages shows a compact repository summary. View repository inventory opens a read-only secondary view of the repository manager’s exact contents, which can differ from managed Packages. The inventory view includes three installation commands with working Copy controls and public file actions; those file actions remain fixture dialogs. System Health shows only operational repository state. |
| Recipe plan | Source, prior Test identity, detection, effective package plan, provenance, blockers and next action. Advanced groups retain the Recipe v5 capability map. |
| Run detail | Contextual controls depend on lifecycle. The dependency summary is collapsed by default and exposes detected/manual/bundled/override/unresolved/effective values on demand. Stage list and log are separate. |
| System tabs | Health, Maintenance and Developer. Self-build is a compact managed section. Unknown capability remains explicitly unknown. |
| Settings tabs | General, Repository, GitHub, Authentication, Notifications, Automation and Advanced. Theme and language are browser preferences in General. Maintenance is in System. |
| Status and provenance | Text and shape accompany semantic color. `Detected`, `Suggested`, `Configured`, `Resolved` and `Unknown` carry different meanings and visual treatments. |
| Dialog | Native `dialog` has title, initial focus, Escape close and focus return. Fixture actions explain their boundary. |

The prototype uses `StatusChip.svelte`, `Provenance.svelte` and `Modal.svelte` across screens. This is a design reference; API parity is a later checkpoint.
