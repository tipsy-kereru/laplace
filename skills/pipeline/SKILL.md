---
name: pipeline
description: End-to-end checkpoint pipeline over intake/verify/approve/parallel/release. Composes 5 existing commands into one phase machine; halts at every gate; re-invoke to resume from the recorded phase.
---

# /laplace:pipeline <prd>

## Intent

Drive a PRD all the way from intake to release as a single checkpoint pipeline. The pipeline is a thin state machine that composes the 5 existing Laplace commands (`/laplace:intake`, `/laplace:verify`, `/laplace:approve`, `/laplace:run-parallel`, `/laplace:release`) in their fixed order. It removes the keystroke choreography between gates; it does NOT remove any gate.

Every gate halts with a specific "what I need from you" message and records the current phase in a pipeline-run log. Re-invoking `/laplace:pipeline --resume` (or `/laplace:pipeline <prd>` again) continues from the next phase.

## When to Run

- You have a PRD and want to drive it end-to-end instead of chaining the 5 commands manually.
- After each gate the human resolved (approved drafts, merged an issue, etc.), to resume from the recorded phase.
- When `/laplace:status` shows an active `Pipeline:` block and you want to advance it.

Do NOT invoke:
- To skip or weaken any gate. The pipeline composes the existing commands; every check inside them still fires.
- Concurrently for multiple PRDs. v1 supports one active pipeline per harness (single-project). Running `/laplace:pipeline <other-prd>` while another is active refuses with `active pipeline for <other>; cancel it first or use --resume`.
- As a replacement for the individual commands. Power users still chain them manually; the pipeline is the convenience path.

## What It Does

### Phase machine

```
intake -> verify -> approve-gate -> parallel -> release-gate -> done
```

- **intake** — calls `intake.cmd_intake(prd_path, target=target)`. On non-zero rc → halt `intake-failed`.
- **verify** — calls `verify.cmd_verify(Namespace(prd_path=prd, target=target))`. rc 0 (PASS) or 2 (usage) → proceed; rc 1 (FAIL) → halt `verify-failed` unless `--force-verify`.
- **approve-gate** — human approval gate.
  - Default: halt once, surfacing the verify report + draft list + per-issue `issue=risk`. On resume: batch-approve all remaining drafts via `state.cmd_approve`, proceed to parallel.
  - With `--auto-approve-low-risk`: approve risk.level==low drafts inline. If any medium+/high draft remains → halt surfacing those. (On resume the dispatcher re-runs the gate so the risk filter fires again.)
- **parallel** — delegates to `parallel_queue.cmd_parallel_start` wave semantics (one wave per invocation). Maps the active parallel-run outcome to a pipeline sub-state:
  - `None` / `wave-dispatched` / `wave-dispatched:waiting` → halt `parallel:wave-dispatched:waiting`.
  - `merge-<reason>:<id>` → halt `parallel:merge-wait:<id>`.
  - `cancel-failed:<id>` → halt `parallel:cancel-failed:<id>`.
  - `start-failed:<id>:<rc>` → halt `parallel-blocked:<id>`.
  - `queue-exhausted` → proceed to release-gate.
  - Any issue in `blocked` / `human-approval-required` → halt `parallel-blocked:<id>`.
- **release-gate** — human release gate.
  - Default: halt, suggesting `/laplace:release <X.Y.Z>`.
  - With `--release <X.Y.Z>` AND parallel reached queue-exhausted AND no halted issues: call `release.cmd_release(version=<ver>, target=target, force=False)`. The release command's 8-check gate + Option A push still fire unchanged. On rc != 0 → halt `release-failed`.
- **done** — finalize the pipeline log with `outcome: released`.

### Pipeline-run log

`.harness/state/runs/<pipeline-run-id>.json` with `kind: "pipeline"`:

```json
{
  "run_id": "...",
  "kind": "pipeline",
  "prd": "<realpath>",
  "started_at": <ts>,
  "ended_at": null | <ts>,
  "outcome": null | "released" | "cancelled" | "verify-failed" | ...,
  "phase": "intake" | "verify" | "approve-gate" | "parallel" | "release-gate" | "done",
  "phase_history": [{"ts": ..., "phase": "...", "result": "..."}],
  "max_parallel": <int>,
  "auto_approve_low_risk": <bool>,
  "release_version": null | "<X.Y.Z>",
  "force_verify": <bool>
}
```

