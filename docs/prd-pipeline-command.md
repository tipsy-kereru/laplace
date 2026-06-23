# PRD: `/laplace:pipeline` — single end-to-end command

## Status
Draft — for `/laplace:intake`.

## Context

Laplace today is 5 commands chained by the human:
`/laplace:intake` → `/laplace:verify` → `/laplace:approve` (per issue) → `/laplace:run-parallel` (per wave) → `/laplace:release`.

Every transition is a manual invocation. For a maintainer who wants "take this PRD and run it to a release", that's 5+ keystrokes plus remembering the order, the per-issue approve loop, and the merge-wait/queue-exhausted handshake. The pieces are all automated individually; the **sequencing** is not.

This PRD adds a **checkpoint pipeline** — a single command that drives the whole sequence, halting at each human gate, surfacing what it needs, and resuming on re-invocation. It does NOT remove any gate. It removes the keystroke choreography between gates.

## Problem

- 5 commands in a fixed order; a missed step (forget verify, forget to re-approve after a fix) silently degrades.
- No single "where is this PRD in the flow?" view — status reports queue state, not pipeline-phase state.
- Resume after interruption requires the human to remember which command was next.
- Per-issue approve is N keystrokes for N issues; a pipeline gate can batch-approve after the human accepts the verify report.

## Goals

