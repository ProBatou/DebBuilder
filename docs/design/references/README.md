# #24B2 operator review references

These PNGs are generated from the isolated Svelte fixture prototype with `npm run test:browser -- --capture` at 1440×1000 and 390×844. The clean screenshots use `?clean=1`, which removes prototype controls entirely. Open the live prototype without that query to switch fixtures. No API or production data is used. These are review images, not fixed visual regression baselines.

| Screen | Desktop Light EN | Mobile Light EN | Additional review |
| --- | --- | --- | --- |
| Overview | [normal](desktop-overview-light-en.png) | [normal](mobile-overview-light-en.png) | [dark](desktop-overview-dark-en-normal.png), [one action FR](desktop-overview-light-fr-one.png), [many actions FR mobile](mobile-overview-dark-fr-manyActions.png), [many actions](desktop-overview-many-actions.png) |
| Packages | [list and compact repository summary](desktop-packages-light-en.png) | [list](mobile-packages-light-en.png) | [28 packages](desktop-packages-many.png), [repository inventory](desktop-packages-repository-inventory.png), [mobile inventory](mobile-packages-repository-inventory.png), [many packages ES mobile](mobile-packages-light-es-manyPackages.png) |
| Recipes | [editor](desktop-recipes-light-en.png) | [list](mobile-recipes-light-en.png) | [blocker DE dark](desktop-recipes-dark-de-blocker.png), [mobile detail DE dark](mobile-recipes-dark-de-blocker.png) |
| Runs | [list/detail](desktop-runs-light-en.png) | [list](mobile-runs-light-en.png) | [failure dark](desktop-runs-dark-en-failed.png), [running ES dark mobile](mobile-runs-dark-es-running.png), [many Runs](desktop-runs-many.png) |
| System | [Health](desktop-system-light-en.png) | [Health](mobile-system-light-en.png) | [recovery FR dark](desktop-system-dark-fr-recovery.png) |
| Settings | [General](desktop-settings-light-en.png) | [General](mobile-settings-light-en.png) | [dark ES](desktop-settings-dark-es-normal.png) |

The browser test also exercises 0/1/3/8+ actions, 28 packages, 30 Runs, all four locales across all six pages at desktop/mobile widths, sidebar collapse/persistence/sticky anchors, mobile navigation, Recipe blocker, dialogs, Run poller mounting, theme and language persistence and overflow. The live preview is required to review secondary repository inventory, search, selection, drill-down, Advanced disclosures and theme/language controls.
