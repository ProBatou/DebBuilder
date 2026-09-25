# #24A — Migration and functional parity plan

No implementation begins until #24A review resolves the visual direction, default/advanced boundary, happy path and frontend architecture. The plan below preserves backend authority and gives #24B measurable checkpoints.

## Phases

1. **Approve design targets:** compare the three [directions](visual-directions.md) using high-fidelity desktop/mobile references for Overview, Recipe, Run, Packages/Repository, System and Settings. Record chosen tokens, interaction rules and object map. Resolve any proposed backend gap separately.
2. **Freeze behavior inventory:** map every existing control/API contract to a new destination, including failure/empty/loading states. Capture stable browser assertions for normal, queued, running, prepared, failed, cancelled, recovery, validation, publication and mobile navigation. Keep the current Playwright/Behavior Lab suite as the baseline.
3. **Shared foundation:** chosen architecture, tokens, shell/navigation, API/error client, status/provenance, form/dialog/drawer/list/log primitives and lifecycle controllers. Check keyboard/mobile behavior before screen migration.
4. **Vertical slice:** new package/source entry, one Recipe section, Test/preflight and Run detail. Use real GitHub-only API contracts and Recipe v5 adapters. Verify import/export, managed restrictions and automation before expanding.
5. **Remaining surfaces:** Packages/Repository, Dashboard/Overview, System and Settings/Maintenance. Keep lifecycle actions tied to canonical Run/artifact. Route existing deep links and action outcomes consistently.
6. **Parity and cleanup:** compare behavior independent of screenshots, remove superseded selectors/scripts/assets, then establish selected visual references. Verify Debian package contains correct static build output if a framework is chosen. Avoid long-lived double frontend.

## Critical parity checklist

- [ ] Auth/session behavior, 401/403 handling and settings/secret persistence.
- [ ] Recipe v5 round-trip, migrations/validation errors, autosave revisions, dirty/in-flight controls, JSON validate/apply/import/export and file import.
- [ ] Managed DebBuilder Recipe read-only paths, allowed operational overrides, and delete prohibition.
- [ ] GitHub source tracking/ref/version, repository/release/tag/source archive/Release asset exact and pattern selection, archive inspection/tree selection, raw asset staging and resolved identity.
- [ ] Project detection/proposal versus accepted commands; source changes; post-build `ensure_directories`; install mappings/policies, account, directories, service and resource limits.
- [ ] Test vs Build semantics, preflight report and truthful “commands not executed” note, async queued/running/prepared/failed/cancelling/cancelled states, double-submit prevention.
- [ ] Polling start/stop on view/dialog change, Run list/detail selection, log offset/verbosity/auto-follow/manual resume, stage badges, diagnostics, structured error codes.
- [ ] Queue capacity/manager unavailable, cancellation including 409 terminal race, recovery interrupted and recovery-blocked admission, graceful shutdown representation.
- [ ] Package observation, automation check/retry/status, generation/revision and policy; associated Run links.
- [ ] Validation attempt start/poll/cancel/retry/results and dependency preparation; Publication exact proof, eligibility, confirmation, lock/contention, retry/history.
- [ ] Settings autosave/flush, OIDC/GitHub/notification secrets, storage measurement and destructive cleanup preview/confirmation.
- [ ] System diagnostics/capability states, Recipe/Run inspectors, scoped support bundles and raw OpenAPI link.
- [ ] Dialog Escape/backdrop/close/focus return, drawer keyboard behavior, toasts/live regions, empty/loading/errors, mobile navigation/list-detail/long forms/log scrolling.
- [ ] Public APT landing install/key/metadata links, independent of admin auth and without mutation controls.

## Gates and limits

Use Behavior Lab’s isolated data/ports and existing Playwright desktop/mobile tests. Compare the actual API response and action result, not only visual similarity. Keep logs and validation attempts attached to their Run. A frontend change must not loosen backend fail-closed validation, resource, recovery, publication or repository-lock rules. Any new source-preview API or generated-secret primitive needs a separately reviewed bounded contract; #24A makes no backend change. Do not add generic Git/Archive URL UI while #25 is deferred.
