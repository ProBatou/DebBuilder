# Security

Please report security issues privately before opening a public issue.

DebBuilder is designed for trusted self-hosted use. Treat its administrative
UI/API as a privileged surface: keep it on localhost or protect it with OIDC, a
trusted reverse-proxy identity header, a VPN, or another access-control layer.
Build Recipes may execute upstream code as root; command containment is not a
security sandbox.

## Sensitive data

- Do not commit `data/*.json`, run logs, workflow state, tokens or generated secrets.
- Use environment variables or the local settings page for deployment-specific values.
- Restrict recipe editing to trusted administrators. Build commands run without a shell, reject shell operators and command substitution, use a bounded workspace, and receive a controlled environment.
- Real builds and publications require explicit UI confirmation; publication also validates its package-and-version confirmation token.

## HTTP surfaces

The admin UI/API defaults to `127.0.0.1:8099`. The independently configurable
APT repository listener defaults to `127.0.0.1:8081` and intentionally serves
public files without authentication:

- `/`
- `/dists/*`
- `/pool/*`
- `/repository.gpg`
- `/install.sh`

The repository listener has no administrative routes. Private repository
signing material stays outside the public repository root.
