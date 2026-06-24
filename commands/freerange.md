---
description: Toggle freerange scope override (suppress approval gates for flow/publish/supply/all)
argument-hint: "[on|off|status] [scope] [--ttl HOURS]"
allowed-tools: Bash, Read
---

Manage the freerange scope override (SPEC-007). Freerange suppresses
Laplace's approval layer for a chosen scope. It is a convenience aid,
NOT a security boundary (SPEC-002 NG-007).

Usage:
- `/laplace:freerange on {flow|publish|supply|all} [--ttl HOURS]`
- `/laplace:freerange off`
- `/laplace:freerange status`

Run the corresponding subcommand via `python3 $CLAUDE_PLUGIN_ROOT/scripts/freerange.py`.

Before enabling, surface this disclaimer to the user verbatim:

> Freerange is a convenience aid, not a security boundary (SPEC-002
> NG-007). A determined agent can defeat it. The deny layer
> (rm -rf /, curl|sh, sudo, aws, gcloud, kubectl) is never suppressed.

For `on`, use AskUserQuestion to confirm with `Cancel (Recommended)` as
the first option. Proceed only on explicit confirmation.
