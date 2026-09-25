# Design system — interactions and state (#24B2)

## Navigation

Desktop navigation is persistent within the viewport. Collapse changes width, labels and tooltip visibility, and persists in browser storage. Mobile has a separate overlay; Escape closes it and returns focus. The six top-level pages have no redundant breadcrumb. Object detail uses `Packages / package`, `Recipes / recipe` or `Runs / run`. Repository inventory is reached from a compact Packages summary. The secondary view offers copyable key/source/update commands, published package inventory and public file affordances. Copy uses the browser clipboard; public file actions show fixture dialogs. System Health covers only operational state.

Overview action and recent Run rows are whole native buttons. The first five actions appear, with View all when more exist. Packages, Recipes and Runs support a keyboard reachable selector; mobile selection opens detail and Back returns to the selector. Filters/search narrow the list without mutating the fixture.

## Recipe

The simple plan summarizes source, exact identity when previously resolved, detection, build, output, installation, service and dependency proposal. The source fixture labels prior Test evidence. A blocker prevents Test/Build until confirmed. Advanced consists of five domain disclosures: source/tracking, build/output, package/installation, service/resources, and automation/data. The [capability map](navigation.md) records Recipe v5 coverage. Test is asynchronous in production and does not execute Build commands or `dpkg-deb`; fixture dialogs send no request.

## Runs

A Run's status determines controls: queued/running can request cancellation; validated can publish or revalidate; prepared/completed can validate; failed can open Recipe review. Recovery-blocked history remains readable. A compact dependency summary expands to detected, manual, bundled, overrides, unresolved and effective Depends. Raw resolver evidence remains for future diagnostics. Log follow/pause affects viewing only. The fixture poller starts while Runs is mounted and stops on unmount.

## System and Settings

System Health groups runtime, queue/admission, host containment, tooling, validation capability, repository health/signing/metadata/last publication and automation; unknown capability is labeled unknown. Maintenance houses storage, history, cleanup, retention and recovery. Developer houses inspectors, support bundle and OpenAPI references. Settings retains the actual UI configuration fields; only theme and language operate locally in this prototype. Secrets and backend settings are read-only fixtures here.

## Focus and status

Native controls and `dialog` have a visible focus ring. Opening a dialog focuses Close; Escape closes and returns focus to the trigger. Every status has text plus icon or shape. The prototype's actions are explanatory dialogs, not simulated backend state mutations. Reduced motion removes transitions.
