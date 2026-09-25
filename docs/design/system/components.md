# Design system — components (#24B3)

The approved six destinations are Overview, Packages, Recipes, Runs, System and Settings. Repository inventory is a secondary read-only Packages view, not a top-level destination or permanent tab.

| Component | Behavior |
| --- | --- |
| Shell | Desktop rail stays in the viewport, expands/collapses with a local preference, and anchors repository state and version at the bottom. Mobile uses a separate overlay menu. Top-level pages have one title and one subtitle; breadcrumbs appear only in object detail. |
| Attention | Compact healthy state on Overview; a prominent notice appears for blocker, failure or recovery. The first five priority actions appear as whole-row buttons with a View all link when more exist. |
| List and detail | Packages, Recipes and Runs have searchable selectors. Packages and Runs filter by state. Native button rows support pointer, Enter and Space; `aria-current` and visible focus expose selection. Desktop details are content height; mobile opens detail with Back. |
| Repository summary and inventory | Packages shows a compact repository summary. View repository inventory opens a read-only secondary view of the repository manager’s exact contents, which can differ from managed Packages. The inventory view contains repository facts and published package/version/architecture only, plus a link to the independent public landing. System Health shows only operational repository state. |
| Recipe plan | Source, prior Test identity, detection, effective package plan, provenance, blockers and next action. Advanced groups retain the Recipe v5 capability map; rows open a local fixture editor in a desktop drawer or full mobile view. |
| Run detail | Contextual controls depend on lifecycle. The dependency summary is collapsed by default and exposes detected/manual/bundled/override/unresolved/effective values on demand. A compact eight-stage pipeline, structured in-Run diagnosis and log options are separate. |
| System tabs | Health, Maintenance and Developer. Self-build is a compact managed section. Unknown capability remains explicitly unknown. |
| Settings tabs | General, Repository, GitHub, Authentication, Notifications, Automation and Advanced. Theme and language are browser preferences in General. Editable cleanup preferences are in Settings > Advanced; operational maintenance stays in System. |
| Status and provenance | Text and shape accompany semantic color. `Detected`, `Suggested`, `Resolved`, `Unknown` and capability states `Default`, `Configured`, `Enabled`, `Disabled`, `None` carry different meanings and visual treatments. |
| Dialog | Native `dialog` has title, initial focus, Escape close and focus return. Fixture actions explain their boundary. |

The prototype uses `StatusChip.svelte`, `Provenance.svelte` and `Modal.svelte` across screens. This is a design reference; API parity is a later checkpoint.

The cube mark v0 and six local line SVG icons share one style in the expanded/collapsed rail, public landing and favicon.
