# Design system v0 — components

| Primitive | Contract and use |
| --- | --- |
| App shell | Desktop rail with six approved destinations; mobile labeled overlay navigation. Page heading provides object context and one main action. |
| Panel/card | Flat surface, visible border, title/eyebrow, optional action/status. Use for a coherent task or fact set, not every individual field. |
| Attention hero | Persistent summary of highest-priority state, reason and next action. Danger/warning/info/success variants retain text. |
| Status chip | Text plus icon and semantic tone; words distinguish `Queued`, `Running`, `Cancelling`, `Cancelled`, `Prepared`, `Failed`, `Validated`, `Published`, `Blocked`. |
| Provenance chip | `Detected`, `Suggested`, `Configured`, `Resolved`, `Unknown`; always attached to a value or summary. A stale prior Run is labeled as prior evidence. |
| Data list/table | Heading and scan columns on desktop; within-card horizontal scrolling or labeled rows on mobile. Row actions remain keyboard reachable. Do not collapse built/published versions into one value. |
| Recipe plan row | Concept, effective/proposed value and provenance; blockers follow the relevant row with safe action. Desktop side panel names the next step. |
| Stage timeline | Ordered Source → Detection → Dependencies → Build → Staging → Package → Validation → Publication; each stage has textual state. Never infer later stages from Build success. |
| Log viewer | Distinct monospace surface, explicit live/saved status and follow/pause. Raw verbosity and metadata belong in an advanced disclosure. |
| Dialog/drawer | Named title, initial focus, Escape close and focus return. Confirmation copy identifies exact artifact/action. A drawer follows the same focus contract. |
| Forms/buttons | Native labeled controls, help/error adjacent to field; primary, secondary, text and destructive button roles. Disabled controls need a reason near the workflow. |
| Disclosure/tabs | Native semantics or equivalent keyboard behavior; Advanced retains state when collapsed. Tabs require real panel switching before production; the prototype’s Repository tab opens a labeled reference dialog only. |
| Empty/loading/error | Empty state offers first action; loading names what is being fetched; error states show affected workflow, reason and next step. Transient toasts may confirm success but must not carry the sole error explanation. |

Prototype components `StatusChip.svelte`, `Provenance.svelte` and `Modal.svelte` establish reuse. Their exact API is not a commitment to the migration implementation. The public APT landing would share type/spacing/status identity through a small separate stylesheet and remain read-only.
