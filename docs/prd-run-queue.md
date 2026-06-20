# PRD: Queue Runner (`/laplace:run-queue`)

## Context

Laplace's run loop is **per-issue**: `/laplace:run <issue>` executes one issue through PM → Dev → Review → Security, then stops. The human must manually invoke `/laplace:run` again for the next approved issue.

For a PRD that decomposes into many small, independent issues, the manual hand-off between issues is friction with no safety benefit — every intra-issue gate already halts the loop. The only thing the manual step adds is a checkpoint *between* issues.

This PRD adds a **queue runner**: advances through the approved queue automatically while preserving every existing intra-issue gate.

## Goals

- `/laplace:run-queue` executes approved issues in order, auto-advancing on `review-passed`.
- Every intra-issue gate preserved: PM `blocked`, `needs-fix`, Security `human-approval-required`, all `require_approval_for` categories still halt.
- Stops at: queue exhaustion, blocking state, unmet dependency, or configurable consecutive-issue cap.
- Merge policy prevents issue N+1 building on unmerged N code.
- Full auditability: every auto-advance recorded in run log.

## Non-goals

- Parallel issue execution (future PRD).
- Auto-merge to protected branch (`main`/`master`).
- Weakening any existing gate.
- Changing per-issue `/laplace:run` (remains default + fallback).

## Global acceptance criteria

- **AC-G1**: Queue runner calls the SAME `runner.py` start/advance/end primitives as `/laplace:run`. No parallel gate implementation.
- **AC-G2**: Subject to `policy.py` deny list and `require_approval_for`. No bypass.
- **AC-G3**: PR creation decoupled — issue must independently pass `/laplace:create-pr` human gate (AC-LP-015).

## Global risks

- **R-1 Conflict cascade** under stacked branches → mitigated by `stack-branches` opt-in only (deferred from v1).
- **R-2 Evidence dilution** → every auto-advance records evidence; `max_queue_run` caps chain.
- **R-3 Lock contention** → reuse existing lock semantics; stop-and-report on held lock.

---

## Task: Config schema for queue runner

Add `max_queue_run` and `merge_policy` to `.harness/config.yml` defaults and validation.

### Background
Queue runner needs two new config knobs: a consecutive-issue cap and a merge policy selector. Both are additive, backward-compatible defaults.

### Scope
**In Scope:**
- `max_queue_run` (int, default 5) in `config.yml` `limits` block.
- `merge_policy` (enum: `wait-for-human-merge` | `auto-merge-branch`, default `wait-for-human-merge`) in `policy` block.
- Validation in `state.py` init/config-load: reject unknown policy values, non-positive caps.

**Out of Scope:**
- `stack-branches` policy (deferred).
- Runtime override of cap (config-time only in v1).

### Acceptance Criteria
- AC-QR-001-config: `state.py init` writes both keys with defaults.
- AC-QR-002-config: invalid `merge_policy` value rejected at load with non-zero exit + clear message.
- AC-QR-003-config: existing `.harness/config.yml` without new keys still loads (defaults applied).
- Unit tests for load + validation paths.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch
- Security Sensitivity: low (config plumbing only)

---

## Task: Issue schema — depends_on field

Add optional `depends_on` field to issue records; enforce in `state.py`.

### Background
Queue runner must refuse to start issue N+1 if it declares `depends_on: [ISSUE-NNNN]` and that issue is not yet `review-passed` (or, under `wait-for-human-merge`, not merged).

### Scope
**In Scope:**
- `depends_on` optional list field in issue schema + ISSUE-NNNN.md template.
- `intake.py` populates from explicit `Depends on:` lines in PRD task sections.
- `state.py` validates: referenced issues exist; cycle detection on approval.
- `state.py show` displays the field.

**Out of Scope:**
- Automatic dependency inference from code (declaration only in v1).

