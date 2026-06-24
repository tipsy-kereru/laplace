# Changelog

All notable changes to Laplace are documented here. Versions follow
[Semantic Versioning](https://semver.org/). All features listed are
opt-in / off by default unless noted — existing loops are unchanged on
upgrade.

## [0.7.0] — 2026-06-24

### Added

- **SPEC-007: freerange scope override.** `/laplace:freerange on {flow|publish|supply|all}`
  suppresses Laplace's approval layer so the loop can run unattended.
  Human-only (slash command), scope-bounded, TTL-limited (24h default,
  168h ceiling). Audit log at `.harness/logs/freerange.jsonl`.
  - `flow`: auto-approve drafts (bypasses the `/laplace:approve` gate and
    the pipeline approve-gate halt).
  - `publish`: allow `git push`, `gh pr create`, `npm publish` without approval.
  - `supply`: allow `pip install`, `npm install`, `claude mcp add` without approval.
  - `all`: union of the above.
  - **Not a security boundary** (SPEC-002 NG-007). A determined agent
    with Bash can defeat it. The deny layer (`rm -rf /`, `curl|sh`,
    `sudo`, `ssh`, `aws`, `gcloud`, `kubectl`) is never consulted or
    suppressed by freerange. `aws`/`gcloud`/`kubectl` remain
    approval-required under every scope (intentionally unsuppressed —
    cloud production access).

### Changed

- `policy.py:check_command` accepts an optional `target` and consults
  freerange on the approval path. Deny path unchanged.
- `state.cmd_approve` records `user="freerange"` when `flow` is active.
- `pipeline.py:_phase_approve_gate` skips the halt and auto-approves
  drafts when `flow` is active.

### Tests

- `tests/test_spec007_freerange.py` (19). Suite: 315 passing, zero
  regressions against 0.6.0.

## [0.6.0] — 2026-06-24

Four opt-in features extending the loop with type-aware evidence gates,
dependency failure propagation, a release budget gate, and event-driven
resumption. All disabled by default; upgrading from 0.5.1 changes no
existing behavior.

### Added

- **SPEC-003: type-aware evidence gates.** `routing-rules.yml` now accepts
  an `evidence_requirements:` block mapping issue types to required
  evidence kinds on state exits. Defaults: `bug` requires `reproduction`
  evidence at `pm-review`; `ui` requires `visual` evidence at `review`.
  Adding a type is data-only (no code change). New evidence kinds
  `reproduction` and `visual` join the allowed set.
- **SPEC-004: upstream blocker propagation.** When an issue reaches a
  failure (`blocked`, `cancelled`, `max-attempts-exceeded`) or stalled
  (`human-approval-required`) terminal, its non-terminal dependents are
  marked `blocked` with reason `upstream:<id>:<state>`. Transitive chains
  settle in one pass. Eliminates dispatching dependents of failed
  upstreams (previously they were dispatched because terminal states
  satisfied `_dependencies_satisfied`).
- **SPEC-005: motivation triggers.** New `scripts/motivations.py`
  one-shot scheduler (`motivations.py --once`) for external timers
  (cron, launchd, systemd). Four triggers: `clock`, `git-upstream`,
  `idle-queue`, `test-signal`. Each enforces an issue-state
  precondition; `human-approval-required` and other non-`approved`
  states are no-ops. Global sliding-window rate limiter. Kill switch
  (`motivations.enabled`) re-read every invocation.
- **SPEC-006: cost watcher gate.** New optional `cost-review` phase
  between `security-review` and `review-passed`. Aggregates runtime,
  files-changed, and best-effort token signals from the run log;
  blocks release on threshold breach (transition to
  `human-approval-required` with reason `cost-block:<signal>:<value>`).
  Config validator refuses `block >= hard_cap`. AC-LP-008 test-evidence
  gate preserved on every release.

### Changed

- `scripts/state.py` `VALID_TRANSITIONS`: `security-review` gains
  `cost-review` exit; new `cost-review` row. `IN_FLIGHT_STATUSES`
  in `parallel_queue.py` includes `cost-review`.
- `_set_issue_state` accepts a `block_reason` kwarg; cleared on any
  transition away from `blocked`.
- `load_config` returns a `cost_watcher` key.

### Tests

- `tests/test_spec003_evidence_gates.py` (8)
- `tests/test_spec004_blocker_propagation.py` (6)
- `tests/test_spec005_motivations.py` (9)
- `tests/test_spec006_cost_watcher.py` (14)

Total suite: 296 passing, zero regressions against 0.5.1.

## [0.5.1] — prior

Load-aware rate limiter, orphan worktree reconcile, advisory file-overlap
warning. See git history.
