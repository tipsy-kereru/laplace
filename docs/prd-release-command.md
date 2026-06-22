# PRD: `/laplace:release` — orchestrator-driven release command

## Status
Draft — for `/laplace:intake`.

## Context

Releasing a laplace version today is a 5-step manual ceremony: bump `VERSION` + `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`; commit; tag; push main; push tag. Every step is done by hand (by the orchestrator model on behalf of the human), and every release this session hit at least one friction point: version-desync between the three files, tag-already-exists, smoke-test pollution swept into a release, or a `git push` blocked by laplace's own `require_approval_for: git_push` policy (forcing a `!`-prefixed manual push).

The push policy is correct — push is an external side effect. But the human's intent ("release 0.3.1") is a single authorization that should drive the whole safe sequence, halting only when a check fails. This mirrors `/laplace:create-pr` (AC-LP-015): invocation IS the approval; the command does the work; the orchestrator validates before each irreversible step.

## Problem

- 5 manual steps per release, each a chance to forget one (the three-file version sync drifted twice this session).
- No pre-release validation: broken tests, dirty tree, unmerged issues, or existing tags surface only after a partial release is pushed.
- The `git_push` policy forces a `!`-manual push every time, even though the human already authorized the release by invoking the command.
- No single audit trail: release decisions are scattered across manual commits/tags rather than a release log.

## Goals

- One command `/laplace:release <X.Y.Z>` runs the full release sequence: version sync (3 files) → commit → tag → push main → push tag.
- A pre-release gate runs 7 checks; ANY failure halts before the first irreversible step with a specific resolution path.
- Push is authorized by command invocation (the human ran `/laplace:release`), matching the `/laplace:create-pr` pattern. The release command is the policy-approved path for its own push.
- Halt is safe: no commit, no tag, no push on any check failure. State preserved. Re-runnable after the human resolves.
- The existing release workflow (`.github/workflows/release.yml`, triggered by tag push) is unchanged — it still validates version consistency and creates the GitHub Release. `/laplace:release` is the local half; CI is the remote half.

## Non-goals

