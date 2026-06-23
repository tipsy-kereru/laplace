# PRD: Parallel queue scheduler

## Status
Draft — for `/laplace:intake`.

## Context

`/laplace:run-queue` (v0.2.0+) executes approved issues **sequentially**: one at a time, halting at each `merge-wait`. `/laplace:run` (v0.3.0) isolates each issue in its own git worktree (`.harness/worktrees/<id>/`), so the main working tree is never blocked by an in-flight issue. Worktree isolation removed the structural reason the queue had to be sequential — two issues no longer fight over one working tree.

What's missing is a **scheduler** that exploits worktree isolation: run independent approved issues concurrently, serialize only when a dependency (`depends_on`) forces it, and never deadlock. The dependency graph already exists (`state._check_dependency_graph` does cycle detection at approve; `intake` parses `depends_on` from PRD `Depends on:` lines). What's missing is the runtime that consumes the graph and dispatches issues to parallel worktrees.

## Problem

- Independent issues (no `depends_on` between them) run one-at-a-time even though they touch disjoint files in disjoint worktrees. Wasted wall-clock.
- No mechanism answers: "given the approved queue + its dependency graph, what's the maximal set of issues I can run right now without blocking?"
- Human babysitting scales linearly with issue count (one `merge-wait` halt per issue in the sequential runner). Parallel collapse reduces that to one halt per dependency "wave".

## Goals

- `/laplace:run-queue --parallel` (or a new `/laplace:run-parallel`) executes the approved queue respecting the dependency graph: issues with no unmet `depends_on` start concurrently, up to a configurable concurrency cap.
- **Deadlock-free by construction**: cycle detection already rejects cycles at approve; the scheduler does a topological sort and only dispatches issues whose dependencies are all `review-passed` (the satisfaction contract from `_dependencies_satisfied`). A cycle can't reach the scheduler.
- Each parallel issue runs in its own worktree (already supported by `runner.cmd_start` since v0.3.0). No shared working tree.
- On any issue halting (`blocked`, `human-approval-required`, `merge-wait`), the scheduler does NOT cancel sibling in-flight issues; it records the halt and continues dispatching other ready issues until none remain ready. Then it halts the queue with a summary.
- The existing sequential `run-queue` stays the default (small repos, single maintainer, simpler mental model). Parallel is opt-in.
- Human gates preserved: each issue still goes PM→Dev→Review→Security; `merge-wait` still halts per-issue; the human still merges to main.

## Non-goals

- **Auto-merge to main** — still human. Parallel scheduler produces review-passed branches in worktrees; the human merges them. (An `auto-merge-branch` integration-branch policy already exists for stacking without per-issue merge; the parallel scheduler can reuse it but does not add main auto-merge.)
- **Real distributed execution** — parallelism is within one host (subprocess/worktree concurrency), not across machines. The cap is local CPU/context-budget, not a cluster.
- **Re-planning mid-run** — if a PRD changes or issues are added mid-queue, the human cancels and re-runs. Dynamic re-topology is out of scope.
- **Replacing sequential `run-queue`** — both coexist; `--parallel` opts in.
- **Conflict resolution across parallel branches** — if two issues touch the same file, both land in separate worktrees; their merge to main is the human's problem (the existing `merge-wait` / `auto-merge-branch` policies handle it). The scheduler does NOT detect cross-issue file overlap (advisory future, not v1).

---

## Task: parallel scheduler over worktree-isolated runner

