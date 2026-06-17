---
name: laplace-report
description: Generate or show an issue report. Produces sanitized test/review/security reports and patch/PR-draft artifacts. Read-only unless generating artifacts into .harness/artifacts/. Full body lands in P6/P7.
---

# /laplace:report <issue>

Status: P0 stub. Full implementation lands in phase P6 (partial) and P7 (full).

## Intent

Produce or display an issue report per SPEC-002 §Output Format.

## Required Behavior (P6 partial)

- Read issue file and run history
- Aggregate evidence from `.harness/logs/test-runs/`, `.harness/logs/agent-runs/`
- Apply redaction (`scripts/redaction.py`) to every field
- Write report to `.harness/artifacts/reports/<issue-id>.md`
- Print report using SPEC-002 §Result Template

## Output Format

```
Laplace result: <outcome>

Issue: <issue-id>
State: <from> -> <to>

Evidence:
  - <check>: <observed result>

Artifacts:
  - <path>

Risks:
  - <risk or none>

Next:
  <one concrete next action>
```

## Constraints

- MUST NOT include raw command output
- MUST NOT include unredacted secrets, tokens, keys, URLs with credentials
- MUST NOT create external side effects (no PR, no push, no publish)
