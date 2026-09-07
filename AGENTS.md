# DebBuilder agent guidance

Read this file and [`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md)
before modifying DebBuilder.

- Treat the assigned public GitHub Issue as the engineering specification and
  the private GitHub Project as the source of current coordination state.
- Work only in the assigned branch/worktree and scope. Never reset, stash,
  overwrite, or otherwise destroy unrelated uncommitted work.
- Use one dedicated branch/worktree and one owning agent/session per active
  Issue. Parallel sessions must use isolated runtime data and ports.
- Audit/plan first when uncertainty is significant; implement in bounded
  checkpoints; validate each checkpoint; stop when review is requested.
- Reuse existing project primitives and preserve established architecture.
  Avoid opportunistic refactors and report limitations and known baseline
  failures explicitly.
- Do not assume authority to commit, push, merge, close Issues, or change
  roadmap state unless the task explicitly grants it. Do not stage changes.

Every Codex checkpoint and final report must begin with this compact identity
header, using actual Git/Project state where reliably available:

```text
WORK CONTEXT
Issue: #<number> — <title>
Branch: <branch>
Checkpoint: <checkpoint>
Worktree: <path>
Base commit: <sha>
HEAD: <sha>
Project Status: <status>
```

Never invent values; use `unknown` when they cannot be determined reliably.
Use `Issue: none` for work without a GitHub Issue. For audit-only work, report
the actual inspected branch. `Worktree` is ephemeral report context and must
not be stored in Project metadata. Dynamic values belong in the private
Project, which remains authoritative for coordination state; do not hard-code
them in repository documentation. The header identifies the actual execution
context at report time.

Follow the detailed startup, checkpoint, testing, Git, Project, and public/
private-information rules in the linked workflow document.
