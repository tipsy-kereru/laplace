# Changelog

All notable changes to Laplace are documented here. Versions follow
[Semantic Versioning](https://semver.org/). All features listed are
opt-in / off by default unless noted — existing loops are unchanged on
upgrade.

## [0.7.3] — 2026-06-25

### Added — Codex plugin discovery (fixes "plugin not found in /plugins")

- **`.codex-plugin/plugin.json`** — the Codex-required plugin manifest
  (entry point). Codex looks for this exact path; without it the
  marketplace lists the plugin but Codex cannot load it. Mirrors the
  Claude Code manifest with Codex-native fields (`interface.displayName`,
  `homepage`, `license`, `keywords`).
- **`.agents/plugins/marketplace.json`** — the Codex-native marketplace
  catalog with the required `source.path`, `policy.installation`,
  `policy.authentication`, and `category` fields. Codex reads this in
  preference to the legacy `.claude-plugin/marketplace.json`.

### Changed

- README (en + ko) Codex install section: added the mandatory `/hooks`
  trust step. Per Codex docs, plugin-bundled hooks are non-managed and
  Codex skips them until the user reviews and trusts them — the previous
  "no /hooks step" claim was wrong.

## [0.7.2] — 2026-06-25

### Changed (documentation correction)

- **Codex runs at full hook parity with Claude Code.** Codex loads
  `hooks/hooks.json`, sets `CLAUDE_PLUGIN_ROOT`, and dispatches the full
  lifecycle event surface (PreToolUse, PostToolUse, Stop, SessionStart,
  UserPromptSubmit, SubagentStart/Stop, PostToolUseFailure). The 0.7.0
  "instruction-only tier" and 0.7.1 "SessionStart only" framings were
  **wrong** — the deny layer, evidence gates, and stop-loop all enforce
  on Codex exactly as on Claude Code. Per the
  [Codex hooks documentation](https://developers.openai.com/codex/hooks)
  and [Build plugins](https://developers.openai.com/codex/plugins/build).
- README Codex section rewritten: parity table now shows every hook
  firing on both hosts. The 0.7.1 Node activation hook remains valuable
  for Windows Codex (where `router.sh` cannot run).
- `AGENTS.md` subtitle and approval-gate section updated: gates are
  enforced, not self-enforced.

No code changes. The hooks were already correct; only the docs
overclaimed the limitation.

## [0.7.1] — 2026-06-25

### Added

- **Codex Node SessionStart hook** (`hooks/laplace-activate.js`). Pure-Node,
  cross-platform activation that reads `.harness/` state and injects a
  harness summary (queue counts, active run, freerange status, next-action
  hint) into the session. Fires identically on Claude Code and Codex
  (macOS/Linux/Windows). Mirrors the Ponytail `ponytail-activate.js`
  portability pattern. Registered in `hooks/hooks.json` SessionStart
  alongside the existing `router.sh`, with a `commandWindows` variant.

### Changed

- README Codex section replaced the "instruction-only" caveat with an
  honest hook-by-hook table: SessionStart activation + UserPromptSubmit
  routing fire on Codex; PreToolUse/PostToolUse/Stop (deny layer,
  evidence gates, stop-loop) remain Claude-Code-only.

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
