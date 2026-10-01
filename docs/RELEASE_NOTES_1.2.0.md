# DebBuilder v1.2.0

This feature release expands package lifecycle visibility, makes Node.js builds
independent of host-global tooling, and preserves actionable Run failures. The
changes were implemented and qualified on Dev; production qualification remains
pending until the release is authorized and deployed.

## Packages

- **Built** reflects the newest successful `.deb` artifact retained from Build
  history, while **Published** continues to report actual publication in the
  configured APT repository.
- **Status** follows the relevant active Run, and package rows update from live
  lifecycle state.
- Packages can be removed safely from the APT repository while preserving their
  Recipes and Run history. A later successful Build can recreate the Package.
- Removal is refused while a conflicting active Run exists and verifies that
  only the selected package is absent after repository mutation.

## Node.js builds

- Each admitted Run resolves and prepares an isolated Node.js toolchain from
  the project's Node engine range and exact npm or pnpm requirement.
- Official Node distributions are checksum-verified, and package-manager
  downloads are checked against published integrity metadata before immutable,
  shared cache promotion.
- Run-local `PATH`, home, and cache state remove the build-time dependency on a
  host-global Node installation. Node 22 and Node 24 builds can coexist; Dev
  qualification included DebBuilder with Node 24/npm 11 and Seerr with Node 22/
  pnpm 10.24.0.

## Diagnostics

- Runs preserve structured, actionable dependency and toolchain errors,
  including required ranges and detected versions where available.
- Validation failures preserve the profile, failed check, lifecycle phase, and
  bounded reason. Projected details are allowlisted, length-limited, and
  redacted, with the generic command-failure fallback retained.
- Historical Zoraxy validation failures cannot recover specificity
  retroactively. A new validation is required to capture the failed check and
  reason.

## Release and reliability

- Debian release artifacts are reproducible from identical source and immutable
  Validation descriptors under the supported release toolchain. Release and
  pull-request gates build independent candidates, compare SHA-256 values, and
  require byte identity before publication can proceed.
- Release construction normalizes timestamps and compression and derives
  package identity and metadata from the canonical built-in Recipe.
- Returning to System after visiting Runs once again shows the default or
  remembered System view instead of retaining a route-forced managed tab.
