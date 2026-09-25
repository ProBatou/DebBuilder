# Isolated #24B Svelte design spike

This directory is a design reference. It does not replace `static/`, call the DebBuilder API, or write production data. Source and Run values are deterministic fixtures, and action dialogs explain the real backend boundary. The six navigation destinations follow the approved information architecture. Use the Reference state selector to inspect normal, blocker, running, failed, empty and recovery-blocked states.

```sh
npm ci
npm run check
npm run build
npm run test:browser
npm run test:browser -- --capture  # regenerate docs/design/references/*.png
npm run preview
```

The build is relative-base and emits hashed JS/CSS plus a manifest in ignored `dist/`. A future migration would run `npm ci && npm run build` **before** Debian packaging, then copy the built admin entry/assets into the source `static/` tree packaged by the built-in Recipe (`build.output.paths` includes `static`). `RuntimeConfig.static` and the HTTP handler already serve HTML/JS/CSS from that tree. The manifest is a build artifact, not a runtime API. The current static handler has a narrow MIME mapping, so a migration must either keep output to HTML/JS/CSS or deliberately extend that mapping for any added asset types. Do not modify the built-in Recipe/release pipeline during this spike. Node/npm and `node_modules` remain build/test dependencies, not Debian runtime dependencies.

The production UI still needs real API adapters, Recipe v5 round-trip, auth/session, lifecycle parity and package tests. This reference does not claim those are implemented.
