---
name: run-queue
description: Execute the approved-issue queue. Iterates issues through the loop via runner.py, auto-advancing on review-passed, halting at every intra-issue gate (merge-wait, conflict, approval-required, cap).
---

# /laplace:run-queue

## Intent

Execute the approved queue: compose `runner.py` per issue, advancing to the next approved issue on `review-passed` (when merge-policy advance + dependency pre-check both succeed), halting at every intra-issue gate. The skill instructs the model; deterministic iteration, gating, the cap, the parent run log, and merge-policy routing are delegated to `scripts/queue_runner.py`, which composes `scripts/runner.py` / `scripts/state.py` / `scripts/policy.py` primitives.

## When to Run

- After multiple `/laplace:approve <issue>` calls have populated the approved queue and a human wants them run back-to-back.
- To resume after a human merge completes (the previous run halted at `merge-wait:<id>`).
- To resume after resolving a blocker (`blocked` / `human-approval-required`) on an earlier issue in the queue.

Do NOT invoke on an empty approved queue (the runner exits 0 with `noop:empty-approved-queue`), on a queue containing only `draft` issues, or while a per-issue run lock is held by an active single-issue run.

## What It Does

### Step 1: Start the queue

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/queue_runner.py start [issue-id]
```

- With no `<issue-id>`, starts at the head of the approved queue.
- With `<issue-id>`, starts at that issue (must be `approved`).
- The runner owns everything else:
  - Per-issue delegation to `runner.cmd_start` (lock acquire, branch setup, state transitions, evidence gates, fix-attempt limits).
  - Dependency pre-check before each issue (issues whose declared deps are not yet `review-passed` are deferred).
  - Lock pre-probe (skips issues whose lock is held).
  - Post-issue decision matrix (AC-QR-007): on `review-passed` + merge-policy-advance + deps-satisfied, advance; otherwise halt with the matching outcome.
  - Consecutive-issue cap (AC-QR-008, default bound from `.harness/config.yml`): halts with `max-queue-run-reached:<n>` rather than running unbounded.
  - Parent queue-run log at `.harness/state/runs/<queue-run-id>.json` with a `queue_steps` array recording each per-issue outcome (AC-QR-009).
  - Merge-policy routing: default `wait-for-human-merge` halts at `merge-wait:<id>`; opt-in `auto-merge-branch` targets only `laplace/queue-<queue-run-id>` and halts at `merge-conflict:<id>` on conflict.

The skill does not iterate, does not call `runner.py` per issue directly, and does not implement the decision matrix — `queue_runner.py` does.

## Output Format

The runner prints a parent-run result block, which the skill surfaces verbatim:

```
Queue halted: <outcome>   (or: Queue exhausted)
queue-run-id: <queue-run-id>
queue_steps:
  - <issue-id>: <per-issue outcome>
  - ...
Next: <one concrete next action>
```

## Constraints

- MUST NOT weaken any gate. All transitions, fix-attempt limits, test-evidence gates (AC-LP-008), and security checks remain inside `runner.py` primitives; the queue runner only composes them.
- MUST NOT create PRs, push, publish releases, or produce any external side effect. Same external-side-effect ban as `/laplace:run`.
- MUST NOT merge into `main` or `master`. The only legal auto-merge target is `laplace/queue-<queue-run-id>` (by construction — the integration branch is per queue-run); the default policy is `wait-for-human-merge`, which never auto-merges at all.
- MUST route every git invocation through `policy.check_command`. `queue_runner.py` already does this; the skill must not bypass it.

## Failure Modes

- **Empty approved queue**: runner exits 0 with `noop:empty-approved-queue`. Nothing to run; report and stop.
- **Lock held**: runner halts with `held-lock:<id>` (another run is active on `<id>`). Recommend `/laplace:status`.
- **Unmet dependency**: issue deferred; if no issue in the queue is runnable, runner halts with the deferral reason. Surface verbatim.
- **Merge conflict**: `merge-conflict:<id>` on `laplace/queue-<run>`. Resolve manually, then re-run.
- **Start failed / non-terminal halt**: `start-failed:<id>:<rc>` or a non-terminal defensive halt. Surface verbatim and recommend `/laplace:status`; do not retry.
- **Not a git repo**: merge-policy routing records the repo-missing state and halts; per-issue state transitions still proceed (fail-safe, same as `/laplace:run`).
