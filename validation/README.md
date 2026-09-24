# Debian validation images

Normal installed Validation uses release-owned public GHCR images by exact
`repository@sha256:...` reference. The release build places
`debbuilder/validation_images.json` inside the `.deb` only after the qualified
input lock still matches the effective Dockerfile inputs and each existing
amd64 descriptor has been anonymously pulled and checked for its digest and
OCI architecture. A normal application release does not rebuild or push these
images. DebBuilder pulls a missing exact image on the first Validation request.
A registry outage fails that request and can be retried; it does not stop the
service. An existing exact image is reused.
The exact digest is recorded at admission; the verified local image ID is
recorded before container creation. A later manifest change cannot alter the
admitted attempt.

The release-owned image repositories are
`ghcr.io/probatou/debbuilder-validation-bookworm` and
`ghcr.io/probatou/debbuilder-validation-node22`. The qualified image matrix
currently covers amd64. Unsupported profile architectures affect Validation
only; the DebBuilder package remains `Architecture: all`.

The commands below are for local image development and controlled tests.

Build the disposable Debian/systemd test image with either supported runtime:

```sh
docker build -t debbuilder-validation:bookworm -f validation/Dockerfile validation
# or
podman build -t debbuilder-validation:bookworm -f validation/Dockerfile validation
```

Validation profiles provide a curated baseline of Debian runtime packages for
offline lifecycle tests. Both profiles include the managed DebBuilder package's
declared runtime dependencies, including `podman` and `kmod`.
Validation uses
`dpkg --install` inside a network-disabled container; it does not fetch
arbitrary packages declared in `Depends`. A package requiring capabilities
outside a profile must use or add an explicitly reviewed validation profile.

The controlled allowlist also contains `bookworm-node22`. Build it with:

```sh
podman build -t debbuilder-validation:bookworm-node22 -f validation/Dockerfile.node22 validation
```

That profile is based on the official Node image
`node:22.22.1-bookworm-slim`, pinned to platform digest
`sha256:af5818e10f6294a719b4314f34ec03d8e8ad8f571a8d23742418790e6ebb5c90`.
Node comes from that upstream image's verified Node.js distribution. The image
build checks `node --version` and installs a local Debian context package named
`nodejs` version `22.22.1-1`; `dpkg-query` must confirm it is installed. This
lets an actual `dpkg --install` satisfy `Depends: nodejs` while the separate
upstream engine constraint is still checked against the real runtime.

The resulting local digest can vary if Debian package repositories change.
Bookworm's `debian:bookworm` base and both images' APT inputs are currently
unpinned. Therefore an effective Dockerfile input change stops the normal
application release. The changed profile must be built, tested, and published
in a separate qualification step, which updates its exact descriptor and
`validation/validation_images.lock.json` before the application release is
retried. A future reproducible image-build effort can pin the remaining inputs
separately. The Node base is pinned to an amd64 platform digest.

DebBuilder mounts only the selected Build Run workspace read-only at `/validation`.
The container has no network, is privileged so systemd can run, and is forcibly
removed after each validation. No package is installed on the DebBuilder host.
Recipes select only a profile name known by `debbuilder.validation_profiles`;
they cannot supply an arbitrary OCI image. Every run records the profile,
capabilities, image ID/digest, actual runtime version, and network policy.
