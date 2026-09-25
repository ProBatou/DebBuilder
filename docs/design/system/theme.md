# Theme behavior (#24B2)

The preference has three modes: System, Light and Dark. System follows `prefers-color-scheme: dark` and updates when that media query changes. Explicit Light/Dark overrides it. The choice is stored as `debBuilder24Theme` in localStorage and applies immediately via `data-theme` on the document root. It is independent of backend Settings. Invalid stored values fall back to System.

Both modes use the semantic token roles in [tokens.md](tokens.md). Check panels, inputs, badges, focus, disabled controls, dialogs and logs in each mode. The clean reference screenshots cover desktop Overview, Recipes, Runs, System and Settings, plus mobile Overview, Recipes and Runs in both modes, including blocker/failure cases. Automated checks verify switching and persistence; visual review remains necessary before production.

The public APT landing has its own lightweight stylesheet with the same semantic roles. It follows `prefers-color-scheme` directly and has no admin theme selector.
