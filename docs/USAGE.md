# Laplace — Usage Guide

End-to-end walkthroughs with realistic examples. Read the [README](../README.md) first for installation, philosophy, and architecture.

Laplace runs **inside your target project** (the codebase you want Laplace to work on), not inside the `laplace/` plugin repo itself.

---

## Prerequisites

- Laplace plugin installed in Claude Code (`/plugin install laplace@laplace`)
- Working directory = your project root
- `python3`, `git` on `PATH`
- `gh` CLI authenticated (only for `/laplace:create-pr`)

---

## Use case 1 — First-time setup and health check

Goal: install the runtime workspace and verify the plugin is healthy.

```
/laplace:doctor
```

Output (abbreviated):

```
Laplace doctor.

1. plugin.json             pass
2. hooks.json              pass
3. skill frontmatter       pass (9 skills)
4. agent frontmatter       pass (5 agents)
5. state selftest          pass
6. policy selftest         pass
7. redaction selftest      pass
8. python3                 pass (3.11.x)
9. .harness/config.yml     warn (not initialized; run /laplace:init)
10. Moon Cell profile      warn

Overall: PASS WITH WARNINGS

Next:
  /laplace:init
```

Two warnings are expected before init: no `.harness/` and no Moon Cell profile. Initialize the workspace:

```
/laplace:init
```

This creates `.harness/` with config, routing rules, and the state directory tree. Re-run doctor — both warnings resolve (or Moon Cell stays a warning, which is fine by default).

Add `.harness/` to your project `.gitignore` if you do not want to commit runtime state:

```
.harness/
```

---

## Use case 2 — Bug fix: full loop, no blockers

Scenario: a PRD describes a login rate-limit bug. Walk it from PRD to PR.

### Step 1 — Write the PRD

`docs/prd-login-rate-limit.md`:

```markdown
# Bug: brute-force protection missing on login

## Background
The login endpoint has no rate limiting. Failed attempts are unbounded,
enabling credential stuffing.

## Acceptance criteria
- Per-IP login attempts capped at 5 per minute
- Excess attempts return HTTP 429
- Counter backed by Redis with 60s TTL
- Unit tests for the limiter
- No changes to the existing auth schema
```

### Step 2 — Intake: convert PRD into draft issues

```
/laplace:intake docs/prd-login-rate-limit.md
```

Laplace parses the PRD and emits one or more `ISSUE-NNNN` records under `.harness/issues/` in `draft` status. The model clarifies scope during intake if the PRD is ambiguous.

### Step 3 — Review and approve

```
/laplace:status
```

Confirm `ISSUE-0001` sits in the draft queue. Inspect it:

```
/laplace:report ISSUE-0001
```

Verify the scope, acceptance criteria, and risk classification match your intent. This is the **human approval gate** — Laplace never auto-approves.

```
/laplace:approve ISSUE-0001
```

Records the approval in `.harness/state/approvals.jsonl` and moves the issue to the approved queue.

### Step 4 — Run the loop

```
/laplace:run ISSUE-0001
```

The loop:

1. Acquires the issue lock, creates branch `laplace/ISSUE-0001`.
2. **PM phase** — clarifies scope, acceptance criteria, technical notes. Produces `ready` or `blocked`.
3. **Dev phase** — implements the change + tests on the branch, captures test evidence.
4. **Review phase** — independent code review against acceptance criteria.
5. **Security phase** — security dimension review (this change touches auth-adjacent code, so security runs).

Each transition writes evidence to `.harness/state/runs/<run-id>.json`. The loop stops at `review-passed`.

### Step 5 — Check state and logs

```
/laplace:status
/laplace:report ISSUE-0001
```

The report renders sanitized test output, the review verdict, and the security verdict. Secrets are redacted by `scripts/redaction.py` before anything is persisted, so the report is safe to share.

### Step 6 — Create the PR

```
/laplace:create-pr ISSUE-0001
```

