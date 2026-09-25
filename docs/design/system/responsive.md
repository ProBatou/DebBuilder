# Design system v0 — responsive rules

At widths above 950px, show the stable rail and contextual side panels. At 950px and below, move navigation to an overlay with a named menu button; move Recipe next-step content ahead of the long form and stack Package detail under its list. At 650px and below, use one-column task content, compact metrics, a two-column stage grid and a contained scrolling Package table. These are v0 implementation thresholds, not final device classes.

The mobile Recipe keeps step names visible, shows the next action before the form and preserves source identity/provenance. Run detail keeps diagnostic text ahead of stages and logs. Long code/log content scrolls inside its region. Controls need a later production touch-target pass; the reference currently uses some compact row actions and small metadata text. Do not ship those values without accessibility review.

The [reference screenshots](../references/README.md) cover 1440×1000 desktop and 390×844 mobile. Automated browser checks assert no page-level horizontal overflow in all six normal screens and exercise keyboard dialog close, blocker resolution, Run polling, and failed/recovery states. Screenshots are review artifacts, not pixel-perfect regression baselines.
