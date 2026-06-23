---
name: run-parallel
description: Execute approved issues in parallel waves over worktree-isolated runner. Dispatches ready issues up to max_parallel, re-invoked after each terminal transition. Deadlock-free by construction.
---

# /laplace:run-parallel

## Intent

Dispatch the approved queue in parallel waves: on each invocation, start every approved issue whose `depends_on` are fully terminal, up to `max_parallel` concurrently (default 2). Each dispatched issue runs in its own worktree (worktree isolation is provided by `runner.cmd_start`). The skill instructs the model; deterministic wave dispatch, the concurrency cap, the halted set, the parent parallel-run log, and the deadlock-free readiness rule are delegated to `scripts/parallel_queue.py`, which composes `scripts/runner.py` / `scripts/state.py` / `scripts/policy.py` primitives.

This is the parallel sibling of `/laplace:run-queue`. The sequential runner stays the default; parallel is opt-in.

## When to Run

- Multiple approved issues are independent (no `depends_on` between them) and you want to collapse wall-clock by running them concurrently.
- After a batch of `/laplace:approve <issue>` calls has populated the approved queue with a mix of independent and dep-ordered issues.
- After an in-flight parallel issue reaches a terminal state (`review-passed`, `blocked`, `human-approval-required`, `cancelled`), to dispatch the next wave.

Do NOT invoke on an empty approved queue (the scheduler exits 0 with `queue-exhausted`), or to replace per-issue driving — each dispatched issue still goes PM → Dev → Review → Security driven by the model, same as `/laplace:run`.

## What It Does

### Step 1: Dispatch one wave

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/parallel_queue.py start
```

The scheduler owns everything else:
- Compute the ready set: approved issues not already in-flight, not in the halted set, with `state._dependencies_satisfied` == True (deps `review-passed` or terminal).
- Compute free slots: `slots = max(0, max_parallel - len(in_flight))`. Dispatch `ready[:slots]` via `runner.cmd_start` (one worktree per issue).
- Record a wave entry `{ts, dispatched, in_flight, halted, ready_count}` in the parent parallel-run log.
- Decide the wave outcome and exit. The model re-invokes after the next terminal transition.

### Wave model (synchronous, one dispatch per invocation)

The scheduler is re-invoked after each issue reaches a terminal state. There is no long-running background process. This mirrors `/laplace:run-queue`'s synchronous contract: the model drives each in-flight issue through its phases and re-invokes `run-parallel` to dispatch the next wave.

### Deadlock-free invariant

The scheduler cannot deadlock, by construction:

1. **Cycles are rejected at approve.** `/laplace:approve` runs `state._check_dependency_graph`, which does DFS cycle detection over the `depends_on` map. A cyclic `depends_on` is rejected with exit code 2 before the issue ever enters the approved queue. The scheduler never sees a cycle.
2. **Readiness requires deps terminal.** An issue is dispatched only when `state._dependencies_satisfied` returns True — every dep is `review-passed` or terminal. The scheduler never waits on a non-terminal dep.
3. **Terminal-ness is monotonic.** Once an issue reaches a terminal state (`review-passed`, `blocked`, `cancelled`, `human-approval-required`, etc.) it never leaves it (the state machine has no outgoing edges from terminal states). So a dep that is "not yet terminal" can only transition to "terminal"; it can never become "waiting on me".

Therefore the scheduler can never wait on an issue that is waiting on the current one — the wait graph is a DAG (cycles were rejected), and dispatch only proceeds forward along topological order.

### Halt isolation

If `runner.cmd_start` returns `EXIT_BRANCH_STALE` (branch behind base), the issue is added to the parent log's `halted` set and the scheduler continues dispatching other ready issues. The halted set persists in the parent log; on re-invocation the scheduler skips any issue still in `halted`. The human resolves a halted issue (rebase or delete the branch) and removes it from `halted` (or re-approves) to re-enable dispatch.

### Concurrency cap

`slots = max(0, max_parallel - len(in_flight))` — by construction the number of live worktrees can never exceed `max_parallel`. Default 2 (conservative; each issue is a full agent loop). Configurable via `.harness/config.yml` `limits.max_parallel`.

## Output Format

The scheduler prints a wave summary:

```
parallel: wave dispatched (N started), M in-flight, R ready
```

Or on exhaustion:

```
parallel: exhausted (no ready, no in-flight)
```

The skill surfaces this verbatim and maps it to exactly one next action (see the command wrapper).

## Constraints

- MUST NOT weaken any gate. All transitions, fix-attempt limits, test-evidence gates (AC-LP-008), security checks, and worktree lifecycle remain inside `runner.py` / `state.py` primitives; the scheduler only composes them.
- MUST NOT exceed `max_parallel`. The cap is enforced structurally via `slots = max_parallel - len(in_flight)`.
- MUST NOT cancel siblings when one issue halts. Halt isolation is the contract (AC-PQ-005); only the halted issue is recorded.
- MUST NOT create PRs, push, publish releases, auto-merge to main, or produce any external side effect. Same external-side-effect ban as `/laplace:run` and `/laplace:run-queue`.
- MUST NOT re-implement readiness. `state._dependencies_satisfied` is the single source of truth for the dep gate.

## Failure Modes

- **Empty approved queue**: scheduler exits 0 with `queue-exhausted`. Nothing to run; report and stop.
- **All deps unmet**: ready set is empty; if nothing is in-flight, `queue-exhausted`; if issues are in-flight, `wave-dispatched:waiting`.
- **Branch stale**: `EXIT_BRANCH_STALE` on dispatch adds the issue to `halted`; siblings continue. Recommend `/laplace:status` and human resolution (rebase or delete branch).
- **Start failed**: `start-failed:<id>:<rc>` — dispatching `<id>` failed with a non-stale error. The wave halts immediately. Surface verbatim and recommend `/laplace:status`; do not retry.
- **Lock held**: if an issue's run lock is already held (e.g. a stray single-issue run), `runner.cmd_start` returns `EXIT_LOCK_HELD` (3); the wave treats it as `start-failed`. Resolve the stray lock first.
- **Stalled queue (not deadlocked)**: if the model forgets to re-invoke after a terminal transition, the queue stalls (waits, never deadlocks). `/laplace:status` shows the in-flight set and the re-invoke guidance.
