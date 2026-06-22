# PRD: Worktree isolation for issue development

## Status
Draft — for `/laplace:intake`.

## Context

Laplace's run loop develops each issue on a branch `laplace/<issue-id>` via `git checkout -b` in the project's main working tree. This session exposed three concrete defects from that single-working-tree model:

1. **Smoke-test pollution of main** — a smoke-test branch merged its tagline commit into main and the cleanup (`git reset --hard`) was harness-denied, leaving test content in the release. With worktrees, the smoke test would have lived in an isolated worktree and main's working tree would never have carried the tagline.
2. **Stale branch reuse** — `runner.py start` reuses `laplace/<issue-id>` if it exists. Across PRD rounds (or interrupted runs), a stale branch from a prior round was resurrected with old code, hiding the current main state. The runner never validated the branch was based on current main.
3. **Parallel-impossible** — two issues cannot run concurrently when they share one working tree; `git checkout` between them serializes everything. Parallel queue execution (a future PRD) requires per-issue worktrees.

Git worktrees solve all three: each issue gets its own working directory linked to the same repo, main's working tree stays clean, and parallel execution becomes structurally possible.

## Problem

- Issue dev work happens in the project's only working tree → main's tree carries uncommitted/in-flight issue state, polluting manual inspection and release cuts.
- `runner.py start` reuses any existing `laplace/<id>` branch without checking its base is current main → stale branches resurrect old code.
- No structural foundation for parallel issue execution (the queue runner is sequential partly because of this).

## Goals

- Each issue's dev phase runs inside a dedicated git worktree, not the main working tree.
- Main working tree is never modified by issue dev (only by explicit merges the human drives).
- `runner.py start` validates a reused `laplace/<id>` branch is based on current main; if stale, either fast-forwards or halts with a clear message (never silently resurrects old code).
- `runner.py end` removes the worktree (working dir) but preserves the branch for merge.
- Non-repo projects (BRANCH_SKIPPED) keep current behavior — worktree is git-only.
- Foundation for parallel queue execution (a separate PRD builds on this).

## Non-goals

- Parallel queue execution itself (future PRD — this PRD only provides the isolation primitive).
- Multi-worktree lifecycle management UI (list/prune worktrees beyond what `git worktree` already provides).
- Worktree isolation for the PM/review/security agents (they are read-only or operate on the dev worktree's diff; they don't need their own worktrees).
- Changing the merge policy or the `/laplace:create-pr` flow.
- Remote worktrees (local only).

---

## Task: runner.py worktree-per-issue + stale-branch guard

### Background
`runner.py _setup_branch` (the branch-creation step in `start`) currently does `git checkout -b laplace/<id>` in the main working tree. Replace the checkout with `git worktree add`; add a stale-base check on reuse. `end` removes the worktree. The queue runner and per-issue agents operate against the worktree path, not the main working tree.

