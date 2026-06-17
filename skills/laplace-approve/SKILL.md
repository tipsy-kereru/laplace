---
name: laplace-approve
description: Move a draft issue into the approved execution queue. Explicit human approval gate — only this command transitions draft to approved. Records approval in approvals.jsonl.
---

# /laplace:approve

## Intent

Transition a single draft issue from `draft` to `approved`, moving it into the execution queue. This is the explicit human approval gate required by SPEC-002 §State Machine and AC-LP-006: no other command, agent, or prompt path moves an issue from `draft` to `approved`. Intake, PM, dev, review, and security agents are all incapable of approving an issue into the queue.

## When to Run

- After `/laplace:intake` has produced draft issues and a human has reviewed one via `/laplace:show <issue>`.
- When the human decides a draft issue is ready for queue execution.
- One issue at a time, after explicit human review of scope, acceptance criteria, risk, and source. Batch approval is not supported without per-issue confirmation.

## What It Does

1. Invokes the state engine approve command:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/state.py approve <issue-id>
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD, and `--user <name>` to attribute the approval.

2. The state engine (in `scripts/state.py`):
   - Loads `.harness/state/tasks.json` and reads the issue's current status.
   - Validates the transition via `validate_transition(current, "approved")`. Only `draft -> approved` is accepted.
   - On success: atomically writes the new status to `tasks.json`, updates `queue.json` (removes from `draft`, appends to `approved`), and appends a redacted line to `.harness/state/approvals.jsonl` of the form:

     ```json
     {"ts": <epoch>, "issue_id": "ISSUE-NNNN", "action": "approve", "user": "<name>"}
     ```

   - All persisted fields pass through `redaction.py`.

3. The approve command does NOT acquire a long-lived run lock. It performs a single atomic state change. The run lock is acquired later by `/laplace:run` via `runner.py start`.

## Output Format

Per SPEC-002 §Output Format (Result Template):

```
Laplace result: approved

Issue: ISSUE-0001
State: draft -> approved

Evidence:
  - approval recorded in .harness/state/approvals.jsonl
  - queue.json: removed from draft, appended to approved

Artifacts:
  - .harness/state/approvals.jsonl (appended)
  - .harness/state/tasks.json (updated)
  - .harness/state/queue.json (updated)

Risks:
  - none (no source files modified; no branch created; no run started)

Next:
  /laplace:run ISSUE-0001  (or /laplace:approve ISSUE-0002 for the next draft)
```

## Constraints

- MUST NOT auto-approve. Every approval requires an explicit human `/laplace:approve <issue>` invocation. No agent, hook, or script may call `state.py approve` on the human's behalf without explicit instruction.
- MUST NOT approve non-draft issues. The state engine rejects any transition that is not `draft -> approved`. An issue already in `approved`, `pm-review`, `in-progress`, etc. cannot be re-approved without first returning to `draft` via the exception flow (`blocked -> human-resolution -> draft`).
- MUST NOT batch-approve without explicit per-issue confirmation. Each `/laplace:approve` call accepts exactly one issue id.
- MUST record `{ts, issue_id, action: approve}` in `.harness/state/approvals.jsonl` for auditability. This is done by `state.py approve`; the skill does not write the file directly.
- MUST NOT modify the issue file content. Approval only changes state metadata.
- MUST NOT create branches, install dependencies, push, or produce any external side effect.

## Failure Modes

- **Issue not found**: `state.py approve` prints `cannot approve <id>: unknown source state: <state>` (or equivalent) when the issue id is not in `tasks.json`. Exit code 2. No state change.
- **Issue not in draft state**: e.g. `cannot approve ISSUE-0001: invalid transition: pm-review -> approved`. Exit code 2. No state change. Recommend `/laplace:show <id>` to inspect current state.
- **`.harness/` not initialized**: `tasks.json` is missing or unreadable. Exit code 1. Recommend `/laplace:init`.
- **Approvals log unwritable**: rare filesystem error. Exit code 1. No state change (the atomic write to `tasks.json` and `queue.json` happens before the approvals append in `state.cmd_approve`; if the append fails after the state change, the approval is still reflected in `tasks.json` but may be missing from the audit log — surface this to the user).
