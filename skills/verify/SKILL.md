---
name: verify
description: Read-only pre-approve quality gate. Checks all draft issues against the source PRD in one pass — field completeness, source-traceability, PRD coverage, AC traceability, depends_on consistency, duplicate AC. Per-issue pass/warn/fail table + PRD coverage matrix.
---

# /laplace:verify

## Intent

One read-only pass that catches the mechanical defects intake produces (TBD
fields, mis-parsed subheadings, coverage gaps, broken `depends_on`, duplicate
AC) **before** the human `/laplace:approve` gate. Reduces, does NOT replace,
approve. Per ISSUE-0001 / `docs/prd-verify-gate.md`.

## When to Run

- After `/laplace:intake`, before `/laplace:approve`.
- After editing a draft `.harness/issues/ISSUE-*.md` by hand, to confirm
  coverage / traceability still hold.
- Any time the user wants a structural sanity check on the draft pool.

## What It Does

1. Invokes the read-only verify command:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/verify.py <prd-path>
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD.

2. Reads (no writes):
   - The named PRD markdown file.
   - `.harness/issues/ISSUE-*.md` (draft status only).
   - `.harness/state/tasks.json` (for `all_issue_ids`, used by `depends_on`).
   - Re-uses `intake._split_sections` to re-extract PRD task sections.

3. Runs six checks, each at warn or fail level:

   | Code       | Check                  | Level | Trigger |
   |------------|------------------------|-------|---------|
   | AC-VRF-002 | Field completeness     | warn  | Summary / Background / Scope In+Out / AC is TBD or empty |
   | AC-VRF-003 | Source traceability    | fail  | Source.Section not in PRD |
   | AC-VRF-003 | Source traceability    | warn  | Source.Lines out of bounds or malformed |
   | AC-VRF-004 | PRD coverage           | warn  | PRD task section with no matching issue (orphan section) |
   | AC-VRF-004 | PRD coverage           | warn  | Issue Source.Document != verified PRD (orphan issue) |
   | AC-VRF-005 | AC traceability        | warn  | AC bullets share no significant token with matched PRD section body |
   | AC-VRF-005 | depends_on consistency | fail  | depends_on ref does not match any issue id |
   | AC-VRF-006 | Duplicate AC           | warn  | Pairwise Jaccard over significant-token sets > 0.8 |

4. Prints:
   - Per-issue table: `ISSUE-NNNN: PASS|WARN|FAIL` + each finding line.
   - PRD coverage matrix: `section (lines X-Y) <- ISSUE-NNNN` or `ORPHAN`.
   - Cross-issue: broken deps + duplicate pairs.
   - Verdict: `PASS` (0 fail, 0 warn) / `WARN` / `FAIL`.

## Constraints

- MUST NOT modify any state (read-only, AC-VRF-007). No writes to `.harness/`,
  `tasks.json`, `queue.json`, or issue files.
- MUST NOT transition any issue state (AC-VRF-010). Does NOT call
  `/laplace:approve`. Approve stays human.
- MUST NOT import or call any state-mutation primitive (`_save_*`,
  `_set_issue_state`, `_atomic_write_*`, `acquire_lock`, `cmd_approve`,
  `cmd_transition`, `cmd_run_start`).
- MUST NOT read `.env`, `.ssh/`, `.aws/`, `secrets/**`, or any path on the
  policy deny list.
- MUST NOT execute network commands.
- Re-runnable (idempotent) — safe to invoke repeatedly.
- Non-draft issues are out of scope (verify is a pre-approve gate).

## Output Format

See §What It Does above. Exit codes: `0` clean (or warn-only) / `1` any fail /
`2` usage error (PRD missing, not initialized).

## Failure Modes

- If `.harness/` is missing: exit `2`, print
  `Laplace is not initialized. Run /laplace:init first.`
- If the PRD path is missing: exit `2`, print `PRD not found: <path>`.
- If a draft `.md` fails to parse: verify prints a WARN line and skips that
  issue (does not crash the whole pass).
- If no draft issues exist: per-issue table reports `(no draft issues)`;
  verdict may still be PASS.
- AC traceability is a heuristic (`warn`-level). False positives are expected
  and the human dismisses them at approve (PRD R-1).
