# Debian validation image

Build the disposable Debian/systemd test image with either supported runtime:

```sh
docker build -t debbuilder-validation:bookworm -f validation/Dockerfile validation
# or
podman build -t debbuilder-validation:bookworm -f validation/Dockerfile validation
```

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

The image built for the 2026-09-03 validation was 265,468,342 bytes, image ID
`b8cf192d78dd118509bb92174122ae03b2f010ba2217c58793befbd884cad1fe`,
and local digest
`sha256:2127aa66bf250e9f5c80711c0d9b6135da21c3e066fcf8e62f2f41c639bc048a`.
Rebuilding from the pinned Dockerfile is the reproducible source of truth; the
resulting local digest can vary if Debian package repositories change.

DebBuilder mounts only the selected Build Run workspace read-only at `/validation`.
The container has no network, is privileged so systemd can run, and is forcibly
removed after each validation. No package is installed on the DebBuilder host.
Recipes select only a profile name known by `debbuilder.validation_profiles`;
they cannot supply an arbitrary OCI image. Every run records the profile,
capabilities, image ID/digest, actual runtime version, and network policy.
