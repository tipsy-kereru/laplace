---
description: Show current Laplace harness state — queue counts, active run, next safe action
argument-hint: ""
allowed-tools: Bash, Read
---

Report the current Laplace harness state now. Read-only — do not modify anything.

Run: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/state.py" status`

If `.harness/` does not exist, report:
```
Laplace not initialized in this project.
Run /laplace:init first.
```

If it exists, print the state engine's output verbatim (queue counts, active run id and phase if any, current issue states, and the recommended next action). Do not ask for confirmation.
