# Release process

DebBuilder GitHub Releases are the bootstrap and recovery distribution channel.
Every version tag builds its official Debian package from that exact tagged
source and publishes the package together with `SHA256SUMS`.

## Local package proof

From the source revision being released, run:

```sh
python3 -m debbuilder.release_build \
  --tag v0.3.0 \
  --source-root . \
  --output-dir release-assets
```

The helper validates the tag against `debbuilder.__version__`, reads package
identity, Debian revision, architecture, dependencies, installation mappings,
and service policy from the managed built-in Recipe, and uses the same build
output, Debian staging, package construction, and inspection modules as a
normal DebBuilder build. Intermediate source, staging, extraction, and control
data live in temporary directories. The only retained outputs are the checked
`.deb` and `SHA256SUMS` in a newly created output directory. Production paths
under `/opt/debbuilder` and `/var/lib/debbuilder` are refused as output targets.

## GitHub workflow boundary

The Release workflow checks out the exact tag, runs the packaging-focused test
suite, builds and inspects the `.deb`, verifies its checksum, and passes the two
assets to a separate least-privilege publication job. That job creates a draft,
verifies both remote asset names, and only then makes the Release public and
latest. Package filenames and expected metadata are derived from the canonical
Recipe rather than repeated in workflow shell.

GitHub-hosted CI intentionally does not perform DebBuilder's full installation
lifecycle validation. That validator installs packages in a disposable,
network-disabled, privileged Podman/systemd environment; reproducing it inside
a hosted runner would add privilege and nested-runtime fragility. Full
lifecycle validation remains the separate DEV/runtime capability documented in
[`validation/README.md`](../validation/README.md). The portable Release gate
still validates the Recipe, generates the package, inspects Debian metadata and
contents, extracts and compares the systemd unit with the canonical Recipe,
proves the packaged runtime data path, and verifies SHA-256.
