---
name: release
description: Orchestrator-driven release. 8-check gate (branch, format, tests, sync, semver, tree-clean, tag-absent, remote-not-ahead, no-pending-approved) then atomic bump + commit + tag + push. Invocation is the push authorization. Halts on any failure with a resolution message and no side effects.
---

# /laplace:release

## Intent

One command runs the full release ceremony — version sync across three files,
commit, tag, push main, push tag — with an 8-check pre-release gate that halts
before the first irreversible step on any failure. The push is authorized by
command invocation (the human ran `/laplace:release <X.Y.Z>`), matching the
`/laplace:create-pr` pattern. Per ISSUE-0003 / `docs/prd-release-command.md`.

## When to Run

- After a piece of work lands on `main` and tests pass, when you want to
  cut a release.
- When you would otherwise manually bump `VERSION` + `plugin.json` +
  `marketplace.json`, commit, tag, and push.
- NOT for hotfix branches (v1: main only). NOT for changelog generation.

## What It Does

1. Runs the 8-check gate (all must pass; any fail → halt with a resolution
   message, exit non-zero, no side effects):

   | #  | Check              | Halt condition                                              |
   |----|--------------------|-------------------------------------------------------------|
   | 0  | Branch             | Not on `main`                                               |
   | 1  | Format             | `version` does not match `^\d+\.\d+\.\d+$`                  |
   | 2  | Tests              | `python3 -m pytest -q` exits non-zero                       |
   | 3  | Sync (post-bump)   | After bump, the three files disagree with `version`         |
   | 4  | SemVer direction   | `version` <= current (downgrade); `--force` relaxes         |
   | 5  | Tree clean         | `git status --porcelain` non-empty                          |
   | 6  | Tag absent         | `git rev-parse --verify v<X.Y.Z>` succeeds (tag exists)     |
   | 7  | Remote not ahead   | `origin/main` is ahead of local `main`                      |
   | 8  | No pending approved| `state._load_queue().approved` non-empty; `--force` relaxes |

   Checks 0, 1, 2, 4, 5, 6, 7, 8 run BEFORE any write. Check 3 runs inside
   the atomic sequence (after the bump).

2. On all-pass, executes atomically (best-effort; on any step failure,
   halt with a partial-state report):

   1. Bump `VERSION`, `.claude-plugin/plugin.json` `version`,
      `.claude-plugin/marketplace.json` `plugins[0].version`.
   2. `git add` the three files + `git commit -m "chore(release): bump <old> -> <new>"`.
   3. `git tag -a v<X.Y.Z> -m "v<X.Y.Z>: release"`.
   4. `git push origin main` (invocation-authorized — Option A).
   5. `git push origin v<X.Y.Z>`.

3. Every `git` op is routed through `policy.check_command`. For `git push`,
   the expected policy denial is overridden by the invocation authorization;
   the basis (`release-invocation`) is recorded in the release log.

4. Appends a release record to `.harness/state/releases.jsonl`:
   - On success: `{ts, version, prev_version, checks_passed: true,
     sequence_ok: true, pushed_at, commit, tag, main_pushed, tag_pushed,
     authorization_basis: "release-invocation"}`.
   - On halt: `{ts, version, prev_version, checks_passed: false,
     failed_check, reason}`.
   - On partial-push: `{..., partial: true, main_pushed: true, tag, reason}`.

## Constraints

- MUST route every `git` op through `policy.check_command` first.
- MUST NOT auto-rollback a successful main push on tag-push failure (the
  main commit is already public; rollback is manual `git revert`).
- MUST NOT skip checks 1, 2, 3, 5, 6, 7 with `--force`. `--force` ONLY
  relaxes checks 4 (downgrade) and 8 (pending approved).
- MUST treat the invocation (`/laplace:release <X.Y.Z>`) as the single
  authorization for the push (Option A, mirrors `/laplace:create-pr`).
- MUST NOT push from a non-main branch (v1: main only).
- MUST NOT infer the version number (the human passes `<X.Y.Z>`).
- The CI release workflow (`.github/workflows/release.yml`, triggered by
  tag push) is unchanged — `/laplace:release` is the local half; CI is the
  remote half.

## Output Format

On success (exit 0):

```
Released <prev> -> <new>: commit <sha8>, tag v<X.Y.Z>, pushed main + tag.
```

On halt (exit 1):

```
HALT: check '<name>' failed:
  <resolution message>
```

On partial-push (exit 1, main pushed but tag failed):

```
PARTIAL RELEASE: main pushed, tag push failed.
  main pushed but tag push failed: <error>; recover with: git push origin v<X.Y.Z>
```

On usage error (exit 2): not initialized, not a git repo, missing version arg.

## Failure Modes

- **Not on main** (check 0): halt, `not on main (on '<branch>'); run /laplace:release from main only`.
- **Bad format** (check 1): halt, `version '<v>' has bad format; expected X.Y.Z`.
- **Tests fail** (check 2): halt, last lines of pytest output shown.
- **Sync failure** (check 3, post-bump): halt, the three current values shown.
- **Downgrade** (check 4): halt unless `--force`, `downgrade <cur> -> <new>; pass --force to confirm`.
- **Dirty tree** (check 5): halt, `git status --porcelain` output shown.
- **Tag exists** (check 6): halt, `tag v<X.Y.Z> exists; bump to next version or delete the tag`.
- **Remote ahead** (check 7): halt, `origin/main has N new commits; pull/rebase first`.
- **Pending approved** (check 8): halt, `N issues approved but not run; release them first or /laplace:discard, or pass --force`.
- **Partial-push** (R-2): main pushed, tag push failed. Exit 1. Do NOT roll back main. Recover with `git push origin v<X.Y.Z>`.
- **Non-git repo**: exit 2, `Not a git repo: <path>`.
- **Not initialized**: exit 2, `Laplace is not initialized at <path>. Run /laplace:init first.`
