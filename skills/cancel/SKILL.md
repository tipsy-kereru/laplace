---
name: cancel
description: Stop an active Laplace loop safely. Clears active-loop state, records cancellation in issue run history, releases locks. Does not delete branches or artifacts. Full body lands in P6.
---

# /laplace:cancel [issue]

Status: P0 stub. Full implementation lands in phase P6.

## Intent

Safely stop an active loop per SPEC-002 §State Machine exception flow and §Loop Limits terminal states.

## Required Behavior (P6)

- If issue specified: cancel that issue's active run
- If no issue specified: cancel the currently active run (read `.harness/state/runs/`)
- Set issue state to `cancelled`
- Release any lock in `.harness/state/locks/`
- Append `cancelled` transition to issue run history
- Remove active-loop marker from Stop-hook state
- MUST NOT delete branches, patches, or other artifacts
- MUST NOT push, force-push, or modify remote state

## Output

State change (active → cancelled), evidence (lock released, run history appended), next safe action.