### Acceptance Criteria
- AC-QR-004-deps: intake parses `Depends on: ISSUE-0001, ISSUE-0002` lines into `depends_on`.
- AC-QR-005-deps: approval rejected on cycle or missing reference.
- AC-QR-006-deps: queue runner queries dependency state before starting.
- Unit tests: parse, cycle detection, missing-ref.

### Risk / Release Impact
- Risk Level: medium (schema change)
- Release Type: patch
- Security Sensitivity: low

---

## Task: Queue runner core (queue_runner.py)

New `scripts/queue_runner.py` composing `runner.py` primitives; queue-step run-log entries.

### Background
The runner is the orchestration layer. It does NOT re-implement gates — it loops over approved issues, invoking `runner.py` per issue, and decides advance vs halt based on the terminal state each issue reports.

### Scope
**In Scope:**
- `queue_runner.py start <start-issue?>`: acquire queue run id, iterate approved queue from head (or named issue).
- Per issue: call `runner.py start/advance/.../end` exactly as `/laplace:run` does.
- Decision matrix after each issue ends:
  - `review-passed` + merge policy satisfied → advance to next approved issue.
  - `blocked` / `needs-fix` / `human-approval-required` / unmet dependency → halt, report, preserve state.
  - consecutive-issue counter ≥ `max_queue_run` → halt after current.
  - queue exhausted → halt, report summary.
- Parent queue run id nests per-issue run ids.
- Queue-step entries appended to parent run log: `{from_issue, to_issue, from_terminal_state, evidence_run_id, ts}`.

**Out of Scope:**
- Merge execution itself (separate tasks).
- UI / TUI.

### Acceptance Criteria
- AC-QR-007-core: advancing only on `review-passed`; halts on every other terminal state.
- AC-QR-008-core: `max_queue_run` enforced.
- AC-QR-009-core: parent run log contains one queue-step entry per advance with required fields.
- AC-QR-010-core: held lock on next issue → halt + report (no force-acquire).
- Unit + characterization tests.

### Risk / Release Impact
- Risk Level: high (core orchestration)
- Release Type: minor
- Security Sensitivity: medium (gate routing)

---

## Task: Merge policy — wait-for-human-merge

Default policy: pause after `review-passed`, wait for human merge before starting next issue.

### Background
The safest chaining policy. After issue N reaches `review-passed`, the runner pauses and tells the human to merge N. If N+1 `depends_on` N, this pause is mandatory regardless of policy.

### Scope
**In Scope:**
- After `review-passed`, runner halts with `merge-wait:ISSUE-NNNN` status.
- Status message names the issue to merge and the resume command.
- Re-invoking `/laplace:run-queue` detects the merge state and continues.

**Out of Scope:**
- Performing the merge (human does it).

### Acceptance Criteria
- AC-QR-011-merge-wait: `review-passed` produces `merge-wait` halt, not auto-advance.
- AC-QR-012-merge-wait: resume after human merge detected via branch/base state check.
- Unit tests for halt + resume detection.

### Risk / Release Impact
- Risk Level: low
- Release Type: minor
- Security Sensitivity: low

---

## Task: Merge policy — auto-merge-branch

Opt-in policy: auto-merge issue branches into a designated integration branch.

### Background
For users who want chaining without per-issue human merge. Merges into `laplace/queue-<run-id>` integration branch, never into `main`/`master`.

### Scope
**In Scope:**
- On `review-passed`, merge issue branch into integration branch.
- Protected-branch guard: refuse to merge into `main`/`master`/configured protected refs.
- Merge conflict → halt with `merge-conflict`, do not force.

**Out of Scope:**
- Pushing integration branch to remote (local only in v1).

### Acceptance Criteria
- AC-QR-013-merge-auto: clean merge → advance; conflict → halt.
- AC-QR-014-merge-auto: protected-branch merge attempted → halt + error (never auto-merge protected).
- Unit tests with temp git repos.

### Risk / Release Impact
- Risk Level: medium (git mutations)
- Release Type: minor
- Security Sensitivity: medium (protected-ref guard is security-critical)

