# DebBuilder development workflow

This document records stable development rules for contributors and coding
agents. It does not record current task assignments, checkpoint values, branch
names, base commits, session details, or other changing project state.

## Sources of truth

Keep information in the right place:

- Repository documentation: stable development and architecture rules.
- Private GitHub Project: current internal work state and coordination.
- Public GitHub Issue: professional engineering/product specification.
- Git branch/worktree: actual implementation and reviewable changes.

Chat history is useful context but is not the sole authoritative project state.

## Parallel Git workflow

The intended integration branch is `DEV/main`. Each active Issue
implementation has one dedicated branch and isolated worktree, owned by one
agent/session. Never run two independent agents against the same
implementation branch.

Before integration, update or rebase the work against the integration branch,
resolve conflicts deliberately, run relevant tests, and review the diff. Do
not automatically resolve large architectural conflicts. After integration,
remove obsolete worktrees and branches when appropriate.

Agents must not assume authority to commit, push, merge, close Issues, or
change roadmap status. A task may explicitly override this rule. Never reset,
stash, stage, or overwrite unrelated uncommitted work.

## Private Project lifecycle

Use the private Project workflow:

`Backlog` → `Next` → `In progress` → `Review` → `Done`

- **Backlog** — not currently scheduled.
- **Next** — ready to start.
- **In progress** — actively owned by an implementation or audit session.
- **Review** — agent work is finished and awaits review/integration.
- **Done** — reviewed and integrated.

The Project may use dynamic fields such as `Branch`, `Checkpoint`, and `Base
commit`. Keep their current values in the private Project, not in repository
documentation.

## Public and private information

Public Issues should contain useful engineering information: the problem,
goal, relevant architecture, acceptance criteria, dependencies, and meaningful
implementation decisions. Do not use public Issues or comments as an internal
agent diary. Keep session IDs, model/reasoning choices, temporary worktree
paths, prompt history, checkpoint chatter, and routine debugging state private
in the Project or the relevant local workflow.

Never place secrets, credentials, tokens, passwords, private host details, or
sensitive operational data in repository documents.

## Agent startup

Before changing code, an agent should:

1. Read repository agent and development guidance.
2. Read the assigned GitHub Issue.
3. Inspect the assigned branch and worktree.
4. Inspect the relevant code and tests.
5. Consult private Project metadata when available.
6. Verify dependencies and the intended base revision.
7. Confirm the assigned scope/checkpoint and work only within it.

## Checkpoints and validation

For substantial backend or architecture work, audit and plan first when
uncertainty is significant. Implement in bounded checkpoints and validate each
one. A checkpoint normally includes focused tests, relevant regression tests,
broader tests when justified, `git diff --check`, and syntax/compile checks
where relevant. Report changed files, known limitations, and any baseline
failures separately, then report `git status`. Stop when review was requested.

Avoid unrelated refactors. Reuse existing primitives, preserve the established
architecture unless a change is justified, and distinguish a new regression
from a pre-existing failure.

## Development isolation

Parallel sessions must not share mutable runtime data. Isolate the worktree,
`DEBBUILDER_DATA_DIR`, development port, and temporary test state for each
instance. Never point development tests at production data.

Python/backend changes require restarting the relevant DEV server or process
before manual validation if it would otherwise still hold old code. Static
frontend-only changes may not require a backend restart.

## Stable architecture principles

These are guiding principles, not a mandate to add new abstractions:

- Prefer standard Debian/Linux/systemd mechanisms over unnecessary custom
  infrastructure.
- Execute canonical commands with structured `argv` and no `shell=True`.
- Confine workspaces and paths; extract archives safely.
- Use atomic durable state writes where required.
- Fail closed when destructive ownership cannot be proven.
- Do not claim security guarantees stronger than the implementation provides.
- Keep Recipe and UI concepts simple and readable; avoid unnecessary
  abstractions.

On supported Linux/systemd hosts, systemd transient services with cgroup v2
are the preferred strong command-containment mechanism. Process identity
containment remains defense-in-depth or fallback. Resource/process containment
is not a security sandbox: root-executed upstream code must not be described
as securely sandboxed.

Because containment uses per-command systemd/cgroup units, future resource
controls should reuse those mechanisms rather than create a parallel resource
manager. Possible controls include memory, task/process count, CPU, and I/O;
disk-capacity quotas are a separate concern. This is future guidance, not an
implementation requirement for the current task.

Persisted formats must evolve deliberately. Version durable schemas, migrate
supported older formats through explicit sequential/idempotent migrations, and
fail clearly on unsupported future or corrupt formats. Users should not need
to delete and recreate data merely because internal formats evolved.

## Prompt convention for Codex work

When ChatGPT prepares a ready-to-paste Codex implementation or audit prompt
for Baptiste, it should state outside the prompt: the recommended model,
reasoning level, whether to use the same or a new session, and a short reason.
This is guidance, not a rigid technical requirement; choose the cheapest model
appropriate to the risk and complexity. As a general guide:

- **Luna** — tiny, mechanical, administrative, or simple documentation work.
- **Terra** — localized implementation, UI work, or moderate isolated changes.
- **Sol** — serious backend, multi-file, runtime, architecture, or complex
  debugging work.
- **Astra** — exceptional, very difficult investigation only.

For long-running work, preserving a useful existing session may be preferable
to starting a new one. A complete ready-to-paste prompt uses one outer Markdown
fence; nested fences are allowed only when safely contained by that outer fence.
