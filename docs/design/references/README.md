# #24B3 final operator review references

These PNGs are generated from the isolated Svelte fixture prototype with `npm run test:browser -- --capture` at 1440×1000 and 390×844. The clean screenshots use `?clean=1`, which removes prototype controls entirely. Open the live prototype without that query to switch fixtures. No API or production data is used. These are review images, not fixed visual regression baselines.

| Screen | Desktop Light EN | Mobile Light EN | Additional review |
| --- | --- | --- | --- |
| Overview | [normal](desktop-overview-light-en.png) | [normal](mobile-overview-light-en.png) | [dark](desktop-overview-dark-en-normal.png), [one action FR](desktop-overview-light-fr-one.png), [many actions FR mobile](mobile-overview-dark-fr-manyActions.png), [many actions](desktop-overview-many-actions.png) |
| Packages | [list and compact repository summary](desktop-packages-light-en.png) | [list](mobile-packages-light-en.png) | [28 packages](desktop-packages-many.png), [repository inventory](desktop-packages-repository-inventory.png), [mobile inventory](mobile-packages-repository-inventory.png), [many packages ES mobile](mobile-packages-light-es-manyPackages.png) |
| Recipes | [simple plan](desktop-recipes-light-en.png) | [list](mobile-recipes-light-en.png) | [Advanced drawer, dirty state](desktop-recipes-advanced-editor.png), [blocker DE dark](desktop-recipes-dark-de-blocker.png), [mobile detail DE dark](mobile-recipes-dark-de-blocker.png) |
| Runs | [list/detail](desktop-runs-light-en.png) | [list](mobile-runs-light-en.png) | [running and log options](desktop-runs-running.png), [failed diagnosis](desktop-runs-failed-diagnosis.png), [many Runs](desktop-runs-many.png), [running ES dark mobile](mobile-runs-dark-es-running.png) |
| System | [Health](desktop-system-light-en.png) | [Health](mobile-system-light-en.png) | [recovery FR dark](desktop-system-dark-fr-recovery.png) |
| Settings | [editable General](desktop-settings-light-en.png) | [General](mobile-settings-light-en.png) | [validation error](desktop-settings-validation-error.png), [dark ES](desktop-settings-dark-es-normal.png) |

The browser test also exercises 0/1/3/8+ actions, 28 packages, 30 Runs, all four locales across all six pages at desktop/mobile widths, sidebar collapse/persistence/sticky anchors, mobile navigation, Recipe blocker, dialogs, Run poller mounting, theme and language persistence and overflow. The live preview is required to review local Settings/Recipe edits, search, selection, drill-down, Run log options and theme/language controls.

## Public APT landing — separate reference

The **[public APT landing](../public-repository-landing.md)** is a second, standalone prototype entry at `/repository-public.html`. It represents the future unauthenticated page served by `repo.probatou.com`. It is distinct from the Packages > Repository inventory admin subview above. It has no sidebar, Packages header, admin controls or API. Its package rows and status are fixtures; its command and link paths follow the existing repository installer/public-file contract.

| Light desktop | Dark desktop | Light mobile | Long locale / Dark mobile |
| --- | --- | --- | --- |
| [EN default](public-repository-desktop-light-en.png), [manual open](public-repository-manual-open.png) | [EN default](public-repository-desktop-dark-en.png) | [EN default](public-repository-mobile-light-en.png) | [DE default](public-repository-mobile-dark-de.png) |

Open `http://localhost:4173/repository-public.html` after starting the prototype preview. The small language control changes EN/FR/DE/ES; Light/Dark follows the browser's color scheme. `?clean=1` hides the fixture footer note for screenshots.

## #24B3 interaction boundaries

Settings and Recipe Advanced edits save only to in-memory fixtures. Secret values are never echoed. The Run diagnosis stays in Runs, with a separate Recipe navigation action for the dependency fixture. The Run list scrolls inside the selector while its search and status controls remain visible. The public landing uses `install.sh` as its primary action and keeps its three detail sections closed by default. The cube mark v0 appears in both entries and the favicon; local SVG icons make the collapsed admin navigation recognizable.
