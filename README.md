# DebBuilder

DebBuilder is a self-hosted console for building, validating, and publishing Debian packages from GitHub sources into a personal APT repository.

## Current features

- GitHub release, tag, source-archive, and official release-asset acquisition;
- Node.js, Python, Rust, and static project detection;
- declarative source modifications and source builds;
- upstream Debian artifact validation;
- FHS-aware Debian package generation with per-file ownership and modes;
- service-account, persistent-directory, and advanced systemd unit generation;
- Podman-based validation profiles and toolchains;
- `reprepro` publication and reconciliation;
- package lifecycle and Build Run tracking;
- versioned JSON Recipes;
- OIDC/header/disabled authentication modes and notifications.

## Project layout

```text
debbuilder/          Python backend package
static/              Browser UI
tests/               Unit and static UI tests
examples/            Public examples
data/workflows/      Versioned Recipes
data/                Other local runtime data (ignored by Git)
server.py            Entrypoint
```

## Configuration

Copy `.env.example` and adapt it for your instance:

```bash
cp .env.example .env
```

Main variables:

- `DEBBUILDER_HOST`
- `DEBBUILDER_PORT`
- `DEBBUILDER_DATA_DIR`
- `DEBBUILDER_REPO_ROOT`
- `DEBBUILDER_REPO_URL`
- `DEBBUILDER_SUITE`
- `DEBBUILDER_COMPONENT`
- `DEBBUILDER_AUTH_MODE`
- `DEBBUILDER_OIDC_*`
- `DEBBUILDER_NTFY_TOKEN`
- `GNUPGHOME`

Secrets and local runtime state are stored under `DEBBUILDER_DATA_DIR` (the source-tree `data/` directory by default) and are not intended for Git. Packaged deployments use `/var/lib/debbuilder`; their non-secret defaults are installed from `packaging/debbuilder.env` into `/etc/debbuilder/debbuilder.env` without overwriting an existing administrator-owned file.

Build tools are resolved from the same effective `PATH` used to run build commands. Administrators can extend the DebBuilder service's `PATH` in `/etc/debbuilder/debbuilder.env`, while a Recipe can provide a build-specific `PATH` through its build environment. Tools found there do not need to be owned by a Debian package; manually added build dependencies remain Debian packages checked with `dpkg-query`.

## Python projects

Python detection recognizes `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `requirements-*.txt`, `Pipfile`, `poetry.lock`, and `uv.lock`. It parses declared build-system, interpreter, dependency, and entry-point metadata without executing project files and without translating PyPI names into Debian package names.

Projects with an explicit PEP 517 build system receive `python3 -m build` as a reviewable proposal. Source applications such as DebBuilder have no compilation step: the selected runtime files are packaged directly. A lone helper `.py` file is not a strong Python marker.

## Packaged service privileges

The current package runs `debbuilder.service` as root. Real builds write isolated workspaces, OCI validation starts privileged systemd containers through Podman, and APT publication needs access to reprepro and its signing keyring. A dedicated unprivileged service account would require a separately designed rootless-Podman setup (including subordinate IDs and runtime directories) or privileged helpers; the package does not pretend that such a boundary already exists.

## Run locally

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:8099
```

## Tests

```bash
python3 -m py_compile server.py debbuilder/*.py
python3 -m unittest discover -s tests -v
node --check static/*.js
git diff --check
```

## Repository access command

The sidebar displays an install command derived from `DEBBUILDER_REPO_URL`:

```bash
curl -fsSL https://repo.example.invalid/install.sh | sudo bash
```

Clicking the command copies it to the clipboard.

## Safety notes

DebBuilder is meant to be self-hosted and operated by trusted administrators.

- Real execution is disabled unless explicitly enabled.
- Unsafe build commands are disabled unless explicitly enabled.
- Administrative API routes can be protected with OIDC or a trusted reverse-proxy header.
- Public APT files under `/dists/*`, `/pool/*`, `/repository.gpg` and `/install.sh` stay accessible without authentication.

## Version

The current release is DebBuilder 0.1.5.
