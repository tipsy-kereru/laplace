---
name: laplace-status
description: Show current Laplace harness state. Read-only. Reports queue counts, active run, current state, evidence, and next safe action per SPEC-002 §Status Template.
---

# /laplace:status

## Intent

Read-only harness status per `specs/SPEC-002-laplace-claude-code-plugin.md` §Output Format and §Status Template.

## When to Run

- Any time the user wants to know where Laplace is in the issue loop.
- Before approving, running, or cancelling an issue.
- After a Stop-hook continuation to confirm loop progress.

## What It Does

1. Invokes the read-only status command:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/state.py status
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD.

2. Reads:
   - `.harness/state/tasks.json`
   - `.harness/state/queue.json`
   - `.harness/state/runs/<run-id>.json` (most recent in-progress run, if any)

3. Formats output per SPEC-002 §Status Template:

   ```
   Harness status.

   Queue:
     draft: <n>
     approved: <n>
     in-progress: <n>
     blocked: <n>
     release-candidate: <n>

   Active run:
     Run: <run-id>
     Issue: <issue-id>
     State: <state>
     Agent: <agent>
     Attempt: <n>/<limit>
     Last evidence: <command/result or artifact>

   Next action:
     <one concrete next command or approval>
   ```

   If no active run exists, `Active run:` reports `(no active run)`. The next-action recommendation picks the highest-priority safe action in this order:

   - If `approved` queue has an entry and no active run: `/laplace:run <first-approved>`.
   - Else if `draft` queue has an entry: `/laplace:approve <first-draft>`.
   - Else if a run is active: `await current run completion or /laplace:status`.
   - Else: `/laplace:intake <prd> to create draft issues`.

## Constraints

- MUST NOT modify any state (read-only).
- MUST NOT read `.env`, `.ssh/`, `.aws/`, `secrets/**`, or any path on the policy deny list.
- MUST NOT execute network commands.
- Evidence strings shown in output are subject to redaction (see `scripts/redaction.py`); raw command output is never displayed.

## Output Format

See §Status Template above. No additional formatting; the script output is the user-facing output.

## Failure Modes

- If `.harness/` is missing: print `Laplace is not initialized. Run /laplace:init first.` and exit.
- If `tasks.json` or `queue.json` is corrupt: print a sanitized parse error and recommend `/laplace:doctor`. Do not attempt recovery.