- Auto-deciding the version number (the human passes `<X.Y.Z>`; the command does not infer patch/minor/major).
- Changelog generation (deferred — the CI workflow generates release notes from commits).
- Releasing from a non-main branch (v1: main only).
- Rollback of a published release (once pushed, it's public; rollback is manual `git revert` + force-tag, out of scope).
- Replacing the CI release workflow.
- Signing tags (GPG) — deferred.

---

## Task: release command + 7-check gate + policy-authorized push

### Background
The release command is a thin orchestrator over `git` + the existing `state.py`/`policy.py` primitives. It does NOT re-implement version validation (the CI workflow already does 3-way consistency on tag push); it adds LOCAL pre-checks so failures surface before push, not after.

### Scope
**In Scope:**
- `scripts/release.py` with `cmd_release(args)`:
  - Args: `version` (required, `X.Y.Z`), `--target`, `--force` (skip downgrade/pending-issue warnings only; never skips tests or tag-exists).
  - 7-check gate (all must pass; any fail → halt with resolution message, exit non-zero, no side effects):
    1. **Format**: version matches `^\d+\.\d+\.\d+$`.
    2. **Tests**: `python3 -m pytest -q` exits 0 (route through policy.check_command; this is a local test run, not network).
    3. **Version sync target**: after bumping, `VERSION` == `plugin.json.version` == `marketplace.json.plugins[0].version` == `<version>` arg. (The command writes all three itself, so this is a self-check that the writes landed.)
    4. **SemVer direction**: new version > current version (parse major.minor.patch numerically). Equal or lower → halt unless `--force` (downgrade is almost always a mistake).
    5. **Tree clean**: `git status --porcelain` empty (no uncommitted work swept into the release). Halt with `git status` output.
    6. **Tag absent**: `git rev-parse --verify v<X.Y.Z>` fails (tag doesn't exist). Halt if it does.
    7. **Remote not ahead**: `git fetch origin main` then `git rev-list --count main..origin/main` == 0 (or main is ahead, which is fine). If origin is ahead → halt "pull/rebase first; origin has N new commits".
    8. **No pending approved issues**: `state._load_queue().approved` empty. If non-empty → halt "N issues approved but not run; release them first or /laplace:discard". (Draft issues are fine — they're not committed work.)
  - If all checks pass, execute atomically (best-effort; on any step failure, halt with partial-state report):
    1. Bump `VERSION`, `.claude-plugin/plugin.json` `version`, `.claude-plugin/marketplace.json` `plugins[0].version`.
    2. `git add` the three files + `git commit -m "chore(release): bump <old> -> <new>"`.
    3. `git tag -a v<X.Y.Z> -m "v<X.Y.Z>: <one-line from latest commit subject>"`.
    4. `git push origin main` — this is the policy-authorized push (see below).
    5. `git push origin v<X.Y.Z>`.
  - Every `git` op routed through `policy.check_command` first.
  - Append a release record to `.harness/state/releases.jsonl` (ts, version, prev_version, checks_passed, pushed_at). Audit trail.
- **Policy integration**: `policy.py`'s `require_approval_for: git_push` would block the release push. Two options (PM to pick):
  - **Option A**: release.py calls `policy.check_command` for push; on the expected denial, it proceeds anyway because the human authorized via `/laplace:release` invocation (documented as the approved path, mirrors create-pr). Record the authorization basis in the release log.
  - **Option B**: add a policy allowlist exception for pushes originating from the release command (e.g. a flag `policy.check_command(cmd, authorized_by="release")`). More invasive.
  - **Recommend Option A**: simpler, matches create-pr precedent, no policy.py change.
- `commands/release.md` — imperative wrapper. frontmatter: description "Release a version: validate, bump, commit, tag, push (7-check gate, halt on failure)", argument-hint "<X.Y.Z>", allowed-tools "Bash, Read". Body runs `python3 "$CLAUDE_PLUGIN_ROOT/scripts/release.py" $ARGUMENTS`.
- `skills/release/SKILL.md` — Intent / When to Run / What It Does (7 checks + atomic sequence) / Constraints (invocation=authorization, halt-on-failure, push policy) / Output Format / Failure Modes (one per check). `name: release`.
- README command-surface row + `docs/USAGE.md` row + a "Release workflow" section in USAGE.
- `release.py selftest` — temp git repo: clean pass (full sequence), each of the 8 halt cases (bad format, failing test injected, version sync forced-desync, downgrade, dirty tree, existing tag, remote ahead, pending approved).
- `tests/test_release_unit.py` — one test per halt case + the happy path + the audit-log append.

**Out of Scope:**
- Changelog generation.
- Non-main release branches.
- GPG signing.
- Rollback automation.
- Inferring the version number.

### Acceptance Criteria
- AC-REL-001: `/laplace:release 0.3.1` with a clean repo + passing tests + synced version → bumps 3 files, commits, tags `v0.3.1`, pushes main + tag, appends release log entry. Exit 0.
- AC-REL-002: bad version format (`0.3`, `0.3.1.2`, `v0.3.1`) → halt, exit non-zero, message "expected X.Y.Z", no file/commit/tag/push.
- AC-REL-003: failing test → halt, exit non-zero, message with failing test names, no commit/tag/push.
- AC-REL-004: version sync selfcheck fails (write didn't land) → halt, message shows the 3 current values.
- AC-REL-005: version <= current → halt unless `--force`; message "downgrade 0.3.1 -> 0.3.0; pass --force to confirm".
- AC-REL-006: dirty tree → halt, message includes `git status --porcelain` output.
- AC-REL-007: tag `v<X.Y.Z>` exists → halt, message "tag exists; bump to next or delete tag".
- AC-REL-008: origin/main ahead → halt, message "origin has N new commits; pull/rebase first".
- AC-REL-009: approved queue non-empty → halt, message "N issues approved but not run; release or discard first".
- AC-REL-010: every git op routed through `policy.check_command`; push proceeds as the invocation-authorized path (Option A), authorization basis recorded in release log.
- AC-REL-011: halt is safe — on any check failure, no commit/tag/push occurred; working tree unchanged (the bump writes happen AFTER all checks pass).
- AC-REL-012: release log `.harness/state/releases.jsonl` appended with {ts, version, prev_version, checks_passed: true, pushed_at}; failed attempts append {checks_passed: false, failed_check, reason}.
- AC-REL-013: re-runnable — after a halt + human fix, re-invoking `/laplace:release <same version>` completes from scratch (idempotent checks).
- AC-REL-014: characterization — the CI release workflow still fires on the pushed tag and creates the GitHub Release (unchanged).

### Risks
- **R-1 Policy-authorized push ambiguity**: Option A means release.py pushes despite `require_approval_for: git_push`. If a user runs `/laplace:release` by accident, they've pushed. Mitigation: the 7-check gate is the guardrail (dirty tree / existing tag / pending issues all halt); the invocation itself is the deliberate act. Document in the skill that `/laplace:release` is irreversible-pushing.
- **R-2 Partial-push failure**: main push succeeds, tag push fails (network blip). State: main has the release commit, tag not pushed. Mitigation: release.py attempts tag push; on failure, halt with "main pushed, tag failed; run `git push origin v<X.Y.Z>` manually". Record partial state in release log. Do NOT auto-rollback main (it's already public).
- **R-3 Test command assumption**: check 2 assumes `python3 -m pytest -q` is the test command. For non-py laplace targets (future), this is wrong. v1: hardcode pytest (laplace is Python); config key `release.test_command` deferred.
- **R-4 Fetch in check 7**: `git fetch origin main` is a network op. policy.check_command must allow `git fetch` (it's not push/publish; verify it's not denied). If denied, skip check 7 with a warn (don't block release on a policy-denied fetch).

### Risk / Release Impact
- Risk Level: medium (automates push — irreversible)
- Release Type: minor (new command)
- Security Sensitivity: high (push authorization; the gate is the safety boundary)
