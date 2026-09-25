# #24B high-fidelity references

Generated from the isolated Svelte prototype with `npm run test:browser -- --capture` at 1440×1000 and 390×844. All imagery uses deterministic fixtures; no production or admin API data is included. These are design-review images, not frozen visual regression baselines.

| Screen | Desktop | Mobile |
| --- | --- | --- |
| Overview | [normal](desktop-overview-normal.png), [empty](desktop-overview-empty.png) | [normal](mobile-overview-normal.png), [empty](mobile-overview-empty.png) |
| Packages / Repository | [normal](desktop-packages-normal.png) | [normal](mobile-packages-normal.png) |
| Recipe editor | [normal](desktop-recipes-normal.png), [blocker](desktop-recipes-blocker.png) | [normal](mobile-recipes-normal.png), [blocker](mobile-recipes-blocker.png) |
| Run detail | [normal](desktop-runs-normal.png), [running](desktop-runs-running.png), [failed](desktop-runs-failed.png) | [normal](mobile-runs-normal.png), [running](mobile-runs-running.png), [failed](mobile-runs-failed.png) |
| System | [normal](desktop-system-normal.png), [recovery blocked](desktop-system-recovery.png) | [normal](mobile-system-normal.png), [recovery blocked](mobile-system-recovery.png) |
| Settings | [normal](desktop-settings-normal.png) | [normal](mobile-settings-normal.png) |

The prototype itself adds interaction states: source choice, Advanced disclosure, blocker confirmation, Test/Build/cancellation/validation/publication explanation dialogs, simulated Run polling and log follow/pause. It is a reference, not an alternate production frontend.
