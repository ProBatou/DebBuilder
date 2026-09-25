# Design system — responsive rules (#24B2)

Above 800px, the sidebar is sticky at the viewport top, exactly viewport height, with logo/navigation at top and repository/version at bottom. It remains fixed in expanded and collapsed states while the document's main content scrolls. Exceptionally short desktop viewports (550px or less) allow rail scrolling so destinations stay reachable. At 800px and below, a separate overlay navigation opens from the mobile header; desktop collapse state does not alter it.

At 600px and below, Packages, Recipes and Runs switch to a list → detail flow with a Back button. Other screens stack cards in one column. Package rows suppress built/published columns in the list, which remain in detail. The sidebar does not take space from mobile content. Long values wrap inside their panels; code/log output stays inside its region.

System, Settings and Recipe horizontal subnavigation keeps touch and keyboard scrolling at mobile widths. Native scrollbars are hidden across WebKit, Chromium and Firefox; selecting a tab scrolls it into view. A short mobile drawer may scroll internally so its repository status and version remain reachable without overlap.

The [references](../references/README.md) include 1440×1000 and 390×844 screenshots. Browser checks cover all six screens in both sizes, four locales, light/dark combinations, long fixtures, no page-level horizontal overflow, and stable desktop sidebar anchors during document scrolling.
