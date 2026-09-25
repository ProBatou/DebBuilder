# Isolated #24B2 Svelte design reference

This directory renders deterministic fixture data only. It does not replace `static/`, call a DebBuilder API, or write production data. The six destinations are Overview, Packages, Recipes, Runs, System and Settings; Repository inventory is a read-only secondary view reached from a compact Packages summary. Its installation commands can be copied locally; public file controls remain fixture dialogs. The desktop sidebar is viewport anchored and locally collapsible; mobile uses a separate menu. Theme (System/Light/Dark) and language (EN/FR/DE/ES) are local browser preferences.

```sh
npm ci
npm run check
npm run build
npm run test:i18n
npm run test:browser
npm run test:browser -- --capture
npm run preview -- --host 0.0.0.0
```

Use the floating Prototype controls to switch normal, blocker, running, failed, empty, recovery, 0/1/3/8+ actions, many packages, many Runs and long Advanced fixtures. Add `?clean=1` for a UI-only screenshot with no fixture controls; `?view=runs&scenario=failed&clean=1` opens a specific state. All action dialogs explain the future backend boundary and send no requests. See [reference index](../../docs/design/references/README.md) and [design system](../../docs/design/system/principles.md).

The relative-base Vite build emits hashed JS/CSS plus a manifest into ignored `dist/`. A future migration must handle Recipe v5 round-trip, auth/session, lifecycle/automation parity, packaging, asset caching and API authority. This checkpoint does not implement those tasks.

## Public APT landing reference

`/repository-public.html` is a **separate HTML entry** with its own Svelte root and stylesheet. It previews the unauthenticated landing at `repo.probatou.com`, independently of the admin shell and Packages > Repository inventory. The public entry follows browser Light/Dark preference and defaults its language from the browser, with a discreet EN/FR/DE/ES selector. Published packages and Online status are fixtures. The generated installer command and public links follow the repository's existing URL and path contract. The browser reference test captures and checks this page as well. Use `npm run test:browser -- --capture-public` to regenerate only its four screenshots.

The [porting note](../../docs/design/public-repository-landing.md) explains how a later checkpoint can apply the design to `debbuilder/repository_templates/index.html` while preserving the separate public listener. This spike does not perform that cutover.