---

## Task: Skill and command — run-queue

`skills/run-queue/SKILL.md` + `commands/run-queue.md` exposing the runner.

### Background
User-facing entry point. Command wraps the skill like the other laplace commands.

### Scope
**In Scope:**
- `commands/run-queue.md` (thin imperative wrapper, like doctor/status).
- `skills/run-queue/SKILL.md` with intent, when-to-run, what-it-does, constraints, output format, failure modes.
- README + docs/USAGE.md updated with run-queue command + use case.

**Out of Scope:**
- Changes to existing `/laplace:run` command.

### Acceptance Criteria
- AC-QR-015-cmd: `/laplace:run-queue` and `/laplace:run-queue <issue>` both work.
- AC-QR-016-cmd: command is self-contained imperative (not passive "read and follow").
- AC-QR-017-cmd: documented in README command surface + USAGE guide.
- doctor recognizes the new skill.

### Risk / Release Impact
- Risk Level: low
- Release Type: minor
- Security Sensitivity: low

---

## Task: Status integration for queue runs

`/laplace:status` reports resumable queue runs. (Synchronous semantics: a queue run is never live between invocations — "resumable" means halted at a `merge-` gate awaiting human merge/resolution, not an active background process.)

### Background
Humans need visibility into where the queue runner is: current issue, position, merge policy, consecutive counter.

### Scope
**In Scope:**
- `state.py status` detects resumable queue run and emits: parent run id, current issue, position/total, merge policy, consecutive count.
- `commands/status.md` passes through verbatim.

**Out of Scope:**
- Live progress UI.

### Acceptance Criteria
- AC-QR-018-status: resumable queue run shows all five fields.
- AC-QR-019-status: no resumable queue run → existing single-issue status unchanged.
- Characterization tests.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch
- Security Sensitivity: low

---

## Task: Cancel and resume queue runs

`/laplace:cancel` stops a queue run safely and persists position for resume.

### Background
Extend cancel to handle queue-scope: finish current issue's in-flight phase per existing cancel semantics, release locks, record queue position so `/laplace:run-queue` resumes.

### Scope
**In Scope:**
- Cancel during queue run: writes queue-position record (last completed issue, next issue).
- Re-invoking `/laplace:run-queue` reads position record and resumes.
- Existing single-issue cancel behavior preserved (characterization).

**Out of Scope:**
- Auto-resume on session restart (explicit re-invocation only in v1).

### Acceptance Criteria
- AC-QR-020-cancel: cancel records queue position; locks released; state preserved.
- AC-QR-021-cancel: resume continues from recorded position.
- AC-QR-022-cancel: single-issue `/laplace:cancel` unchanged.
- Unit + characterization tests.

### Risk / Release Impact
- Risk Level: medium
- Release Type: minor
- Security Sensitivity: low

---

## Task: Test suite for queue runner

Unit + characterization coverage per acceptance criteria.

### Background
Consolidated test task ensuring AC coverage and that existing `/laplace:run` behavior is unchanged.

### Scope
**In Scope:**
- Unit tests: queue ordering, dependency enforcement, cap enforcement, halt-on-gate, merge-policy branching.
- Characterization tests: existing single-issue run flow unchanged.
- Integration tests: queue run with temp git repo through two issues end-to-end.

**Out of Scope:**
- E2E in a live Claude session (manual).

### Acceptance Criteria
- AC-QR-023-tests: all unit tests pass; coverage ≥ 85% for new modules.
- AC-QR-024-tests: characterization tests prove single-issue `/laplace:run` semantics identical.
- AC-QR-025-tests: integration test runs two-issue queue with a mid-queue gate halt.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch
- Security Sensitivity: low

---

## Open questions (PM phase)

- `depends_on` source: PRD-declared (this PRD's choice) vs inferred. Recommend declared + inferred fallback.
- `stack-branches` in v1? Recommend defer.
- Auto-resume on restart? Recommend explicit re-invocation in v1.