Generates a PR draft artifact first, records an approval entry, and opens the GitHub PR **only after explicit human approval** (AC-LP-015). No PR is created silently.

---

## Use case 3 — Feature add that hits a dependency gate

Scenario: the change requires adding a new npm dependency. Dependency additions are a **mandatory human-approval category** — the loop will stop.

### Loop stops at the gate

During the Dev phase, the dev agent recognizes the dependency add. The loop halts at `human-approval-required` rather than installing the package itself. State:

```
/laplace:status
```

```
ISSUE-0002  state: human-approval-required
reason: dependency-add  (mongoose@8.0.0)
```

### Human decides

Review the proposed dependency (license, maintainers, CVE history). If you approve:

```
/laplace:approve ISSUE-0002
/laplace:run ISSUE-0002
```

The loop resumes from where it stopped. If you reject, `/laplace:cancel ISSUE-0002` records the decision and keeps state for later.

---

## Use case 4 — Cancel and resume

Scenario: a run is taking too long, or you notice a scope problem mid-loop. Stop safely.

```
/laplace:cancel ISSUE-0003
```

What cancel does:

- Clears active-loop state and releases the issue lock
- Records the cancellation in the issue run history
- **Does not** delete the branch or any artifacts

State is preserved. To resume:

```
/laplace:run ISSUE-0003
```

The runner detects the existing branch `laplace/ISSUE-0003` and reuses it (idempotent). The loop resumes from the last legal state.

---

## Use case 5 — Blocked issue

Scenario: the PM phase cannot resolve scope because the PRD is internally contradictory.

The loop transitions the issue to `blocked` and ends the run. The run log captures the blocker reason. Surface it:

```
/laplace:status
```

```
ISSUE-0004  state: blocked
blocker: acceptance criteria #2 and #3 are mutually exclusive
```

Resolve the source document or the issue metadata, then re-run:

```
/laplace:run ISSUE-0004
```

---

## Command reference (quick)

| Command | When |
|---|---|
| `/laplace:doctor` | After install, after upgrade, when something behaves oddly |
| `/laplace:init` | Once per project |
| `/laplace:intake <prd>` | Have a PRD/story ready to convert |
| `/laplace:approve <issue>` | You reviewed a draft and want it in the queue |
| `/laplace:run [issue]` | Execute or resume a loop |
| `/laplace:status` | Check queue, active run, blockers |
| `/laplace:report <issue>` | Review sanitized evidence and verdicts |
| `/laplace:cancel [issue]` | Stop a loop safely (keeps state) |
| `/laplace:create-pr <issue>` | Issue is `review-passed`, you want a PR |

---

## Tips

- **Start small.** Run one trivial issue (a doc typo fix) end-to-end to learn the flow before real work.
- **The loop is designed to stop.** Do not expect it to run to completion unattended — every risky category halts for a human.
- **`.harness/` is build state.** Safe to delete (loses history), safe to gitignore.
- **Reports are sanitized.** Secrets are redacted before persistence; safe to paste.
- **Approvals are auditable.** Every `approve` appends to `.harness/state/approvals.jsonl` with a timestamp.
- **Policy cannot be weakened.** If the loop refuses something (force-push, secret read, curl-pipe-sh), that is the hard safety floor, not a bug.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/laplace:*` not found | Plugin not installed or stale marketplace cache | `/plugin marketplace remove tipsy-kereru/laplace` then re-add + reinstall |
| `doctor` reports `state selftest fail` | Python or stdlib issue | `python3 --version` (needs 3.7+) |
| `run` says "not a git repo" | Working dir is not a git repo | `git init` in your project, or run elsewhere |
| `create-pr` says `gh` not authenticated | `gh` missing or logged out | `! gh auth login` |
| Loop keeps stopping at `human-approval-required` | Working as intended — that category needs a human | `/laplace:approve <issue>` then `/laplace:run <issue>` |
