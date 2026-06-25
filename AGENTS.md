# AGENTS.md (Codex)

> Laplace runs on Codex at **full hook parity** with Claude Code.
> Codex loads `hooks/hooks.json`, sets `CLAUDE_PLUGIN_ROOT`, and
> dispatches the same lifecycle events (PreToolUse, PostToolUse, Stop,
> SessionStart, UserPromptSubmit). The deny layer, evidence gates, and
> stop-loop all enforce. This file carries the procedure the model
> should follow on top of that enforcement.

## What Laplace is

Laplace is a local AI engineering loop harness. It converts PRDs or
stories into local issues, routes each issue through scoped phases (PM,
Dev, Review, Security), records evidence at every step, and stops at
explicit human approval gates before irreversible actions.

The bet: the model is often capable enough; the **procedure** is what
usually goes missing. Laplace makes the procedure explicit.

## The loop (per issue)

```
intake → draft → approved → pm-review → ready-for-dev → in-progress
       → review → security-review → review-passed → release-candidate
       → done
```

- **draft → approved**: human gate. Only `/laplace:approve` performs this.
- **review-passed**: requires test evidence in the run log.
- **release-candidate → done**: human gate (push, PR, publish).

On Codex (no hooks): the model MUST self-enforce these stops. Do not
auto-transition through a gate because the harness is not present to
block you. Surface the gate to the human and wait.

## Procedure discipline (follow on every task)

1. **Context before decomposition.** Read the issue file under
   `.harness/issues/<ISSUE-NNNN>.md` before proposing work. State the
   acceptance criteria back in your first reply.
2. **Local issue state before execution.** The issue's current state in
   `.harness/state/tasks.json` decides what work is allowed. Do not
   start dev work on an issue that is not `ready-for-dev` or
   `in-progress`.
3. **Scoped changes.** Touch only the files the issue names. Drive-by
   refactors and adjacent cleanups are out of scope.
4. **Evidence before claim.** "Done" means a run-log entry under
   `.harness/state/runs/<run-id>.json` with captured proof (test output,
   diff, decision). Do not declare a phase complete without writing
   evidence.
5. **Stop, don't guess.** Ambiguity, blockers, and approval-required
   categories halt the loop. Surface them; do not pick an interpretation
   and proceed.
6. **Ask before irreversible.** Credentials, production, dependencies,
   network, release: stop at the gate and ask. Even if you *could* run
   the command, the human decides.

## Approval gates (enforced on Codex)

Stop and surface to the human before:

- `git push`, `gh pr create`, `npm publish` — external publish.
- `pip install`, `npm install`, `claude mcp add` — dependency / tool
  surface change.
- Credential files (`.aws/`, `.ssh/`, env secrets).
- Destructive operations (`rm -rf`, force-push, `git reset --hard` on
  shared branches).
- Release transitions (`release-candidate → done`).

These are enforced by `scripts/policy.py` via the PreToolUse hook, which
fires identically on Codex. The deny layer (`rm -rf /`, `curl|sh`,
`sudo`, cloud CLIs) blocks outright; the approval layer halts the loop
until a human confirms (or freerange suppresses it).

## Slash commands

Laplace ships slash commands under `commands/`. On Codex they surface as
skills (invoke with `@`). The primary ones:

- `@laplace:intake <prd.md>` — PRD → draft issues.
- `@laplace:approve <ISSUE>` — draft → approved (human gate).
- `@laplace:run <ISSUE>` — one issue through the loop.
- `@laplace:run-queue` — approved backlog as a queue.
- `@laplace:status` — current harness state.
- `@laplace:freerange <on|off|status>` — approval-gate override
  (Claude Code only on Codex — the override has no hooks to suppress).

The Python scripts under `scripts/` (state machine, runner, policy,
cost-watcher, motivations, freerange) are the canonical logic. They are
invoked identically on Codex (via the lifecycle hooks) and directly via
Bash (`python3 scripts/state.py ...`) to read or transition state.

## State on disk

```
.harness/
├── issues/ISSUE-NNNN.md         # the work
├── state/tasks.json             # issue → status
├── state/runs/<run-id>.json     # per-run log: transitions, evidence
├── state/approvals.jsonl        # approval audit
├── config.yml                   # limits + policy
└── routing-rules.yml            # per-type phase routing
```

Read state before acting. Write evidence after acting.

## When the harness is not enough

If you find the model routinely skipping a gate that the procedure
requires despite the hooks, file an issue — the hooks are designed to
block the skip deterministically on both Claude Code and Codex.

See `specs/SPEC-002-laplace-claude-code-plugin.md` for the canonical
design and `README.md` for install paths.
