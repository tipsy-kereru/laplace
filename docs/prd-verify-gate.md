# PRD: `/laplace:verify` — pre-approve quality gate

## Status
Draft — for `/laplace:intake`.

## Context

`/laplace:intake` converts a PRD into draft issues, but the conversion is mechanical (keyword/regex parsing) and produces TBD fields, mis-parsed subheadings, or coverage gaps. Today the human catches these at `/laplace:approve` time — one issue at a time, by reading each `.md`. This is where intake defects (the `### Scope` miss, the per-`##` junk split — both hit this session) reach the human instead of being caught structurally.

`/laplace:approve` remains a human gate (PRD intent / risk judgment / decomposition quality are not auto-checkable). But the **mechanical** layer — coverage, field completeness, source-traceability, cross-issue consistency — can and should be auto-verified in one pass before the human looks at anything.

## Problem

- Intake defects surface late (at approve) and one issue at a time.
- No mechanism answers: "did intake cover every PRD task? Are required fields populated? Does each AC trace to the PRD? Any duplicate or orphan?"
- Re-intake (the fix) is expensive; an early verify report points exactly at what to fix.

## Goals

- One command `/laplace:verify [prd-path]` that checks ALL draft issues against the source PRD in a single pass.
- Catches, before approve: missing fields (TBD), coverage gaps (PRD task sections with no issue), orphan issues (issue Source pointing outside PRD), broken `depends_on` refs, duplicate AC across issues.
- Produces a prioritized per-issue pass/warn/fail report + a PRD coverage matrix.
- Read-only (no state mutation). Idempotent (re-runnable after draft edits).
- Reduces, does NOT replace, the human approve gate.

## Non-goals

- Replacing `/laplace:approve` — semantic / risk / decomposition judgment stays human.
- Auto-fixing drafts (verify reports; the human or re-intake fixes).
- Verifying non-draft issues (approved+ have run history; verify is a pre-approve gate).
- Coverage tooling (`pytest --cov`) — unrelated, still out of scope.
- PRD-vs-reality check (verify checks issue-vs-PRD, not whether the PRD itself is correct).

---

## Task: verify command + state primitive + skill + docs

### Background
The verify gate is a single read-only pass over draft issues + the source PRD. It composes existing intake parsing primitives (re-extract PRD sections) with new checks (coverage, field, traceability, cross-issue).

### Scope
**In Scope:**
- `scripts/verify.py` with `cmd_verify(args)` — reads the PRD path (positional, or auto-derived from draft issues' `Source` field), loads all draft issues, runs the check suite, prints the report.
- Re-use `intake._split_sections` / `_HEADING_KEYWORDS` to re-extract PRD task sections for coverage matching (do not duplicate the parser).
- Checks (all read-only):
  1. **Field completeness**: each draft issue's Summary, Background, Scope (In/Out), Acceptance Criteria are non-TBD and non-empty.
  2. **Source-traceability**: each issue's `Source.Document` + `Source.Section` + `Source.Lines` resolve to a real span in the PRD file (warn if line range out of bounds; fail if section not found).
  3. **PRD coverage**: every `## <Keyword>:` task section in the PRD maps to ≥1 draft issue whose Source.Section equals that heading. Report orphan PRD sections (no issue) and orphan issues (Source outside any PRD section).
  4. **AC traceability**: each issue's AC bullets share at least one significant token with the matched PRD section body (warn-level — keyword overlap is a heuristic, not a proof).
  5. **depends_on consistency**: every `depends_on` ref exists as a draft issue (already enforced at approve; verify surfaces it earlier).
  6. **Duplicate AC**: flag when two issues' AC bullet text matches > 80% (warn).
- `state.py` — no new primitive required (verify is a standalone script); only registration if the CLI wants a `state.py verify` alias (optional).
- `commands/verify.md` — imperative wrapper, runs `python3 "$CLAUDE_PLUGIN_ROOT/scripts/verify.py" $ARGUMENTS`, prints the report.
- `skills/verify/SKILL.md` — Intent / When to Run / What It Does / Constraints / Output Format / Failure Modes. `name: verify`.
- README command-surface row + `docs/USAGE.md` row.
- `verify.py selftest` — temp PRD + drafts with seeded defects (TBD field, orphan section, broken line range, broken dep ref, duplicate AC) → assert each is detected; clean case → all pass.

**Out of Scope:**
- Auto-fix (re-intake) — verify only reports.
- Approve integration (verify does not block approve; it's an advisory pre-gate the human chooses to run).
- Non-draft issues.

### Acceptance Criteria
- AC-VRF-001: `/laplace:verify docs/prd-X.md` checks every draft issue whose Source.Document == that PRD, in one pass, printing a per-issue pass/warn/fail table + a PRD coverage matrix.
- AC-VRF-002: detects TBD/empty required fields (Summary, Background, Scope In+Out, AC) per issue — `warn`.
- AC-VRF-003: detects Source.Section not in PRD → `fail`; Source.Lines out of bounds → `warn`.
- AC-VRF-004: detects PRD `## Task:` sections with no matching issue (orphan) → `warn`; issues with Source outside any PRD section → `warn`.
- AC-VRF-005: detects `depends_on` refs to non-existent issues → `fail`.
- AC-VRF-006: detects duplicate AC (>80% text overlap across two issues) → `warn`.
- AC-VRF-007: read-only — no writes to `.harness/`, tasks.json, queue.json, or issue files. Re-runnable (idempotent).
- AC-VRF-008: doctor recognizes the new skill; `/laplace:verify` works as a slash command.
- AC-VRF-009: `verify.py selftest` PASS; unit tests in `tests/test_verify_unit.py` covering each defect class + the clean case.
- AC-VRF-010: does NOT transition any issue state; does NOT call `state.py approve`. Approve stays human.

### Risks
- **R-1 AC-traceability false positives**: keyword-overlap heuristic may warn on legitimately distinct AC that happen to share terms. Keep it `warn` (not `fail`) and document the heuristic; the human dismisses false positives at approve.
- **R-2 Coverage matching by heading string**: rename/typo between PRD heading and issue Source.Section would falsely flag orphan. Match on normalized heading text (case, whitespace, trailing punctuation). Document.
- **R-3 Multi-PRD repos**: if drafts exist from multiple PRDs, verify must scope to the named PRD (or to drafts whose Source.Document matches) — never cross-PRD. The PRD path arg resolves this; if absent and multiple PRDs are referenced, verify exits with a message asking for the PRD path.

### Risk / Release Impact
- Risk Level: low (read-only, advisory)
- Release Type: minor (new command + skill)
- Security Sensitivity: low (read-only; PRD content already redacted at intake, verify does not persist anything)

---

## Open questions (PM phase)

- Should verify optionally take `--all` to check drafts across every referenced PRD (vs requiring a single PRD path)? Recommend: v1 requires a PRD path (explicit); `--all` deferred.
- Should verify be wired into intake as a post-step (auto-run after intake)? Recommend: no — keep them separate; intake already prints "Next: /laplace:approve", can add "consider /laplace:verify first" in its output without coupling.
- AC-duplicate threshold (80%?): make it a config key (`verify.duplicate_ac_threshold`, default 0.8) or hardcode? Recommend: hardcode in v1; config if users complain.