### Scope
**In Scope:**
- `scripts/runner.py`:
  - `_worktree_path(issue_id, target)` → `.harness/worktrees/<issue-id>/` (configurable path; default under `.harness/` so it's gitignored and ephemeral). All git operations for the issue route through `git -C <worktree_path>`.
  - `_setup_branch(issue_id, target)` rewrite:
    - If `laplace/<id>` branch does NOT exist: create it from current main HEAD via `git worktree add -b laplace/<id> <worktree_path> <main>`.
    - If `laplace/<id>` exists: check `git merge-base --is-ancestor main laplace/<id>` (is main an ancestor of the branch? i.e. is the branch up to date with main). If NOT (stale), halt with `BRANCH_STALE:<issue-id>: rebase onto main or delete branch` — do NOT silently reuse. If yes (current), `git worktree add <worktree_path> laplace/<id>` (reuse branch in a fresh worktree).
    - Non-repo / git unavailable → record `BRANCH_SKIPPED:<reason>` (unchanged fail-safe).
    - All git ops routed through `policy.check_command` first (already the case; preserve).
  - `_teardown_worktree(issue_id, target)` (called from `end`): `git worktree remove <worktree_path>` (force only if dirty-tree flag set; default no-force → halt if dirty). Branch `laplace/<id>` is NOT deleted (preserved for merge).
  - All downstream references to the issue's working dir (dev agent spawn path, review agent diff base, evidence capture) use the worktree path, not the main tree.
  - Run log records `worktree_path` alongside `branch`.
- `scripts/queue_runner.py` — no change to decision logic; child `runner.cmd_start` now creates a worktree per issue. Integration test extended to assert worktree creation/removal.
- `skills/run/SKILL.md` — Step 1 (Start the run) updated: documents worktree creation (not checkout); Step 6 (End) documents worktree removal; dev/review agent spawn paths point at the worktree.
- `.harness/worktrees/` added to the `.harness/.gitignore` (worktrees are local ephemeral state).
- `runner.py selftest` — extend: (a) worktree created on start; (b) main working tree clean during dev; (c) stale branch → halt; (d) `end` removes worktree, preserves branch; (e) BRANCH_SKIPPED unchanged for non-repo.
- `tests/test_run_worktree_unit.py` (new) — temp real git repo; assert worktree path exists after start, main tree unmodified, stale detection, teardown.

**Out of Scope:**
- Parallel execution (future PRD).
- Worktree pruning UI (`git worktree prune` is enough; no laplace command).
- Configurable worktree root (hardcode `.harness/worktrees/` in v1; config later if needed).

### Acceptance Criteria
- AC-WT-001: `runner.py start <issue>` creates a git worktree at `.harness/worktrees/<issue-id>/` on branch `laplace/<issue-id>` (branched from current main HEAD). The main working tree is NOT switched to the issue branch.
- AC-WT-002: during the dev phase, the main working tree remains on its prior branch (e.g. `main`) and carries no issue-dev changes. Issue changes exist only in the worktree.
- AC-WT-003: `runner.py end` removes the worktree (`git worktree remove`) but leaves branch `laplace/<issue-id>` intact for later merge.
- AC-WT-004 (stale guard): if `laplace/<issue-id>` already exists AND main has commits the branch doesn't have (branch is stale), `start` halts with `BRANCH_STALE:<issue-id>` and does NOT create a worktree. The human resolves (rebase, delete branch, or force) explicitly.
- AC-WT-005: if `laplace/<issue-id>` exists and IS current with main, `start` reuses it in a new worktree (idempotent).
- AC-WT-006: non-repo target → `BRANCH_SKIPPED:not-a-git-repo` recorded, no worktree op, run proceeds with state transitions only (unchanged fail-safe).
- AC-WT-007: every git op routed through `policy.check_command` first (preserve existing contract).
- AC-WT-008: `git worktree remove` on a dirty worktree (uncommitted changes) at `end` → halt with `WORKTREE_DIRTY:<issue-id>` unless explicitly forced; do NOT silently discard dev work.
- AC-WT-009: run log records `worktree_path` and `branch`; teardown status recorded.
- AC-WT-010: characterization — single-issue `/laplace:run` semantics (state transitions, evidence, gates) unchanged; only the physical working location moves. Existing runner selftest + tests still pass.

### Risks
- **R-1 Worktree path inside `.harness/`** — `.harness/` is gitignored, so worktrees are ephemeral and never committed. But `.harness/worktrees/` holds real git working trees; a careless `rm -rf .harness` could corrupt the worktree registry. Mitigation: `git worktree remove` before any harness reset; document in the cancel/init skills. `runner.py end` already removes the worktree; `state.py init` should refuse if worktrees are registered (warn, don't auto-delete).
- **R-2 Stale guard false positive** — a branch legitimately ahead of main (e.g., dev committed but main hasn't merged prior issue) would read as "stale" under the `--is-ancestor main branch` check. Refine: stale = `main` is NOT an ancestor of `branch` (main has commits branch lacks) → halt. Branch-ahead-of-main is fine (normal during a queue run). Document the direction.
- **R-3 Dirty worktree at end** — dev left uncommitted changes. AC-WT-008 halts rather than discards; the human commits or cancels. Pair with the ISSUE-0012 dev-auto-commit codification (dev should have committed), making this a rare safety net.
- **R-4 Worktree registry corruption** — if the process crashes between `git worktree add` and recording in the run log, an orphan worktree lingers. Mitigation: `runner.py start` records the worktree path in the run log immediately after `git worktree add`; a `runner.py prune` (or `state.py prune-worktrees`) subcommand removes worktrees whose run logs are ended. v1: document `git worktree prune` as the manual recovery; automation deferred.

### Risk / Release Impact
- Risk Level: medium (changes the core run path's physical model)
- Release Type: minor (0.3.0 — architecture change, behavior-preserving for happy path)
- Security Sensitivity: medium (worktree path construction; stale-guard direction; policy.check_command routing preserved)

---

## Open questions (PM phase)

- Worktree root: `.harness/worktrees/` (gitignored, ephemeral) vs `.claude/worktrees/` (Claude Code native convention) vs `~/.moai/worktrees/` (MoAI persistent). Recommend `.harness/worktrees/` for v1 (ephemeral, local, consistent with `.harness/` ownership); revisit if users want persistence across harness resets.
- Stale-guard resolution path: halt-only (human resolves) vs offer `--rebase`/`--reset` flags on `start`. Recommend halt-only in v1 (explicit human action); flags later.
- Should `/laplace:cancel` also remove the worktree? Recommend yes (cancel = full teardown, preserve branch). Add to cancel.py.
- Parallel runner (future PRD): does the queue runner need a `--parallel` flag that spawns N worktrees at once? Out of scope here; this PRD is the prerequisite.
