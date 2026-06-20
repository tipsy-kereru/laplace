# PRD: Laplace Self-Improvements (dogfooding findings)

## Context

While dogfooding Laplace to build the Queue Runner feature (9 issues, PRD `docs/prd-run-queue.md`), the loop surfaced several defects and friction points in Laplace itself. This PRD addresses the actionable findings. Two originally-listed findings are out of scope:

- **pre-commit `npm test` failure**: the gate is an external harness commit hook (no `.git/hooks/`, no `.husky/`, no `core.hooksPath` in this repo). Not a Laplace defect. The empty `package.json` is addressed below as a chore.
- **`terminal:<final>` outcome documentation**: already fixed in ISSUE-0006 (review caught it; command mapping corrected). No further work.

## Goals

- Fix intake parsing so PRDs without explicit `Task:` keywords and with `### Scope`/`### Acceptance Criteria` subheadings decompose correctly.
- Add an issue delete/discard command so mis-intaken drafts can be removed cleanly.
- Make the dev phase commit its work to the issue branch automatically, so the orchestrator does not have to hand-commit.
- Correct the "active queue run" terminology to "resumable" across docs/skills.
- Remove the empty `package.json` that triggers external tooling confusion.

## Non-goals

- Changing the queue runner core (shipped in v0.2.0).
- Adding coverage tooling (`pytest --cov`) — deferred.
- Touching the external harness commit hook (out of Laplace's control).

---

## Task: Intake section parsing improvements

Fix the two intake parser defects found during dogfooding: (a) fallback splits every `##` section into an issue, including non-task boilerplate; (b) `### Scope` / `### Acceptance Criteria` subheadings under a task are not extracted, leaving Scope and AC as `TBD`.

### Background
During the queue-runner intake, the first attempt produced 11 junk issues (one per `##` heading: Status, Background, Problem, Goals...) because no `## Task:` keyword headings were present and the fallback fired. Restructuring the PRD with `## Task:` headings fixed it, but the fallback itself is wrong. Separately, even with `## Task:` headings, the `### Scope` and `### Acceptance Criteria` subheadings inside each task were ignored (Scope=AC=`TBD`), forcing the PM phase to recover them from the raw excerpt.

### Scope
**In Scope:**
- `scripts/intake.py` fallback path: when no `## <Keyword>:` headings exist, do NOT emit one issue per `##` section. Instead, treat the whole document as a single issue with the full body as Background (current "no headings" behavior), OR skip recognized boilerplate section names (Status, Background, Problem, Goals, Non-goals, Risks, Open questions, Context, Acceptance criteria at top-level PRD scope). Recommend: if no keyword headings, single issue from whole doc.
- `scripts/intake.py` Scope/AC extraction: under each task section, recognize `### Scope` (with `**In Scope:**`/`**Out of Scope:**` sub-bullets) and `### Acceptance Criteria` (numbered or bulleted) subheadings and populate the issue's Scope and Acceptance Criteria fields instead of `TBD`.
- Extend `intake.py selftest` for both behaviors.

**Out of Scope:**
- Changing the keyword set (`feature/task/requirement/story/epic/issue`).
- Multi-level PRD outline inference beyond `###` subheadings.

### Acceptance Criteria
- AC-SI-001: a PRD with no `## <Keyword>:` headings and multiple generic `##` sections produces exactly ONE issue (whole-doc Background), not N.
- AC-SI-002: a `## Task:` section containing `### Scope` with In/Out bullets and `### Acceptance Criteria` with bullets populates the issue's Scope and AC fields (not `TBD`).
- AC-SI-003: existing intake tests (keyword-path, depends_on) still pass.
- Unit tests for both new behaviors.

### Risk / Release Impact
- Risk Level: medium (intake output shape change)
- Release Type: minor
- Security Sensitivity: low (parser; input redacted as today)

---

## Task: Issue delete/discard command

Add `/laplace:discard <issue>` (draft only) and the `state.py` primitive to remove a mis-intaken draft issue cleanly.

### Background
When intake produced 11 junk drafts, there was no way to delete them. `state.py` has no delete/discard subcommand. The only escape was nuking `.harness/` entirely.

### Scope
**In Scope:**
- `scripts/state.py discard <issue-id>`: legal ONLY from `draft` status. Removes the issue from `tasks.json`, `queue.json` (draft list), and deletes `.harness/issues/<id>.md`. Atomic. Refuses non-draft with exit code 2.
- `commands/discard.md` + `skills/discard/SKILL.md`: imperative command, human-invoked (no auto-discard). Documents the draft-only constraint.
- README command surface + docs/USAGE.md row.

**Out of Scope:**
- Discarding approved/in-progress/review-passed issues (those have run history; discard would lose evidence — out of scope. Use `/laplace:cancel` + manual close).
- Batch discard.

### Acceptance Criteria
- AC-SI-004: `state.py discard ISSUE-NNNN` on a draft removes it from tasks.json, queue.json, and deletes the .md file; atomic (all-or-nothing).
- AC-SI-005: discard of a non-draft issue exits code 2 with a clear message; no state change.
- AC-SI-006: `/laplace:discard <issue>` works as a slash command; doctor recognizes the new skill.
- Unit tests + selftest.

### Risk / Release Impact
- Risk Level: medium (destructive — deletes issue files)
- Release Type: minor
- Security Sensitivity: medium (deletion; draft-only guard is the safety boundary — MUST NOT allow deleting issues with run history)

---

## Task: Dev-phase auto-commit

The dev agent commits its work to the issue branch at the end of the dev phase, so the orchestrator does not hand-commit and the review agent always sees committed artifacts.

### Background
In ISSUE-0001 the dev agent left work uncommitted in the working tree; the review agent flagged it ("branch HEAD equals main, no commits") and the orchestrator had to hand-commit. ISSUE-0002 onward worked around this by instructing the dev agent to commit in the spawn prompt. This task makes the commit a documented, enforced step in the run skill so the workaround is not needed.

### Scope
**In Scope:**
- `skills/run/SKILL.md` Step 3 (Dev phase): add an explicit final sub-step — after test evidence is captured and before advancing to review, the dev agent (or orchestrator on its behalf) commits all dev changes to `laplace/<issue-id>` with a conventional-commit message. The advance to `review` MUST not happen with a dirty tree.
- The run skill documents the commit as mandatory; if the dev agent cannot commit (e.g., not a git repo, BRANCH_SKIPPED), it records that and proceeds (fail-safe, matches existing branch-skip philosophy).
- `skills/run/SKILL.md` note: the orchestrator's spawn prompt to the dev agent MUST include the commit instruction; codify it so it is not ad-hoc.

**Out of Scope:**
- Pushing the branch (still gated behind `/laplace:create-pr`).
- Changing the commit message format beyond conventional commits.

### Acceptance Criteria
- AC-SI-007: run skill Step 3 documents a mandatory commit sub-step before the `in-progress -> review` transition.
- AC-SI-008: characterization test or selftest asserting that after a dev phase the issue branch HEAD is ahead of its base (a commit exists), unless BRANCH_SKIPPED.
- AC-SI-009: the dev agent spawn contract (documented in the skill) includes the commit instruction.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch
- Security Sensitivity: low

---

## Task: Terminology — "active queue run" → "resumable queue run"

Correct the misleading "active queue run" wording. Queue runs are synchronous and always ended before status can be called; the correct concept is "resumable" (outcome startswith `merge-`).

### Background
ISSUE-0007 PM hit a block because "active queue run" presupposes a live process that never exists in the synchronous model. The implementation already uses "resumable" (`_find_resumable_queue_run`). The docs/skills still say "active."

### Scope
**In Scope:**
- `docs/prd-run-queue.md`: replace "active queue run" with "resumable queue run" in AC-QR-018-status and surrounding prose; clarify the synchronous semantics (a queue run is never live between invocations; resumable = halted at a merge- gate).
- `skills/run-queue/SKILL.md`, `skills/status/SKILL.md` (if it references active runs), `README.md` architecture section if it uses the term.

**Out of Scope:**
- Any behavior change (terminology only).

### Acceptance Criteria
- AC-SI-010: no occurrence of "active queue run" in docs/skills/README; replaced with "resumable queue run" + a one-line synchronous-semantics note where it first appears.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch (docs)
- Security Sensitivity: low

---

## Task: Remove empty package.json

Delete the 0-byte `package.json` that causes external tooling (and the external commit hook) to assume a Node project and run `npm test`, which fails.

### Background
`package.json` is 0 bytes and pre-existing. No Node code exists in the repo. External harnesses mis-detect the repo as a Node project.

### Scope
**In Scope:**
- Delete `package.json`.
- Confirm no laplace code references it (grep).

**Out of Scope:**
- Fixing the external harness commit hook (out of Laplace's control).

### Acceptance Criteria
- AC-SI-011: `package.json` removed; `grep -r "package.json" scripts/ skills/ commands/ hooks/` returns no laplace-authored reference.

### Risk / Release Impact
- Risk Level: low
- Release Type: patch (chore)
- Security Sensitivity: low