### Background
The scheduler is a thin layer over `runner.cmd_start` (which creates a worktree per issue) + `state._dependencies_satisfied` (which says whether an issue's deps are terminal) + the existing queue-step run-log. It does NOT re-implement gates — each parallel issue's PM/Dev/Review/Security flow is driven externally (by the model, same as today); the scheduler's job is dispatch + readiness + join.

### Scope
**In Scope:**
- `scripts/parallel_queue.py`:
  - `cmd_parallel_start(args)` — load approved queue + tasks.json, build the dependency graph, topological-sort, dispatch ready issues up to `max_parallel` (default 2; configurable in `.harness/config.yml` `limits.max_parallel`).
  - **Readiness rule**: an issue is ready iff its `depends_on` are ALL in `review-passed` (or terminal) AND no sibling in-flight issue conflicts (v1: no file-overlap check, so only the dep rule gates readiness). `_dependencies_satisfied` already encodes this.
  - **Dispatch**: for each ready issue, call `runner.cmd_start` (creates worktree) and record it as in-flight. The actual PM/Dev/Review/Security driving is external (model-driven, same as `run-queue`); the scheduler's invocation of `cmd_start` begins issue N and returns — the model drives N to review-passed, then re-invokes the scheduler to dispatch the next wave.
  - **Wave semantics** (matches the synchronous model): the scheduler is re-invoked after each issue reaches a terminal state. Each invocation: (a) dispatches all currently-ready issues, (b) reports in-flight + halted + ready counts, (c) exits. The human/model re-invokes after the next terminal transition. This mirrors `run-queue`'s synchronous contract — no long-running background process.
  - **Halt handling**: if an issue halts (`blocked`, `human-approval-required`, `merge-wait`), record it; do NOT cancel siblings; continue dispatching other ready issues. If no issues are ready AND none in-flight → queue exhausted → halt with summary.
  - **Deadlock guarantee**: because cycles are rejected at approve and readiness requires deps terminal, the scheduler can never wait on an issue that's waiting on the current one. Document the invariant: "the scheduler dispatches only issues whose deps are already terminal; a non-terminal dep blocks dispatch, but can't create a cycle (cycles were rejected at approve)."
  - Parent parallel-run log at `.harness/state/runs/<parallel-run-id>.json` with `kind: "parallel-queue"`, `issues: [child run ids]`, `waves: [{ts, dispatched:[...], in_flight:[...], halted:[...]}]`, `outcome`.
  - `max_parallel` cap: at most N issues `cmd_start`-ed (worktrees live) at once. Default 2 (conservative; each issue is a full agent loop). Configurable.
- `.harness/config.yml` — add `max_parallel` under `limits` (default 2). `state.load_config` validates positive int.
- `skills/run-queue/SKILL.md` — document the `--parallel` flag (or a sibling skill `run-parallel`). Recommend a SEPARATE skill `skills/run-parallel/SKILL.md` + `commands/run-parallel.md` to keep the sequential contract clean.
- `commands/run-parallel.md` + `skills/run-parallel/SKILL.md` — imperative wrapper, mirrors run-queue.md.
- `scripts/parallel_queue.py selftest` — temp repo, 3 approved issues (A independent, B depends_on A, C independent). Assert: wave 1 dispatches A+C (both ready, B blocked on A); after A reaches review-passed, wave 2 dispatches B. Cycle-seeded fixture rejected at approve (characterization, already enforced). Halt-in-sibling case: A halts blocked, C still dispatched.
- `tests/test_parallel_queue_unit.py` — one test per AC.

**Out of Scope:**
- File-overlap detection across parallel branches (advisory future).
- Dynamic re-topology mid-run.
- Distributed/multi-host execution.
- Auto-merge to main.
- Replacing sequential `run-queue`.

### Acceptance Criteria
- AC-PQ-001: `/laplace:run-parallel` dispatches all approved issues whose `depends_on` are fully terminal, up to `max_parallel` concurrently. Each dispatched issue gets its own worktree via `runner.cmd_start`.
- AC-PQ-002: an issue with an unmet dependency is NOT dispatched until the dep reaches `review-passed` (or terminal). Re-invoking after the dep terminates dispatches the dependent issue.
- AC-PQ-003 (deadlock-free): a cycle seeded in `depends_on` is rejected at `/laplace:approve` (existing behavior); the scheduler never sees a cycle. The scheduler's readiness rule (deps must be terminal) cannot create a wait-cycle because terminal-ness is monotonic. Documented + a characterization test.
- AC-PQ-004: `max_parallel` cap enforced — never more than N worktrees live simultaneously. Default 2; configurable via `limits.max_parallel`.
- AC-PQ-005: when an in-flight issue halts (`blocked`, `human-approval-required`, `merge-wait`), siblings continue; the halted issue is recorded and NOT re-dispatched until the human resolves it (re-invocation skips it if still halted).
- AC-PQ-006: queue exhausted = no ready issues AND no in-flight issues → halt with summary outcome `queue-exhausted`.
- AC-PQ-007: parent parallel-run log records `kind: "parallel-queue"`, `waves` (one entry per invocation with dispatched/in_flight/halted lists), child run ids, outcome.
- AC-PQ-008: every gate inside each issue unchanged (PM/Dev/Review/Security, merge-wait, evidence capture) — the scheduler composes `runner.cmd_start`, does not re-implement gates.
- AC-PQ-009: `/laplace:status` reports an active parallel run: in-flight issues (ids + worktrees), ready count, halted count, wave number.
- AC-PQ-010: `/laplace:cancel` (or a parallel-aware variant) cancels in-flight issues cleanly — ends their child runs, removes their worktrees, releases locks, records the parallel-run position for resume.
- AC-PQ-011: characterization — sequential `run-queue` semantics unchanged; `run-parallel` is additive.
- AC-PQ-012: concurrency cap violations impossible — assert in selftest that dispatching 5 ready issues with `max_parallel=2` results in exactly 2 in-flight, 3 deferred to next wave.

### Risks
- **R-1 Worktree resource pressure**: each parallel issue = 1 worktree + 1 agent loop. `max_parallel` caps it; default 2 is conservative. Document that high caps pressure disk (worktrees) + context (the orchestrator model drives N loops). Config knob is the release valve.
- **R-2 Merge-order ambiguity**: with N review-passed branches landing, the human merges in whatever order; later merges may conflict. This is the existing `merge-wait` reality, amplified. Mitigation: the `auto-merge-branch` integration-branch policy stacks them; document that parallel + `wait-for-human-merge` = the human juggles N merges.
- **R-3 Synchronous-wave re-invocation friction**: the scheduler exits after each wave; the model must re-invoke after each terminal transition. If the model forgets, the queue stalls (not deadlocks — just waits). Mitigation: `/laplace:status` shows "N in-flight, re-invoke run-parallel to dispatch next wave" guidance. A future background-process variant is out of scope.
- **R-4 File-overlap silent conflict**: two parallel issues editing the same file both reach review-passed; their merges conflict at main. v1 does NOT detect this (documented). Advisory future: a pre-dispatch overlap check (diff file-set of ready issues, warn on intersection).

### Risk / Release Impact
- Risk Level: medium (concurrency, but each issue already isolated in its own worktree)
- Release Type: minor (0.4.0 — new command, additive)
- Security Sensitivity: medium (concurrency + worktree lifecycle + lock hygiene across parallel issues)

---

## Open questions (PM phase)

- Separate `run-parallel` command vs `run-queue --parallel` flag? Recommend separate command (cleaner skill contract; `run-queue` stays the simple sequential default).
- `max_parallel` default: 2 (conservative) vs 3? Recommend 2 for v1; users bump via config.
- Should the scheduler detect cross-issue file overlap and warn (R-4) in v1, or defer? Recommend defer — adds a diff-analysis step per dispatch; ship the core scheduler first.
- Wave re-invocation: should `run-parallel` auto-loop within one invocation (driving each dispatched issue to terminal internally) or stay one-wave-per-invocation (matches `run-queue` synchronous model)? Recommend one-wave-per-invocation for v1 (consistency with `run-queue`); auto-loop is a future enhancement that blurs the "model drives phases" contract.
