# PRD: Queue Runner — merge-policy gate unreachable in production

## Status
Draft — discovered during live smoke test of `/laplace:run-queue` on 0.2.1.

## Context

The queue runner ships two merge policies (`wait-for-human-merge` default, `auto-merge-branch`) intended to gate advancement: after issue N reaches `review-passed`, the runner should verify N's merge state before starting N+1. This prevents N+1 from proceeding while N's code is unmerged.

**The gate is unreachable in production.** It only fires in the `queue_runner.py selftest` `issue_driver` mode. In the real externally-driven flow, the gate is bypassed and the queue silently advances past unmerged issues.

## Problem — reproduction

1. `/laplace:run-queue` → `queue_runner.start` begins ISSUE-0001 (transitions `approved → pm-review`), then returns. The run is synchronous; ISSUE-0001 is non-terminal at return, so the queue run finalizes with outcome `non-terminal:pm-review:started`.
2. The model (driving the skill) runs ISSUE-0001 through PM → Dev → Review externally, reaching `review-passed`. `review-passed` is terminal; `_set_issue_state` removes ISSUE-0001 from the `approved` queue.
3. The model re-invokes `/laplace:run-queue` to continue. `queue_runner.start` picks the head of the `approved` queue — which is now ISSUE-0002. ISSUE-0001 is never re-examined; `_handle_merge_policy` is never called for it.

Net effect: ISSUE-0002 starts regardless of whether ISSUE-0001's branch was merged. The `wait-for-human-merge` pause never happens; `auto-merge-branch` never executes its merge. Both policies' advancement gates are dead code in the production flow.

## Why the selftest did not catch this

`queue_runner._run_queue` accepts an internal `issue_driver` callback that drives an issue through all phases **synchronously within the queue run**. With `issue_driver`, the runner observes ISSUE-0001 reach `review-passed` inside its own loop and runs `_handle_merge_policy` immediately — the gate fires correctly. The selftest (`test_queue_halt_merge_wait_then_resume_advances`, `test_two_issue_queue_mid_queue_gate_halt`) all use `issue_driver`. Production has no `issue_driver` (the model drives phases externally between `queue_runner.start` invocations), so the gate is never reached.

## Goals

- Make `_handle_merge_policy` reachable in the production (externally-driven) flow.
- `wait-for-human-merge`: after an issue reaches `review-passed` and its branch is NOT merged into base, `/laplace:run-queue` halts with `merge-wait:<id>` instead of advancing to the next issue.
- `auto-merge-branch`: on the same boundary, perform the integration-branch merge (or halt `merge-conflict`), instead of skipping.
- Existing selftest behavior (with `issue_driver`) unchanged — characterization guard.

## Non-goals

- Changing the synchronous model (queue_runner stays one-invocation-per-step).
- Adding a live/background queue process.
- `stack-branches` policy (still deferred).

## Proposed approach (for intake to refine)

Add a `queue_runner.py advance` (or fold into `start`) step that, before starting the next approved issue, inspects the **most recent review-passed issue not yet accounted for** and applies `_handle_merge_policy`:

- Option A — `start` pre-check: at the top of `_run_queue`, find the newest issue in `review-passed` that has no `queue_step` entry recording its merge resolution. If its branch is unmerged (under the configured policy), halt `merge-wait:<id>`. Only if resolved, proceed to the next approved issue.
- Option B — explicit `advance` subcommand: the skill calls `queue_runner.py advance` after completing an issue; `advance` runs the merge-policy check and either halts or starts the next issue. `start` becomes "begin a new queue run"; `advance` becomes "continue after an issue completed".

Recommend Option A (single command, less skill complexity) unless the PM phase argues B is cleaner for the skill contract.

## Acceptance criteria

- AC-MG-001: in the production flow (no `issue_driver`, model drives phases externally between `queue_runner.start` invocations), after ISSUE-N reaches `review-passed` with an unmerged branch and `merge_policy=wait-for-human-merge`, the next `queue_runner.start` invocation halts with `outcome=merge-wait:ISSUE-N` and does NOT start ISSUE-N+1.
- AC-MG-002: after the human merges ISSUE-N's branch into base, the next `queue_runner.start` advances to ISSUE-N+1 (records a `queue_step` for the N→N+1 transition).
- AC-MG-003: with `merge_policy=auto-merge-branch`, the same boundary triggers the integration-branch merge (or `merge-conflict` halt) instead of skipping.
- AC-MG-004: the integration test `tests/test_queue_integration.py` (or a new live-flow test) reproduces the production flow (drive issue externally, re-invoke `start`, assert halt) — NOT using `issue_driver`.
- AC-MG-005: existing selftest `issue_driver` cases unchanged (characterization).
- AC-MG-006: `/laplace:status` "resumable queue run" block correctly surfaces the `merge-wait` halt from AC-MG-001.

## Risks

- **R-1 Option A ambiguity**: "newest review-passed issue not yet accounted for" requires a marker on the parent log (which issues have been merge-resolved). The parent log's `queue_steps` already records advances; a review-passed-but-not-advanced issue is the one to check. Define precisely.
- **R-2 Multiple completed issues**: if the model completes several issues before re-invoking (unlikely in the synchronous skill flow but possible if interrupted), `start` must check the earliest unresolved one first (ordered).
- **R-3 Empty-branch false-merged**: a dev phase that committed nothing produces a branch that is trivially an ancestor of base → `merge-base --is-ancestor` returns 0 (merged). The ISSUE-0012 dev-auto-commit codification should prevent empty dev phases, but the gate should treat "branch HEAD == base HEAD" as a distinct state (record `merge-skipped:empty-branch`, advance) rather than silently advancing.

## Risk / Release Impact

- Risk Level: medium (touches queue advancement, the core path)
- Release Type: patch (bug fix — gate was always meant to work)
- Security Sensitivity: medium (the gate is a safety boundary preventing unmerged-code cascades; making it reachable restores intended behavior)
