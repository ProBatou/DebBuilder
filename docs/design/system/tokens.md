# Design system — semantic tokens (#24B2)

The source of truth is `prototypes/issue-24/src/styles.css`. The same roles are defined for light and dark; business components refer to roles, not literal palette values.

| Role | CSS token | Meaning |
| --- | --- | --- |
| App, panel, nested surface | `--canvas`, `--surface`, `--subtle` | Page, cards, grouped facts |
| Text | `--ink`, `--muted` | Primary and secondary text |
| Borders | `--line`, `--strong-line` | Panels and controls |
| Navigation | `--nav`, `--nav-ink`, `--nav-active` | Sidebar and log surface |
| Action | `--accent`, `--accent-hover`, `--accent-ink` | Buttons and links |
| Focus | `--focus` | 3px visible outline |
| Status | `--success`, `--warning`, `--danger`, `--info` and paired `-bg` tokens | Semantic state, always paired with text/shape |
| Layering | `--shadow` | Dialog and prototype tools |

Light uses a pale grey canvas, white cards and deep blue-green navigation. Dark uses navy surfaces with distinct raised cards and softened text. Success green is reserved for confirmed healthy/available states. Warning, danger and info preserve separate emphasis. Provenance tags use their own roles: detected/info, suggested/warning, configured/neutral, resolved/success, unknown/neutral.

Typography uses the system UI stack, monospace for paths, versions and logs. Page heading is 29–36px, section heading 17px, controls/body mostly 12–13px. The prototype still contains some 10–11px metadata; production needs a final touch-target and text-size review. Spacing follows 4/8/12/16/20/24px increments, with 10px card radius and restrained shadows.
