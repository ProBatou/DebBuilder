# Design system v0 — interactions and state

## Recipe

Show a guided strip only for Source → Detection → Review → Test/Build. The default plan summarizes source identity, detected project, no-compilation/build command, output, installation, service, runtime dependency proposal, blockers and next action. Advanced reveals command/environment, mappings/permissions, detailed systemd, resource limits, maintainer scripts and ELF overrides. `Resolved` is allowed only for a value returned by an authoritative existing operation; a dependency proposal remains `Suggested` until final staging/Build evidence exists. The reference labels its previous Test source identity accordingly. No #25 source options appear.

Test is asynchronous and non-destructive in this reference. In production it creates a Run and prepares source and staging without executing Build commands or `dpkg-deb`. Close does not cancel a real Run. Build admission, queue capacity and cancellation remain backend decisions.

## Runs and actions

A Run detail shows current lifecycle, exact Recipe/source snapshot, stage timeline, primary diagnostic, available actions and logs. Running work can request cancellation; a terminal Run cannot. Validation is a separate attempt; publication requires a matching proof for the exact artifact and holds the existing repository lock. Recovery-blocked admission is persistent and visible even if history remains readable. Structured error code is inspectable below a plain-language diagnosis.

The spike’s `RunPoller.svelte` owns a fixture polling loop. It starts on mount, stops on unmount, rejects stale/aborted results and never overlaps requests. The real implementation would substitute a read-only API adapter and retain these lifecycle boundaries; this spike sends no backend calls. Log follow/pause changes viewport behavior, not Run execution.

## Keyboard and focus

Primary navigation uses buttons and `aria-current`; mobile menu exposes `aria-expanded`. Native controls and `dialog` receive visible focus. Opening a dialog records the invoking element, initial focus goes to its close control, Escape closes it and focus returns. Destructive action confirmations need exact object/action text before production. Announce state transitions selectively; do not stream every log line to a live region. Honor reduced motion for all future transitions.
