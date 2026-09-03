# Security

Please report security issues privately before opening a public issue.

DebBuilder is designed for trusted self-hosted use. Treat it as an administrative tool: protect it behind OIDC, a reverse proxy, VPN, or another access-control layer when exposed outside localhost.

## Sensitive data

- Do not commit `data/*.json`, run logs, workflow state, tokens or generated secrets.
- Use environment variables or the local settings page for deployment-specific values.
- Restrict recipe editing to trusted administrators. Build commands run without a shell, reject shell operators and command substitution, use a bounded workspace, and receive a controlled environment.
- Real builds and publications require explicit UI confirmation; publication also validates its package-and-version confirmation token.

## Public paths

APT repository files are intentionally public:

- `/dists/*`
- `/pool/*`
- `/repository.gpg`
- `/install.sh`
