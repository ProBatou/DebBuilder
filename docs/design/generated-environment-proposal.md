# Future Issue proposal — generated runtime environment and secrets

Pocket-ID confirms a bounded product need beyond #24: install-time, write-once runtime files with operator-provided application values and generated secrets. The current Recipe v5 supports account provisioning, declared directories, source-backed `create_if_missing` mappings and `service.environment_files` references, but it does not declaratively generate a secret and persist an environment file. Maintainer shell remains the current escape hatch. This proposal is not implemented by the #24B prototype.

Proposed acceptance boundary for a separate Issue:

- Recipe declares a target absolute path, owner/group/mode, fixed operator values and generated-value specifications. The schema differentiates ordinary text from secret values; no app-specific URL or policy flag is inferred.
- `create_if_missing` is idempotent: install/upgrade never replace an existing valid file or rotate a generated secret implicitly. Define behavior for missing, file/symlink collision, permissions drift and account ordering.
- Generation uses a documented cryptographically suitable source and encoding/length contract. Define when generation occurs and how Test previews the action without producing or leaking a secret.
- Upgrade and purge semantics are explicit. Upgrade preserves existing values; removal versus purge distinguishes persistent data. Backup/restore and reinstallation do not silently invalidate application data.
- Ownership and restrictive mode are applied without unsafe path traversal or symlink following. Validation confirms service startup using the generated file where the offline environment permits it.
- Logs, Run diagnostics, inspectors, support bundles, API responses and UI previews redact secret content. Structured errors identify the failed step without echoing values.
- Tests cover first install, upgrade, reinstall, purge, missing/collision paths, account creation order, idempotency, redaction and offline validation.

This capability should be designed in the packaging/Recipe layer and reviewed independently of visual migration. #24B neither changes schema nor promises it as available.
