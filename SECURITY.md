# Security

Please report security issues privately before opening a public issue.

DebBuilder is designed for trusted self-hosted use. Treat it as an administrative tool: protect it behind OIDC, a reverse proxy, VPN, or another access-control layer when exposed outside localhost.

## Sensitive data

- Do not commit `data/*.json`, run logs, workflow state, tokens or generated secrets.
- Use environment variables or the local settings page for deployment-specific values.
- Keep `DEBBUILDER_ALLOW_REAL_RUN=0` unless you intentionally want server-side execution.
- Keep `DEBBUILDER_ALLOW_UNSAFE_BUILD_COMMAND=0` unless you fully trust every recipe author.

## Public paths

APT repository files are intentionally public:

- `/dists/*`
- `/pool/*`
- `/repository.gpg`
- `/install.sh`
