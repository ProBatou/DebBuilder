# Information architecture and capability map (#24B3)

Main navigation: Overview / Packages / Recipes / Runs / System / Settings. Packages contains managed package list/detail and a compact repository summary. Its View repository inventory button opens a read-only secondary view of the repository manager’s exact inventory. This inventory can differ from DebBuilder-managed Packages. Its fixture shows repository facts and published packages from reprepro, plus a link to the separate public APT landing. Only the public page offers installer commands and public metadata links. System Health covers repository operation, signing, metadata and last publication. System contains Health, Maintenance and Developer. Settings contains General, Repository, GitHub, Authentication, Notifications, Automation and Advanced.

| Current capability | #24B2 location |
| --- | --- |
| Recipe source, tracking/ref/version, source changes | Recipes > Advanced > Source and tracking |
| Build commands, environment, working directory, timeouts, ensure_directories, output selection | Recipes > Advanced > Build and output |
| Package metadata, runtime Depends, ELF detection/overrides, installation destination, mappings, config files, permissions, directories, account/group, maintainer scripts | Recipes > Advanced > Package and installation |
| systemd directives, environment files, resource limits | Recipes > Advanced > Service and resources |
| Automation, import/export/JSON, managed Recipe restrictions | Recipes > Advanced > Automation and data |
| App name, public URL | Settings > General |
| APT repository URL, distribution, component, architecture | Settings > Repository |
| GitHub token | Settings > GitHub |
| OIDC issuer, client ID, redirect URI, client secret | Settings > Authentication; auth mode is derived from complete OIDC configuration |
| ntfy server, topic, token, test notification | Settings > Notifications |
| Auto validate, auto publish | Settings > Automation |
| Workspace cleanup and failed workspace retention (0–1000) | Settings > Advanced, local fixture editor |
| Storage, execution history, clear history | System > Maintenance |

This inventory was checked against `static/settings.js` in the worktree. `resource_limits` is preserved in that UI's payload but is not currently exposed as a Settings control; the prototype does not invent one. The #24B3 Settings controls edit local fixtures with Save/Cancel and validation. Actual API integration remains a later, separately approved step.