- `/laplace:pipeline <prd>` drives: intake → verify → approve-gate → run-parallel (waves + merge-waits) → release-gate.
- **Every human gate halts** with a specific "what I need from you" message; re-invoking `/laplace:pipeline --resume` (or `/laplace:pipeline <prd>` again) continues from the next phase.
- Pipeline-run log records the current phase; `/laplace:status` reports it ("pipeline at: approve-gate, 3 drafts ready, verify PASS").
- Approve gate: halt ONCE (not per-issue). The human reads the verify report, then resumes — the pipeline batch-approves all drafts. This collapses N approve keystrokes into 1 resume.
- Merge gate: per-issue merge-wait halts are surfaced as part of the run-parallel phase (unchanged — the pipeline delegates to run-parallel's existing wave/merge model).
- Release gate: halt before release. Human either invokes `/laplace:release <ver>` separately, OR passes `--release <ver>` to have the pipeline call it after queue-exhausted.
- **Auto-approve is opt-in and scoped**: `--auto-approve-low-risk` skips the approve gate ONLY for issues whose Risk Level is `low` (per the issue's Risk/Release Impact field). Medium+ still halt. Default OFF (the approve gate always halts).
- Existing commands unchanged — the pipeline composes them (intake.py, verify.py, state.approve, parallel_queue.py, release.py). No re-implementation.

## Non-goals

- Removing or weakening any gate. Every gate still fires; the pipeline only sequences.
- Auto-merging to main. Merge-wait halts per-issue, human merges (unchanged).
- Auto-deciding the release version. `--release <ver>` takes an explicit version; without it, the pipeline halts before release and the human invokes `/laplace:release` themselves.
- Replacing the individual commands. Power users still chain them manually; the pipeline is the convenience path.
- Dynamic re-planning mid-pipeline (PRD changes, issues added). Cancel + re-run.
- Running multiple pipelines concurrently. v1: one active pipeline per harness (the harness is single-project).

---

## Task: pipeline orchestrator over existing commands

### Background
The pipeline is a thin state machine. Each phase calls an existing command's Python entry point (`intake.cmd_intake`, `verify.cmd_verify`, `state.cmd_approve`, `parallel_queue.cmd_parallel_start`, `release.cmd_release`), records the phase transition in a pipeline-run log, and either proceeds (auto phases) or halts (gates). Resume reads the log and jumps to the recorded phase.

### Scope
**In Scope:**
- `scripts/pipeline.py` with `cmd_pipeline(args)`:
  - Phases (in order): `intake` → `verify` → `approve-gate` → `parallel` → `release-gate` → `done`.
  - `intake`: call `intake.cmd_intake` with the PRD path. On failure (parse error) → halt `intake-failed`.
  - `verify`: call `verify.cmd_verify` with the PRD path. On FAIL verdict → halt `verify-failed:<reasons>` (surface the report). On PASS/WARN → proceed (warn is informational).
  - `approve-gate`: if `--auto-approve-low-risk`, batch-approve drafts whose Risk Level == low; halt for medium+. Else HALT (`approve-gate`, surface verify report + draft list + per-issue risk). On resume: batch-approve all remaining drafts, proceed.
  - `parallel`: delegate to `parallel_queue.cmd_parallel_start` wave model. Each wave dispatches; the pipeline re-invokes parallel per wave. Halt sub-states inherit from run-parallel: `wave-dispatched:waiting` (re-invoke after terminal), `merge-wait:<id>` (human merges), `queue-exhausted` → proceed to release-gate. A pipeline-level halt `parallel-blocked:<id>` if an issue hits `blocked`/`human-approval-required`.
  - `release-gate`: if `--release <ver>` provided AND parallel phase reached queue-exhausted AND no halted issues → call `release.cmd_release(<ver>)`. Else HALT (`release-gate`, suggest `/laplace:release <ver>`).
  - `done`: finalize pipeline log.
  - Pipeline-run log at `.harness/state/runs/<pipeline-run-id>.json`: `{run_id, kind:"pipeline", prd, started_at, ended_at, outcome, phase, phase_history:[{ts, phase, result}], max_parallel, auto_approve_low_risk, release_version}`.
  - Resume: re-invocation reads the most-recent non-finalized pipeline log, jumps to `phase`, continues. `--resume` explicit or implicit (same PRD path).
  - `selftest()` — temp harness, happy path (all auto phases + gate halts asserted), each halt sub-state, resume-after-approve, resume-after-merge, release-gate with and without `--release`.
- `commands/pipeline.md` — imperative wrapper. `argument-hint: "<prd> [--release <ver>] [--auto-approve-low-risk] [--max-parallel N] [--resume]"`. Body runs `pipeline.py` and maps each halt to a next-action.
- `skills/pipeline/SKILL.md` — Intent / When to Run / What It Does (phase machine + every gate halts) / Constraints (no gate removed; composes existing commands) / Output Format / Failure Modes (one per halt sub-state).
- `scripts/state.py` `_format_status` — add "Pipeline:" block when an active pipeline log exists (kind=="pipeline", not finalized): phase, prd, drafts/approved/in-flight/merge-waited counts, next action. Byte-identical when no pipeline (characterization).
- README + docs/USAGE.md — pipeline row + "Pipeline workflow" section.
- `tests/test_pipeline_unit.py` — one test per halt + happy path + resume + characterization.

**Out of Scope:**
- Auto-merge to main.
- Concurrent pipelines.
- Inferring release version.
- Replacing individual commands.
- File-overlap detection (inherits run-parallel's defer).

### Acceptance Criteria
- AC-PL-001: `/laplace:pipeline docs/prd.md` runs intake + verify, then halts at `approve-gate` with the verify report + draft list + per-issue risk. Exit 0 (halt, not error).
- AC-PL-002: re-invoking `/laplace:pipeline --resume` (or same PRD path) after the human reviews the gate batch-approves all drafts and proceeds to the parallel phase.
- AC-PL-003: `--auto-approve-low-risk` skips the approve-gate halt for low-risk drafts (batch-approve them) but STILL halts if any draft is medium+ risk (surface those for manual approve).
- AC-PL-004: the parallel phase delegates to `parallel_queue.cmd_parallel_start` wave semantics; merge-wait halts surface as `parallel:merge-wait:<id>`; re-invoking after a merge resumes the next wave.
- AC-PL-005: queue-exhausted (all issues review-passed + merged) transitions to `release-gate`.
- AC-PL-006: `release-gate` halts by default, suggesting `/laplace:release <ver>`. With `--release 0.5.0`, the pipeline calls `release.cmd_release("0.5.0")` (inheriting its 8-check gate + Option A push).
- AC-PL-007: every gate halt is resumable — the pipeline-run log records `phase`; re-invocation continues from that phase (no re-intake, no re-verify if already done).
- AC-PL-008: `intake-failed` / `verify-failed:<reasons>` / `parallel-blocked:<id>` halt with specific recovery paths; the pipeline does NOT silently proceed past a failure.
- AC-PL-009: `/laplace:status` reports the active pipeline (phase, prd, counts, next action); byte-identical output when no active pipeline.
- AC-PL-010: `/laplace:cancel` cancels the active pipeline (finalizes log as `cancelled`, does NOT touch in-flight issues — those are cancelled via the parallel phase's existing cancel path).
- AC-PL-011: characterization — existing commands (intake, verify, approve, run-parallel, release) unchanged; the pipeline composes them, doesn't fork.
- AC-PL-012: a `verify-failed` halt (verify reports FAIL) blocks the approve-gate — the pipeline does NOT let the human batch-approve until verify passes or the human explicitly overrides (`--force-verify`, documented as escape hatch).

### Risks
- **R-1 Batch-approve blast radius**: batch-approving N drafts from one gate means the human trusts the verify report for all N. Mitigation: verify is mandatory before the gate (AC-PL-012); the human reads the per-issue table before resuming; `--auto-approve-low-risk` is opt-in and risk-scoped.
- **R-2 Pipeline/parallel coupling**: the parallel phase delegates wave dispatch but the pipeline must track "am I between waves" vs "am I at a merge-wait". Mitigation: read the active parallel-run log's outcome (`wave-dispatched:waiting` = between waves; `merge-wait:<id>` or `cancel-failed:<id>` = halt) and map to pipeline sub-states explicitly.
- **R-3 Resume ambiguity**: if the human runs `/laplace:pipeline prd-B.md` while a pipeline for prd-A is active, what happens? v1: refuse — "active pipeline for prd-A; cancel it first or use --resume". Document.
- **R-4 Release-gate version mismatch**: `--release 0.5.0` but the human already released 0.5.0 manually → release.cmd_release's tag-exists check (check 6) halts with the existing message. The pipeline surfaces it. No new logic needed.
- **R-5 State drift between phases**: the human could `/laplace:approve` manually mid-pipeline, or `/laplace:discard` a draft. The pipeline must re-read state at each phase entry, not trust its log. Mitigation: every phase prologue re-reads tasks/queue; the phase record is a hint, disk is truth.

### Risk / Release Impact
- Risk Level: medium (new orchestration layer over 5 commands; gate-handling is the safety surface)
- Release Type: minor (0.5.0 — new command, additive, composes existing)
- Security Sensitivity: medium (batch-approve + release-gate + resume state; gates preserved by construction)
