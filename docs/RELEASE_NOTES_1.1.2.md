# DebBuilder v1.1.2

This maintenance release fixes consistency and navigation issues found in production:

- Clearing execution history no longer leaves dangling Run-backed actions in Overview. Deleted Runs are excluded from package projections, and legacy last-build references are reconciled. Published APT packages remain intact.
- Maintenance mutations invalidate the storage inventory. System shows a refreshing or collecting state until a fresh measurement completes instead of presenting stale figures as current.
- Runs logs have a bounded, responsive viewport. Compact, Normal, Verbose, and Raw views scroll internally without resizing the whole page, while Live/follow, detach, and Jump to latest continue to work.
- Previously visited pages render immediately from the in-memory session cache and silently revalidate in the background. Page pollers stop when unmounted, and mutations invalidate affected read models.

Per-Run Node toolchain isolation remains out of scope. This release does not solve Node 22/24 coexistence.
