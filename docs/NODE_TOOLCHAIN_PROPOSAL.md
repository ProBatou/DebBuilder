# Per-Run Node build toolchain proposal

The current project detector reads `engines.node` and `packageManager` from
`package.json` and sends tool requirements to `dependency_checker`. The checker
and `build_executor` use `command_runner` with the same controlled environment,
but both resolve binaries from the host PATH. No per-Run Node resolver, verified
download source, or isolated cache exists. A host Node 26 can therefore reach
the Seerr `pnpm` step, while the managed DebBuilder self-build requires Node 24
and npm 11. Build-time Node and package runtime dependencies are separate: a
packaged service may still require Node at runtime.

Implementation should add a resolver before dependency checking. For Node
projects it should parse the upstream range with a maintained semver parser,
reject unsupported syntax, and choose a pinned compatible version from an
operator-approved inventory. The inventory must bind each Node archive to an
upstream provenance URL and SHA-256 checksum. Download into a bounded cache,
verify before extraction, reject unsafe archive paths, and promote atomically.
The Run records the selected exact version and digest. An explicit cache lease
keeps the chosen toolchain available through cancellation and recovery.

The resolver should create a Run-owned bin directory or environment prefix and
prepend it to PATH only for dependency checks and build commands. It should
prepare the `packageManager` version from `package.json` under the same Node
toolchain, with a verified source and pinned version. `pnpm@10.24.0` must not
be upgraded implicitly; the managed self-build must resolve npm 11.x. Both
checks and execution must use the existing command runner, timeout, cancellation,
and systemd containment path. Diagnostics should report required, selected,
and failure versions without revealing internal cache paths.

This checkpoint has no trusted Node archive inventory, checksum catalog,
cache lifecycle, or tested semver resolver. Introducing unverified downloads
or a new broad package repository would violate the toolchain safety condition,
so the resolver remains a follow-up design task. Unsupported range syntax now
fails the availability check instead of silently accepting host Node.