`state._find_active_pipeline_run(target)` returns the most-recent non-finalized pipeline log (kind=="pipeline" AND outcome is None).

### Resume

Re-invocation reads the most-recent active pipeline log:
- `--resume` explicit, OR
- same PRD path (realpath match) with no `--resume` flag (implicit resume).
- A different PRD while another is active → refuse (`active pipeline for <other>; cancel it first or use --resume`, R-3).

### State drift (R-5)

The dispatcher re-reads disk at each phase entry. The pipeline log's phase is a hint; disk (tasks.json / queue.json / run logs) is truth. If the human `/laplace:approve`s a draft mid-pipeline or `/laplace:discard`s one, the next phase sees the new state.

## Output Format

The pipeline prints one halt block per gate, with the sub-state, the current phase, and a single `Next:` action:

```
Pipeline halt: approve-gate:ISSUE-0001=low,ISSUE-0002=medium
  Phase: approve-gate
  Drafts (issue=risk): ISSUE-0001=low,ISSUE-0002=medium
  Next: review the verify report above, then re-run /laplace:pipeline --resume to batch-approve all drafts.
```

On completion:

```
Pipeline complete.
  Run: <pipeline-run-id>
```

## Constraints

- MUST NOT remove or weaken any gate. Every check inside intake/verify/approve/parallel/release still fires unchanged. The pipeline only composes their `cmd_*` entry points.
- MUST NOT re-implement any composed command's logic. The pipeline imports `intake`, `verify`, `state`, `parallel_queue`, `release` and calls their `cmd_*` functions directly — no subprocess, no re-implementation.
- MUST halt at every gate (approve-gate, verify-failed, merge-wait, release-gate). Resume continues from the recorded phase; no silent skip.
- MUST re-read disk at each phase entry (R-5). The recorded phase is a hint.
- `--auto-approve-low-risk` MUST only approve risk.level==low drafts. Medium+ halts. Default OFF.
- `--force-verify` is the only verify-FAIL escape hatch, and is documented as such.
- stdlib only. No subprocess to the composed commands.
- Selftest MUST NOT do real git push (release.cmd_release is stubbed for the `--release` case).

## Failure Modes

- **intake-failed** — intake returned non-zero (PRD not found, parse error, lock contention). Fix the cause then `/laplace:pipeline --resume`.
- **verify-failed** — verify reported FAIL. The pipeline blocks the approve-gate until verify passes or the human overrides with `--force-verify` (AC-PL-012).
- **verify-usage** — verify returned rc 2 (usage error, e.g. missing PRD path or uninitialized `.harness/`). Inspect the verify message.
- **approve-gate** — default human gate. Review the verify report, read the per-issue risk table, then `/laplace:pipeline --resume`.
- **approve-gate:<ids>** (with `--auto-approve-low-risk`) — low-risk drafts auto-approved; the listed medium+/high drafts need manual `/laplace:approve <id>`.
- **parallel:wave-dispatched:waiting** — issues are in-flight; nothing else ready. Drive each to a terminal state, then `/laplace:pipeline --resume`.
- **parallel:merge-wait:<id>** — issue `<id>` is waiting on a human merge. Merge it (or `/laplace:cancel <id>`), then resume.
- **parallel:cancel-failed:<id>** — a stranded child run. `/laplace:cancel <id>`, then resume.
- **parallel-blocked:<id>** — issue `<id>` is blocked, human-approval-required, or hit start-failed. Resolve it, then resume.
- **release-gate** — default halt before release. Either `/laplace:release <X.Y.Z>` separately, or `/laplace:pipeline --release <X.Y.Z> --resume`.
- **release-failed** — `release.cmd_release` halted on one of its 8 checks (format/tests/sync/tree/tag/remote/approved-queue etc.). Resolve the failing check, then resume.
- **Resume ambiguity (R-3)** — `/laplace:pipeline <other-prd>` while another pipeline is active refuses. Cancel the active one first or use `--resume`.
