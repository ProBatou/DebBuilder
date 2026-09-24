# APT repository operations

DebBuilder owns a local signed APT repository and exposes its public assets on a
listener separate from the administrative UI/API.

## Automatic package bootstrap

The official Debian package performs mandatory local bootstrap during package
configuration and checks it again before each service start. It:

- creates the persistent application and repository directories;
- creates compatible `reprepro` configuration;
- generates a dedicated signing identity when none exists;
- stores private GPG material outside the public repository root;
- exports `repository.gpg`;
- exports signed repository metadata; and
- creates the landing page and generated client installer.

Bootstrap is idempotent and preserves a compatible existing repository and
signing identity. Unsafe, ambiguous, or incompatible state fails visibly
instead of being overwritten. Manual `reprepro` or GPG initialization is not a
normal installation step.

Packaged defaults are suite `Luminous` and component `main`. Source-development
defaults can differ and are controlled by `DEBBUILDER_SUITE` and
`DEBBUILDER_COMPONENT`.

## Public URL and listener

`DEBBUILDER_REPOSITORY_HOST` and `DEBBUILDER_REPOSITORY_PORT` control the
repository listener, which defaults to `127.0.0.1:8081`.
`DEBBUILDER_REPO_URL` is different: it is the client-reachable base URL embedded
in the landing page and `install.sh`. It may also be changed from Settings.

The URL is intentionally empty on a fresh package installation. The repository
still initializes and serves locally, but the landing page reports that client
setup is not configured and `install.sh` exits clearly. Setting a valid public
URL regenerates both managed assets.

The public listener serves only:

- `/`
- `/install.sh`
- `/repository.gpg`
- `/dists/*`
- `/pool/*`

It has no API, authentication, or administrative routes. A reverse proxy may
publish this listener under a suitable hostname; host-based routing is outside
DebBuilder.

## Client installation

After a public URL is configured, the repository landing page supplies a
command like:

```bash
curl -fsSL https://repo.example.invalid/install.sh | sudo bash
```

The installer downloads the public key, verifies the signing fingerprint
embedded during bootstrap, installs it as
`/etc/apt/keyrings/debbuilder.gpg`, creates a dedicated deb822 source using
`Signed-By`, and updates APT. The private signing key is never present in the
installer or public repository tree.

## Publication proof and concurrency

Publication and exact reconciliation take a fail-fast, repository-root-scoped
filesystem lease. The acquisition order is the application mutation gate, the
Run workspace lock, then the repository lease. The lease pins the repository
directory and is inherited by the `reprepro` child so a second process cannot
enter a conflicting mutation.

A Publication is successful only when exactly one database entry, exported
`Packages` entry, and safely opened `pool/` file agree with the retained source
artifact on distribution, component, package, version, architecture, size, and
SHA-256. This result is stored as a versioned Publication proof. Older success
records without a proof remain unverified until explicit reconciliation.

DebBuilder supports the standard repository layout and rejects path
redirections that prevent safe proof. Administrator-managed `reprepro` signing
or hook configuration remains trusted configuration, not a sandbox boundary.
Public downloads are lock-free and stream a pinned, no-follow file descriptor
so a client connection does not hold the mutation lease.
