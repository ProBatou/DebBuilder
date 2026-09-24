# Release process

DebBuilder GitHub Releases are the bootstrap and recovery distribution channel.
Every version tag builds its official Debian package from that exact tagged
source and publishes the package together with `SHA256SUMS`.

## Local package proof

From the source revision being released, run:

```sh
python3 -m debbuilder.release_build \
  --tag vX.Y.Z \
  --source-root . \
  --validation-images /path/to/validated-image-manifest.json \
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

The Release workflow checks out the exact tag with persisted Git credentials
disabled. Its build job has read-only repository access. Validation OCI images
are independently qualified immutable inputs: the workflow verifies each
profile's effective version-controlled input fingerprint against
`validation/validation_images.lock.json`, verifies that the lock and checked-in
descriptor manifest agree, then anonymously pulls and inspects every exact
repository digest before package construction. A normal application release
does not build or push OCI images and does not require package-write access.

If an effective image input changes, the release fails before asset
publication. The changed image must be qualified and published separately;
that qualification updates both the exact descriptor and its input lock before
the normal application release is retried. Documentation files sharing the
Validation directory are not fingerprinted unless an image build actually
consumes them.

The build job runs the packaging-focused test suite, builds and inspects the
`.deb`, verifies its checksum, and passes only the checked `.deb` and
`SHA256SUMS` to a separate publication job. That job alone receives repository
contents-write permission. It creates a draft Release, verifies both remote
asset names, and only then makes the Release public and latest. Package
filenames and expected metadata are derived from the canonical Recipe rather
than repeated in workflow shell.

GitHub-hosted CI intentionally does not perform the complete installed-package
lifecycle gate. That gate installs packages in a disposable,
network-disabled, privileged Podman/systemd environment; reproducing it inside
a hosted runner would add nested-runtime fragility. Full package lifecycle
qualification remains a separate release gate. The portable workflow still
validates the Recipe, verifies the qualified Validation inputs and existing
immutable images, generates the package, inspects Debian metadata and
contents, compares the systemd unit with the
canonical Recipe, proves the packaged runtime and image descriptors, and
verifies SHA-256.

Replace `vX.Y.Z` with the prepared release tag. The tag must match the single
application release version in `debbuilder.__version__`; package version,
artifact name, checksums, and GitHub Release metadata are then derived by the
release tooling. The qualified image manifest remains an explicit immutable
input. A tag and GitHub Release do not exist until the final release gates pass
and publication is explicitly authorized.
