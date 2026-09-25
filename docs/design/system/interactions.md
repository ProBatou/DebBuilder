# Design system — interactions and state (#24B3)

## Navigation

Desktop navigation is persistent within the viewport. Collapse changes width, labels and tooltip visibility, and persists in browser storage. Mobile has a separate overlay; Escape closes it and returns focus. The six top-level pages have no redundant breadcrumb. Object detail uses `Packages / package`, `Recipes / recipe` or `Runs / run`. Repository inventory is reached from a compact Packages summary. The secondary view shows reprepro facts and published package inventory only, with a link to the separate public landing. Installation steps and Copy controls live on that public page. System Health covers only operational state.

Overview action and recent Run rows are whole native buttons. The first five actions appear, with View all when more exist. Packages, Recipes and Runs support a keyboard reachable selector; mobile selection opens detail and Back returns to the selector. Filters/search narrow the list without mutating the fixture.

## Recipe

The simple plan summarizes source, exact identity when previously resolved, detection, build, output, installation, service and dependency proposal. The source fixture labels prior Test evidence. A blocker prevents Test/Build until confirmed. Advanced consists of five domain disclosures with selectable capability rows, varied Default/Configured/Enabled/Disabled/None states and local fixture summaries. Each row opens a right drawer on desktop or a full-width editor on mobile. Edits expose Saved/Unsaved changes, Save/Cancel and inline validation; they never call an API. Import, Export and JSON remain in the header menu. The [capability map](navigation.md) records Recipe v5 coverage. Test is asynchronous in production and does not execute Build commands or `dpkg-deb`; fixture dialogs send no request.

## Runs

A Run's status determines controls: queued/running can request cancellation; validated can publish or revalidate; prepared/completed can validate; failed opens an in-Run structured diagnosis first; a Recipe link is secondary only for a Recipe-related error. Recovery-blocked history remains readable. A compact dependency summary expands to detected, manual, bundled, overrides, unresolved and effective Depends. The compact eight-stage pipeline has translated stage names. Log options preserve the existing compact/normal/verbose/raw levels (normal default). A single follow/pause control appears for a running Run; other Runs show stored output. The fixture poller starts while Runs is mounted and stops on unmount.

## System and Settings

System Health groups runtime, queue/admission, host containment, tooling, validation capability, repository health/signing/metadata/last publication and automation; unknown capability is labeled unknown. Maintenance houses storage, history, cleanup, retention and recovery. Developer houses inspectors, support bundle and OpenAPI references. Settings exposes locally editable fixtures for the actual UI configuration fields, including workspace cleanup and failed workspace retention (0–1000). Dirty/Save/Cancel and required, URL, numeric and automation validation work without an API. Configured secrets are blank on display and new values are cleared after local Save. Theme and language remain separate persistent browser preferences.

## Focus and status

Native controls and `dialog` have a visible focus ring. Opening a dialog focuses Close; Escape closes and returns focus to the trigger. Every status has text plus icon or shape. The prototype's actions are explanatory dialogs, not simulated backend state mutations. Reduced motion removes transitions.
