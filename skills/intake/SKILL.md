---
name: intake
description: Convert a PRD or story document into local draft issues. Parses markdown, creates ISSUE-NNNN.md files with all required schema fields in draft status, registers them in the queue. Does not auto-approve.
---

# /laplace:intake

## Intent

Convert a PRD or story markdown document into local draft issues in `.harness/issues/`. Each generated `ISSUE-NNNN.md` contains all 13 fields from `specs/SPEC-002-laplace-claude-code-plugin.md` §Local Issue Schema, with `Status: draft`. Intake never approves issues — human `/laplace:approve` is always required before queue execution (AC-LP-006).

## When to Run

- After `/laplace:init` has created `.harness/`.
- When a new PRD, story doc, or feature spec is ready to be decomposed into trackable work units.
- Before `/laplace:approve` — there must be draft issues to approve.

## What It Does

1. Invokes the intake script:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/intake.py <prd-path>
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD.

2. **Parsing strategy** (implemented in `scripts/intake.py`):
   - Reads the PRD markdown file.
   - Splits it into work units at top-level (`#` or `##`) headings whose title begins with one of: `Feature:`, `Task:`, `Requirement:`, `Story:`, `Epic:`, `Issue:` (case-insensitive).
   - If no such explicit headings exist, falls back to one issue per `##` section.
   - If the document has no headings at all, creates a single issue with the whole document as `Background`.
   - Assigns sequential `ISSUE-NNNN` ids (zero-padded 4 digits) by scanning existing `.harness/issues/ISSUE-*.md` files and cross-checking `tasks.json`. ID allocation is guarded by a single-writer lock (`ISSUE-INTAKE`) reused from `state.py` so concurrent intakes cannot collide.

3. **Each generated issue file contains all 13 schema fields:**
   - `Issue ID`: `ISSUE-NNNN`
   - `Status`: always `draft` — never auto-approved
   - `Summary`: derived from the section heading (redacted)
   - `Background`: first non-empty paragraph under the heading; `TBD` if absent
   - `Scope`: `In Scope` / `Out of Scope` populated from explicit sub-headings in the PRD if present, else `TBD`
   - `Acceptance Criteria`: bullets extracted from an explicit `Acceptance Criteria` / `AC` section; else `TBD - PM agent to refine`
   - `Technical Notes`: `TBD` (PM/dev agent fills during pm-review)
   - `Test Requirements`: `Unit` / `Integration` / `E2E` / `Regression` / `Manual` sub-bullets, all defaulting to `TBD`
   - `Risk / Release Impact`: `Risk Level: medium`, `Release Type: patch`, `Security Sensitivity: low` (defaults; PM/security refine later)
   - `Routing Metadata`: `Type` inferred from heading keywords (feature/bug/refactor/docs/test/chore/security; default `feature`), `Priority: p2`, `Area` inferred from heading, `Route: pm-review` (always — draft -> approved -> pm-review per state machine)
   - `Source`: document path (relative to repo root), section heading, line range, and a redacted excerpt of the PRD text
   - `Run History`: empty list `[]` initially; append-only across runs

4. **State update** (atomic, via `state.py` helpers):
   - Adds each issue to `.harness/state/tasks.json` with `status: draft`
   - Appends each issue id to `.harness/state/queue.json` `draft` array

5. **Redaction** (G-LP-003): every user-supplied string that is persisted (summary, background, scope, acceptance criteria, area, source document path, raw excerpt) is passed through `redaction.py`'s `redact()` before write. Raw command output is never stored.

## Output Format

Per SPEC-002 §Output Format (Result Template). Example for a 2-section PRD:

```
Laplace result: intake complete

Issue: (none) -> ISSUE-0001, ISSUE-0002

State: (no issues) -> draft

Evidence:
  - ISSUE-0001: .harness/issues/ISSUE-0001.md
  - ISSUE-0002: .harness/issues/ISSUE-0002.md
  - queue.json draft: 2 entries
  - tasks.json: 2 issues

Artifacts:
  - .harness/issues/ISSUE-0001.md
  - .harness/issues/ISSUE-0002.md

Risks:
  - none (draft only; no source files modified; no auto-approval)

Next:
  /laplace:list  (or /laplace:approve ISSUE-0001)
```

## Constraints

- MUST NOT auto-approve. Every generated issue has `Status: draft`; only explicit `/laplace:approve <issue>` moves it to the execution queue (AC-LP-006).
- MUST NOT modify the PRD source file (read-only input).
- MUST redact secrets in the `Source` field and every other persisted field via `redaction.py`.
- MUST treat PRD content as untrusted input — no field value from the PRD is persisted without redaction.
- MUST NOT infer Routing `Route` as anything other than `pm-review`. The state machine fixes the next state after `approved` as `pm-review`.
- MUST NOT run network commands, install dependencies, or create git branches.
- MUST NOT write outside `.harness/`.

## Failure Modes

- **PRD path missing**: prints `PRD not found: <path>` to stderr, exits non-zero. No state change.
- **`.harness/` not initialized**: prints `Laplace is not initialized at <root>. Run /laplace:init first.`, exits non-zero. Recommends `/laplace:init`.
- **PRD has no parseable sections** (no headings): creates a single issue with the whole document body as `Background`, `Summary` set to `(untitled)`. This is not an error — it preserves the input for human triage.
- **ID allocation lock contention**: if another intake is in progress, exits with code 3 and a `intake lock failed` message. Retry after the other intake completes.
- **Parse error on PRD read**: prints a sanitized read error, exits non-zero. No partial state write (issue files + state updates happen inside the lock; a failure before the lock is released rolls back via atomic writes).
