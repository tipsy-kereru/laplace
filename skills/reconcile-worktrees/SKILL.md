---
name: reconcile-worktrees
description: List and optionally sweep orphan Laplace worktrees (on disk with no live run-log reference). Read-only by default; --sweep removes orphans only.
---

# /laplace:reconcile-worktrees

## Intent

Recover worktrees orphaned by a crash between `git worktree add` and the
parent-run-log append (security finding 4, low). `git worktree prune` only
removes worktrees whose DIRECTORY is gone — it cannot recover the reverse
case where the directory exists but no live run references it. This command
scans `.harness/state/runs/*.json` for `worktree_path` references and
reconciles against `git worktree list --porcelain` (ISSUE-0013).

## When to Run

- After a parallel-queue run that crashed or was killed mid-dispatch.
- When `/laplace:status` reports `Orphan worktrees: N` (non-zero).
- As part of periodic harness hygiene.

## Categories

- **live**: on disk AND some NON-finalized run log references it. NEVER
  touched (AC-OW-002).
- **orphan**: on disk AND only FINALIZED run log(s) reference it. The
  last-known issue id is recovered from the most-recent finalized log.
  Sweepable with `--sweep`.
- **manual recovery**: on disk AND NO run log references it at all. Reported
  only, NEVER auto-swept (AC-OW-004) — the issue id is unrecoverable.

## Checklist

Run the reconcile subcommand and report the output verbatim:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/parallel_queue.py reconcile-worktrees [--sweep] [--yes] [--target <path>]
```

- Default (no flags): list orphans and manual-recovery entries, exit 0.
- `--sweep`: remove orphan worktrees only. Live worktrees are never removed.
  Manual-recovery entries are reported but never swept.
- `--sweep --yes`: skip the interactive confirmation prompt.

## Constraints

- MUST route every git invocation through `policy.check_command` (the script
  already does this internally).
- MUST NOT remove a worktree referenced by a non-finalized run (AC-OW-002).
- MUST NOT auto-sweep manual-recovery entries (AC-OW-004).
- MUST NOT push, create branches, or modify issue state.

## Output Format

```
Orphan worktrees (N):
  <path> (last issue: <ISSUE-id>)
Manual recovery (M):
  <path> (no run log; remove manually if safe)
```

When sweeping:

```
swept N orphan worktree(s)
```

Failures (e.g. policy-denied or git error) print `failed: <path>: <reason>`
to stderr and the command exits 1.

## Next

- After a successful sweep, re-run `/laplace:status` to confirm the orphan
  count is zero.
- For manual-recovery entries, inspect the worktree directory by hand and
  `git worktree remove --force <path>` if safe.
