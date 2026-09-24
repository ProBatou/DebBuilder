# DebBuilder

[![Latest release](https://img.shields.io/github/v/release/ProBatou/DebBuilder?display_name=tag)](https://github.com/ProBatou/DebBuilder/releases/latest)

DebBuilder is a self-hosted web console for turning GitHub-hosted projects into
Debian packages, validating their install lifecycle, and publishing them to a
signed personal APT repository.

The [roadmap](docs/ROADMAP.md) tracks the active v1 release audit and the
post-v1 work. The v1 release is still in progress.

## What it does

DebBuilder keeps package creation, validation, publication, and history in one
place. A Recipe describes an upstream source and its Debian packaging policy;
each execution creates a Build Run with pinned source identity and retained
artifacts.

## Features

- GitHub release, tag, source-archive, and official release-asset acquisition
- Node.js, Python, Rust, and static-project detection
- declarative source changes, build commands, Debian metadata, file ownership,
  persistent directories, and systemd units
- Podman-based install, upgrade, restart, removal, and purge validation
- signed `reprepro` publication with exact package reconciliation
- manual and automated Recipe lifecycles, Run history, OIDC/reverse-proxy
  authentication modes, and notifications

## How it works

```text
Recipe -> Build -> Validation -> Publication
```

1. A **Recipe** selects an upstream source and defines how to build and package
   it.
2. **Build** resolves an exact upstream identity and produces a Debian package
   in an isolated Run workspace.
3. **Validation** exercises the package lifecycle in a disposable Podman
   container with networking disabled.
4. **Publication** adds the verified artifact to the signed APT repository.

Automation uses the same lifecycle and never changes the policy or upstream
identity already admitted for a Run. See [Architecture](docs/ARCHITECTURE.md)
for the deeper model.

## Installation

> **Release status:** The steps below describe the qualified v1 installation
> contract. v1 is not published yet; the badge above always reports the latest
> public release.

The currently qualified installation target is Debian 13 on amd64, with
systemd and cgroup v2. The host needs network access for normal APT dependency
installation, GitHub source acquisition, and the first pull of each selected
Validation image.

When the v1 package is published, download its Debian package and `SHA256SUMS`
from [GitHub Releases](https://github.com/ProBatou/DebBuilder/releases), verify
the checksum, then install the local package with APT:

```bash
sha256sum --check SHA256SUMS
sudo apt install ./debbuilder_<version>_all.deb
```

On a new Debian installation, run `sudo apt update` first if package indexes
have not been downloaded. Do not use raw `dpkg -i` as the normal installation
path: APT installs DebBuilder's declared runtime dependencies automatically.

The qualified v1 package performs all mandatory local bootstrap. It installs
and enables the systemd service, initializes `reprepro`, generates the
repository signing key, exports the public key, and creates the repository
landing page and client installer. No manual repository or GPG setup is
required.

Its declared runtime dependencies are `python3`, `python3-dbus`, `reprepro`,
`gnupg`, `gpgv`, `podman`, `kmod`, and `ca-certificates`; APT installs them as
package dependencies rather than as manual bootstrap steps.

## Access

DebBuilder starts two independently configurable loopback listeners:

| Surface | Default | Purpose |
| --- | --- | --- |
| Admin UI and API | `http://127.0.0.1:8099` | Trusted administration, Recipes, Runs, settings |
| Public APT repository | `http://127.0.0.1:8081` | Repository metadata, packages, public key, client installer |

Use an SSH tunnel locally or place one or both listeners behind an appropriate
reverse proxy. If the admin surface is exposed beyond localhost, protect it
with access control such as OIDC, a trusted reverse-proxy identity header, or a
VPN. The repository surface is intentionally public and contains no admin
routes. DebBuilder does not route these surfaces by hostname.

## First use

1. Open the admin listener and review **Settings**, especially the public APT
   repository URL and authentication policy.
2. Create or import a Recipe and use **Test** to review its proposed package.
3. Run **Build**, then **Validation**.
4. Publish the validated artifact.
5. Open the public repository landing page and use its generated `install.sh`
   command on an APT client.

## APT repository

Packaged installations default to suite `Luminous` and component `main`. The
public URL is intentionally unset at first: the local signed repository is
ready, but client instructions are enabled only after the administrator sets a
client-reachable URL in Settings or `DEBBUILDER_REPO_URL`.

Once configured, the repository listener serves:

- `/` — repository landing page
- `/install.sh` — generated client configuration script
- `/repository.gpg` — public repository signing key
- `/dists/*` and `/pool/*` — signed APT metadata and packages

The private signing key stays outside the public repository root. The generated
installer verifies the expected public-key fingerprint, creates a dedicated
keyring and deb822 source, and runs `apt-get update`. See
[APT repository operations](docs/APT_REPOSITORY.md).

## Validation

DebBuilder does **not** need GitHub Container Registry (GHCR) to start. GHCR is
used only when a Validation profile's image is missing locally. Official
Validation images currently live at:

- `ghcr.io/probatou/debbuilder-validation-bookworm`
- `ghcr.io/probatou/debbuilder-validation-node22`

The release package contains immutable repository-and-digest descriptors. On
first use, DebBuilder pulls the exact digest if it is absent; a verified local
copy can be reused. The package install/upgrade/restart/remove/purge lifecycle
then runs in a disposable container with networking disabled. The manifest in
the installed release package—not a mutable tag—is the runtime authority.

The qualified Validation image matrix is currently amd64-only. See
[Validation](docs/VALIDATION.md) for lifecycle and trust details.

## Configuration

Packaged defaults are installed at `/etc/debbuilder/debbuilder.env` without
overwriting an administrator-owned file. Persistent state, Run history,
repository data, settings, secrets, and the private signing home live under
`/var/lib/debbuilder` by default.

Important environment settings include:

- `DEBBUILDER_HOST` / `DEBBUILDER_PORT`
- `DEBBUILDER_REPOSITORY_HOST` / `DEBBUILDER_REPOSITORY_PORT`
- `DEBBUILDER_REPO_URL`, `DEBBUILDER_SUITE`, and `DEBBUILDER_COMPONENT`
- `DEBBUILDER_DATA_DIR` and `DEBBUILDER_REPO_ROOT`
- `DEBBUILDER_AUTH_MODE`, `DEBBUILDER_OIDC_*`, and `DEBBUILDER_NTFY_TOKEN`

See [.env.example](.env.example) for source-development defaults and
[Operations](docs/OPERATIONS.md) for persistence, cleanup, recovery, and
containment behavior.

## Security model

DebBuilder is an administrative tool for trusted operators. Build Recipes can
run upstream code as root on the host, and Validation uses privileged
containers; command containment limits accidents and cleanup scope but is not
a security sandbox. Only trusted administrators should edit Recipes or access
the admin API.

The two HTTP listeners separate the trusted admin surface from intentionally
public APT assets. Private GPG material and application secrets are never
served by the public listener. Please report vulnerabilities as described in
[SECURITY.md](SECURITY.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [APT repository operations](docs/APT_REPOSITORY.md)
- [Validation](docs/VALIDATION.md)
- [Release process](docs/RELEASE_PROCESS.md)
- [Development guide](docs/DEVELOPMENT.md)
- [Roadmap](https://github.com/ProBatou/DebBuilder/blob/main/docs/ROADMAP.md)

## Development and tests

For a source checkout, copy `.env.example` to `.env`, load it into the process
environment, and start the server:

```bash
cp .env.example .env
set -a
. ./.env
set +a
python3 server.py
```

Run the core checks with:

```bash
python3 -m py_compile server.py debbuilder/*.py
python3 -m unittest discover -s tests -v
for file in $(find static -name '*.js' -type f); do node --check "$file"; done
git diff --check
```

Browser tests use Playwright: `npm install`, `npx playwright install chromium`,
then `npm run test:ui`. Development scenarios and contributor rules are in the
[development guide](docs/DEVELOPMENT.md).

## Version and release status

The badge at the top of this page reads the latest public release directly from
GitHub. The repository is preparing for v1, but v1 has not been released.
