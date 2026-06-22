---
description: Verify draft issues against the source PRD (coverage, fields, traceability, duplicates)
argument-hint: "[prd-path]"
allowed-tools: Bash, Read
---

Run the read-only verify gate. No state mutation; approve stays human.

Run: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/verify.py" $ARGUMENTS`

Print the report verbatim (per-issue PASS/WARN/FAIL table + PRD coverage matrix + cross-issue + verdict). Do not modify anything.
