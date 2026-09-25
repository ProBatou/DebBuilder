# Design system v0 — semantic tokens

Working values from `prototypes/issue-24/src/styles.css`. Values may change during high-fidelity review; names describe roles, not page ownership.

| Role | Working value | Use |
| --- | --- | --- |
| Canvas | `#f5f7f8` | Admin page background |
| Surface / subtle surface | `#ffffff` / `#f8fafb` | Cards and nested facts |
| Ink / muted ink | `#1b2935` / `#657482` | Body and secondary copy |
| Line / strong line | `#d9e1e5` / `#c1cdd3` | Panels, controls and emphasis |
| Navigation surface | `#172832` | Stable desktop rail |
| Action | `#0c665f` | Primary button and links; hover `#0a514c` |
| Focus | `#3e93c6` | 3px visible focus outline with 2px offset |
| Success | `#1b7155` on `#e9f5ee` | Confirmed healthy state |
| Warning | `#8f5f15` on `#fff5e5` | Review/optional capability |
| Danger | `#a43c35` on `#fff1ef` | Failure/blocker/destructive context |
| Info | `#28658a` on `#eaf4fa` | Running/observed detail |

Typography: system UI stack for prose and headings, monospace for paths, versions and logs. Page title 29–36px; section heading 18px; body 12–13px in the reference. Treat 10–11px eyebrow/metadata as a v0 density choice requiring contrast and readability review before production. Use weight and line height to distinguish summary from evidence. No external font asset is required.

Spacing uses a 4px base with principal increments 8, 12, 16, 20, 24, 32 and 40px. Card radius is 10px, control radius 6–7px. Content maximum width is 1400px; desktop rail 234px; normal content gutters 38px, then 20px at tablet and 13px at phone. Shadows are reserved for modal layering. Borders and surface contrast establish hierarchy elsewhere.

Status palette is never a standalone signal. Every state component includes text and an icon/shape. Provenance has a separate neutral/blue/amber/violet/green grammar so it cannot be mistaken for lifecycle severity.
