# DebBuilder v1.1.1

This patch release improves operator workflows and automation stability:

- Saving a Recipe no longer reloads the Recipe picker.
- The first Automation observation establishes a baseline instead of queueing every current upstream version.
- Automation deduplicates Runs for the exact upstream version.
- Run and Validation cancellation are restored in the Svelte interface.
- Runs logs have more reliable Live/follow and Jump to latest behavior; changing verbosity preserves reading position.
- The checked-in OpenAPI snapshot now reports the correct application version.
- Node/toolchain incompatibility diagnostics are clearer.

Isolated per-Run Node.js toolchain selection is not part of v1.1.1. This release does not resolve DebBuilder/Seerr Node compatibility; [the proposal](NODE_TOOLCHAIN_PROPOSAL.md) is design input for a dedicated follow-up issue.
