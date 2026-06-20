---
name: cancel
description: Stop an active Laplace loop or a resumable queue run safely. Single-issue path ends the active run with outcome=cancelled, sets issue state to cancelled, releases the lock, appends run history. Queue-scope path rewrites the resumable merge-* parent log outcome to cancelled:<id> so it drops from the resumable set. Does NOT delete branches or artifacts. Does NOT push.
---

# /laplace:cancel [issue-id]

## Intent

Safely stop an active loop per SPEC-002 §State Machine exception flow and
§Loop Limits terminal states. For queue runs (ISSUE-0008), cancel clears
the resumable merge-wait steady state so `/laplace:status` no longer
advertises a "Queue run:" block.

## When to Run

- A single-issue dev/review loop needs to stop early (user abort, scope
  change, wrong issue).
- A queue run is halted on a `merge-*` outcome (waiting on a human merge)
  and the user wants to clear the resumable steady state rather than
  resume it.
- `/laplace:status` shows a "Queue run:" block the user wants gone.

## What It Does

Backed by `scripts/cancel.py` (`cmd_cancel(args)`). stdlib-only, reuses
`runner.cmd_end` and `state` helpers — no duplicated lock logic.

### Detection priority (AC-QR-020-cancel-detect)

1. **Issue arg provided** (`/laplace:cancel <issue>`) → single-issue path
   on that issue.
2. **No arg + active in-progress single-issue run** in `tasks.json` →
   single-issue path on the active issue.
3. **No arg + no active single run + resumable queue run** (parent log
   with `outcome` startswith `merge-`) → queue-scope path.
4. **Neither** → print `nothing to cancel`, exit 0.

When both an active single run AND a resumable queue run exist without an
arg, the single-issue run wins (queue cancel is the fallback path). This
matches the pre-existing "cancel the active run" default.

### Single-issue path

1. Resolve the issue's active run from `tasks.json` (issue with
   `status == in-progress` and a `run_id`).
2. Call `runner.cmd_end` with `outcome="cancelled"` — this releases the
   issue lock and finalizes the child run log (`ended_at` + `outcome`).
3. Set the issue state to `cancelled` via `state._set_issue_state`.
   `in-progress → cancelled` is not a legal state-machine transition (the
   legal path is `in-progress → blocked → human-resolution → cancelled`),
   so the state is set directly: cancel is a user-initiated terminal
   exception per SPEC-002 §State Machine exception flow.
4. Append a `cancel: <run-id> -> cancelled` line to the issue's
   `## Run History` section via `runner._append_run_history_to_issue`.

### Queue-scope path (ISSUE-0008)

1. Find the resumable queue run via `state._find_resumable_queue_run`
   (parent log with `kind == "queue"` and `outcome` startswith
   `merge-`).
2. Rewrite the parent log: change `outcome` from
   `merge-<reason>:<id>` to `cancelled:<id>` (preserving the `:<id>`
   suffix so the merge-waited issue stays identifiable), bump
   `ended_at`. Atomic write via `state._atomic_write_json`. Preserves
   `queue_steps` and `issues`.
3. The rewrite is sufficient to clear resumability because
   `_find_resumable_queue_run` filters on `outcome.startswith("merge-")`.
   `cancelled:...` does not match, so the log drops out of the resumable
   set and `/laplace:status` no longer renders a "Queue run:" block.
4. No lock release is needed: queue runs hold no lock at merge-wait (the
   child run already ended and released its lock).

## Resume After Cancel

Resume is natural re-invocation. After a queue cancel,
`/laplace:run-queue` starts a fresh queue from the current approved
head. No position-record code is added — `review-passed` issues
auto-leave the approved queue, so the next run picks the next approved
issue. The cancelled parent log remains on disk for audit but is no
longer resumable.

## Constraints

- **No deletion**: MUST NOT delete branches, patches, run logs, or any
  other artifacts. Cancel preserves state for audit.
- **No push**: MUST NOT push, force-push, or modify remote state.
- **No gate weakening**: MUST NOT weaken state-machine gates or bypass
  `policy.check_command`. Cancel uses the direct-state-write exception
  for the terminal `cancelled` state only.
- **No integration-contract changes**: MUST NOT modify
  `state._find_resumable_queue_run` or `state._format_status` — the
  `outcome` prefix is the integration contract.
- **Stdlib only**: no third-party imports in `scripts/cancel.py`.

## Output Format

### Single-issue cancel

```
Cancelled single-issue run.
  Issue: <issue-id>
  Run: <run-id>
  State: cancelled
  Lock: released

Artifacts:
  - .harness/state/runs/<run-id>.json

Next:
  /laplace:status
```

### Queue-scope cancel

```
Cancelled queue run (was merge-waiting).
  Queue run: <queue-run-id>
  Merge-waited issue: <issue-id>
  Outcome: cancelled:<issue-id>

Next:
  /laplace:status  (Queue run: block is gone)
  /laplace:run-queue  (starts fresh from approved head)
```

### Nothing to cancel

```
nothing to cancel
```
(exit 0)

## Failure Modes

- **Issue arg + no active run**: stderr `no active run for <issue>`,
  exit 1.
- **Active run missing its `run_id`**: stderr message, exit 1. Indicates
  a corrupt run log; user should inspect `.harness/state/runs/`.
- **Queue log vanished between detection and rewrite**: stderr message,
  exit 1. Should not happen in normal use; indicates a concurrent
  writer.
- **`runner.cmd_end` failure**: cancel aborts with the runner's exit
  code; partial state is limited to the runner's own semantics (the
  issue state is only set after `cmd_end` succeeds).
