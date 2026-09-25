# #24A — Visual directions and design system seed

These are three comparable candidates for six later high-fidelity desktop/mobile reference screens: Overview, Recipe editor, Run detail, Packages/Repository, System, Settings. None is selected at #24A.

| Dimension | A. Calm operations desk | B. Guided workshop | C. Compact control room |
| --- | --- | --- | --- |
| Personality | Precise, quiet, trustworthy operational console. | Approachable, instructional maker workflow. | Dense, expert-facing monitoring surface. |
| Density/navigation | Moderate density; stable side rail, clear page title and contextual tabs. Mobile uses labeled menu and focused task pages. | Low/moderate density; task-led start screen, step navigation and sticky progress. Mobile uses one step at a time. | High density; compact rail and multi-pane Run/Package views. Mobile collapses panes into ordered drill-down. |
| Panels/cards | Flat layered surfaces, restrained borders; “attention” area prominent. | Larger roomy panels with explanatory summaries and review cards. | Tight panels with compact status rows and resizable log emphasis. |
| Tables/lists | Scannable rows, fixed status/next action columns, expandable detail. | Card-list summaries with explicit task action; tabular metadata appears on demand. | Denser tables with filters and keyboard-oriented selection. |
| Forms | Groups by outcome; resolved summary adjacent to controls. | Wizard-like questions, inline explanation and reviewed proposal cards. | Compact two-column controls with advanced inspector. |
| Status language | Text + icon + severity + next step; distinguishes queued, running, prepared, blocked and publication proof. | Plain-language sentences (“Ready to validate”), reasons/actions near each step. | Compact chips plus stage timeline and expandable diagnostic detail. |
| Logs/code | Monospace terminal within Run detail, follow/pause and copy controls. | Collapsed by default, contextual excerpt and “Open full log.” | Prominent log pane with stage navigation and structured metadata. |
| Why it fits | Respects serious package operations while removing needless noise. | Strongest at first package and nonexpert workflows. | Good for frequent operators and debugging several Runs. |
| Main risk | Could feel conventional and under-explain new workflows. | Could make repeat operations slow and consume screen space. | Could preserve the very density #24 intends to reduce. |

**Candidate to explore first: A**, because it can support an efficient repeat workflow and visible safety state while accommodating B’s guided first-run panels. This is a design exploration priority, not a final visual selection. Test all three against a single-binary Recipe, a failed Validation/Publication Run, and mobile navigation before choosing.

## Shared design system structure

- **Tokens:** semantic surfaces/text/borders/status, type roles and numeric/monospace roles, spacing scale, content widths, panel radius/shadow, focus ring, control/touch sizes and responsive thresholds. Use role names; defer exact colors/pixels.
- **Shell/navigation:** persistent desktop landmark, mobile labeled navigation with focus/Escape handling, breadcrumb/object identity, page heading, primary action slot and attention summary.
- **Content primitives:** card/panel, definition list, responsive data list/table, status timeline, summary row, metadata disclosure, code/log viewer, empty/loading/error and capability notices.
- **Forms:** labeled field, help/error, grouped fieldset, source chooser, file/path mapping row, segmented control, buttons (primary/secondary/destructive), tabs and accordions. A consistent Simple/Advanced disclosure retains state and deep links where useful.
- **Overlays:** one dialog and drawer contract for title/description, focus entry/trap/return, Escape/backdrop, pending state, destructive confirmation and mobile sizing.
- **Status/provenance:** badge and icon never rely on color alone; shared `Detected/Suggested/Configured/Resolved/Unknown` chips, timestamps and explanatory hover/focus text. Warning/error/info/success components state workflow impact and action.
- **Interaction:** visible focus, hover and disabled reasons; keyboard list selection; live-region policy for async updates; reduced-motion transitions; no animation that conveys unique information.
- **Responsive:** decide column collapse per content; keep Recipe step names visible; turn tables into labeled rows without losing version comparisons; let logs scroll within their own region; keep primary action and blockers in view without covering fields. Validate at 390px and a larger phone/tablet width.

The public APT page can share type/status identity and spacing tokens through a tiny generated stylesheet, while remaining a separate read-only document without admin navigation or controls.
