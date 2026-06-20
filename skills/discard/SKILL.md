---
name: discard
description: Remove a single draft issue from .harness/ (tasks.json, queue.json, and the .md file). Atomic and draft-only — the safety boundary. Human-only; no batch.
---

# /laplace:discard

## Intent

Remove a single mis-intaken DRAFT issue from `.harness/` so the workspace is not polluted by drafts that should never have been created. This is the only escape for a draft that should not exist short of nuking `.harness/` entirely. It is destructive but narrowly scoped: draft-only, atomic, single issue, no run-log deletion.

## When to Run

- After `/laplace:intake` produced a draft the human decides is wrong (wrong scope, duplicate, malformed).
- One issue at a time, after the human has confirmed via `/laplace:show <issue>` that this draft should not exist.
- Never for an issue that has ever been approved or run. If an issue has any run history, discard refuses it (exit 2) — the human must use the normal exception flow instead.

## What It Does

1. Invokes the state engine discard command:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/state.py discard <issue-id>
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD.

2. The state engine (in `scripts/state.py`):
   - Acquires the shared intake/draft critical-section lock (`INTAKE_LOCK_ID`). If it cannot (intake or another discard is in progress), exits 3.
   - Loads `.harness/state/tasks.json`. If the issue is missing, exits 2 (`not found`).
   - If the issue status is not `draft`, exits 2 (`only draft allowed`). No state change.
   - Defense-in-depth: if the tasks record carries a `run_id`, OR any `.harness/state/runs/*.json` log has `issue_id == this issue`, exits 2 (`run history exists`). No state change.
   - Snapshots prior `tasks.json` and `queue.json` for rollback, then atomically removes the issue from `tasks.json`, from ALL queue states (`draft`, `approved`, `in-progress`, `blocked`, `release-candidate`), and deletes `.harness/issues/<id>.md`. Atomic per file: on any write failure the JSON files are rolled back to the snapshot and the command exits 1.
   - Releases the lock in `finally`.
   - On success prints `discarded <id>: draft -> (removed)` and returns 0.

3. v1 does NOT write an audit log entry. Deletion of a draft is human-decided and not audited in this version.

## Output Format

```
Laplace result: discarded

Issue: ISSUE-NNNN
State: draft -> (removed)

Evidence:
  - tasks.json: issue removed
  - queue.json: removed from all queue states
  - .harness/issues/ISSUE-NNNN.md: deleted

Artifacts:
  - .harness/state/tasks.json (updated)
  - .harness/state/queue.json (updated)
  - .harness/issues/ISSUE-NNNN.md (deleted)

Risks:
  - destructive: the draft .md file is deleted; recovery requires git history

Next:
  /laplace:status  (or /laplace:approve <other-draft> to continue)
```

## Constraints

- MUST NOT discard a non-draft issue. The state engine rejects anything that is not currently `draft` with exit code 2. This is the safety boundary — no agent, hook, or prompt path may bypass it.
- MUST NOT discard an issue that has any run history, even if status was forced back to draft. Defense-in-depth check in `state.py` scans `.harness/state/runs/` for any log referencing the issue.
- MUST NOT delete run logs. Discard only removes the draft issue file and its tasks/queue entries. Run history is preserved.
- MUST NOT batch-discard. Each invocation accepts exactly one issue id. No globbing, no `--all-drafts`.
- MUST NOT be auto-invoked. Discard is human-only; no agent or hook may call `state.py discard` on the human's behalf without explicit instruction.
- MUST be atomic. Either all three mutations (tasks.json, queue.json, .md) complete, or the JSON state is rolled back and the command reports failure (exit 1).

## Failure Modes

- **Issue not in draft state**: `cannot discard <id>: only draft allowed (status=<state>)`. Exit code 2. No state change. Recommend `/laplace:show <id>` to inspect.
- **Issue not found**: `cannot discard <id>: not found`. Exit code 2. No state change.
- **Run history exists**: `cannot discard <id>: run history exists`. Exit code 2. No state change. The human must use the normal exception flow; do not force-delete.
- **Lock contention**: `discard lock failed for <id>: locked by pid=<pid>`. Exit code 3. Another intake or discard is in progress; retry shortly.
- **Atomic write failure**: `discard <id> failed: <error> (state rolled back)`. Exit code 1. JSON files restored to the pre-mutation snapshot; the .md file may or may not exist. Surface to the user.
