# DebBuilder

DebBuilder is a self-hosted console for building, validating, and publishing Debian packages from GitHub sources into a personal APT repository.

## Current features

- GitHub release, tag, and source-archive acquisition;
- project and dependency detection;
- declarative source modifications and source builds;
- upstream Debian artifact validation;
- Debian package and systemd unit generation;
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
- `DEBBUILDER_REPO_URL`
- `DEBBUILDER_SUITE`
- `DEBBUILDER_COMPONENT`
- `DEBBUILDER_AUTH_MODE`
- `DEBBUILDER_OIDC_*`
- `DEBBUILDER_ALLOW_REAL_RUN`
- `DEBBUILDER_ALLOW_UNSAFE_BUILD_COMMAND`
- `DEBBUILDER_BUILD_TEMP_DIR`
- `DEBBUILDER_NTFY_TOKEN`

Secrets and local runtime state are stored under `data/` and are not intended for Git.

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

The initial public release is DebBuilder 0.1.0.
