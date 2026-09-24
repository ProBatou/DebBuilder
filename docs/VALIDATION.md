# Validation

Validation proves that a built Debian package can complete an offline package
lifecycle in a disposable Podman container before Publication.

## Runtime model

DebBuilder itself does not require GitHub Container Registry (GHCR) to start.
GHCR is contacted only when the exact image required by a selected Validation
profile is not already present locally.

The release package contains a manifest of immutable image descriptors. Each
descriptor binds a profile and OCI architecture to a repository and
`sha256` digest. The installed manifest is the runtime authority; mutable tags
and documentation examples are not.

On first Validation for a profile, DebBuilder:

1. selects the descriptor for the host architecture;
2. reuses the local image only if its repository, digest, and OCI architecture
   match exactly;
3. otherwise pulls the exact `repository@sha256:...` reference;
4. records the selected digest at admission and verifies the immutable local
   image ID before container creation; and
5. runs the package lifecycle with container networking disabled.

A registry outage fails that Validation request and it can be retried. It does
not prevent service startup or use of an already verified exact local image.

## Official profiles

The official repositories are:

- `ghcr.io/probatou/debbuilder-validation-bookworm`
- `ghcr.io/probatou/debbuilder-validation-node22`

The qualified profile-image matrix currently covers amd64. The Debian package
remains `Architecture: all`, but Validation reports an unsupported profile
architecture when no matching descriptor exists.

Both profiles provide a curated Debian runtime baseline, including the
dependencies required by the DebBuilder package itself. Validation does not
download arbitrary dependencies declared by a candidate package: it uses
`dpkg --install` inside the network-disabled container. A package that needs
capabilities outside a profile requires an explicitly reviewed profile.

The lifecycle checks package installation, service behavior where applicable,
upgrade from a retained previous artifact, restart, removal, purge, and
container cleanup. The Run retains Validation records and the exact image
identity used.

## Image publication boundary

Validation images are qualified independently from normal application
releases. A normal release verifies the effective version-controlled input
fingerprints against `validation/validation_images.lock.json`, checks that the
lock and immutable descriptor manifest agree, anonymously proves each exact
digest and architecture, and packages those accepted descriptors. It neither
builds nor pushes OCI images.

An effective image input change stops the application release before asset
publication. The changed profile must be built, tested, and published in a
separate qualification step; that step updates both the exact descriptor and
input lock before the normal release is retried. Package construction rejects
an incomplete, invalid, inaccessible, or mismatched descriptor set.

The current Dockerfiles use changing Debian APT inputs, so rebuilding them can
produce different bytes even when their version-controlled content is
unchanged. This is why ordinary application releases reuse already-qualified
digests. Immutability comes from the published digest recorded in each release
package. Reproducibly pinning all image inputs is separate future work.

For local image build commands and profile-maintainer details, see
[`validation/README.md`](../validation/README.md).
