---
name: freerange
description: Toggle the freerange scope override. Suppresses approval gates for a chosen scope (flow/publish/supply/all) so the loop can run unattended, with a TTL. NOT a security boundary — deny layer (rm -rf /, curl|sh, sudo, cloud CLIs) is never suppressed.
---

# /laplace:freerange

## Intent

Manage the freerange scope override (SPEC-007). Freerange suppresses
Laplace's approval layer for a chosen scope, letting the loop run
unattended through specific gates for a bounded time. It is a
convenience aid, **not a security boundary** (SPEC-002 NG-007).

The model instructs; `scripts/freerange.py` executes the state change
and records the audit entry. The deny layer (`rm -rf /`, `curl|sh`,
`sudo`, `aws`, `gcloud`, `kubectl`) is never suppressed by construction.

## When to Run

- The user invokes `/laplace:freerange on {flow|publish|supply|all} [--ttl HOURS]`.
- The user invokes `/laplace:freerange off` to restore all approval gates.
- The user invokes `/laplace:freerange status` to see the active scope and remaining time.

Do NOT enable freerange to work around a gate you do not understand
(find out why it halts first), and do NOT leave `supply` on between
sessions (the model can expand its own tool surface unattended).

## Scopes

| Scope | Suppresses | Risk |
|---|---|---|
| `flow` | `issue_approval` (draft → approved auto-approve). No external effects. | Low. |
| `publish` | `git_push`, `gh_pr_create`, `npm_publish`. | Medium — irreversible external publish. |
| `supply` | `pip_install`, `npm_install`, `claude_mcp_add`. | High — model expands its own capability surface. |
| `all` | union of flow + publish + supply. | High — autonomous end-to-end except the deny layer. |

`aws`/`gcloud`/`kubectl` are intentionally unsuppressed (cloud production access).

## What It Does

### Step 1: Surface the disclaimer (before `on`)

Before any `on`, surface this to the user verbatim:

> Freerange is a convenience aid, not a security boundary (SPEC-002
> NG-007). A determined agent can defeat it. The deny layer
> (rm -rf /, curl|sh, sudo, aws, gcloud, kubectl) is never suppressed.

### Step 2: Confirm (for `on` only)

Use `AskUserQuestion` with `Cancel (Recommended)` as the **first** option
and `Confirm` as the second. Proceed only on explicit `Confirm`. This is
a documented exception to the "first = the action" convention — for a
safety-bypass, the recommended action is to keep safety on.

For `off` and `status`: no confirmation needed.

### Step 3: Execute

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/freerange.py on <scope> --ttl <hours>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/freerange.py off
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/freerange.py status
```

`$CLAUDE_PLUGIN_ROOT` resolves on both Claude Code and Codex. On Codex
the same command runs; the host sets `CLAUDE_PLUGIN_ROOT` for
compatibility.

Defaults: `--ttl 24` (hours). Hard ceiling: 168 (7 days). Above ceiling
refused with exit 2.

### Step 4: Report

Print the script's stdout verbatim — it includes the scope, expiry
timestamp, and the "not a security boundary" reminder. The audit log
entry is written by `freerange.py` to `.harness/logs/freerange.jsonl`.

## Output

On `on`:
```
freerange ON: scope=flow ttl=8h expires_at=2026-06-27T... (8.0h)
NOTE: freerange is a convenience aid, not a security boundary...
```

On `off`:
```
freerange OFF
```

On `status`:
```
freerange: ON scope=flow remaining=7.9h expires_at=2026-06-27T...
```
or `freerange: off` when no override is active.

## Safety Properties

- **Not a security boundary.** A determined model with Bash can edit
  `policy.py`, write directly to `freerange.json`, or forge
  `approvals.jsonl`. Same tier as all Laplace policy hooks.
- **Deny layer untouched.** The deny layer (`FLAT_DENY_COMMANDS`) is
  never consulted by freerange; it is unreachable on the approval path.
- **TTL-bounded.** Every `on` records an expiry; expired overrides are
  treated as off on the next approval check.
- **Fail-closed.** A malformed or tampered `freerange.json` is treated
  as no override; a `tamper` audit entry is appended.
- **Auditable.** Every on/off/expire/tamper event is appended to
  `.harness/logs/freerange.jsonl`.

## See Also

- `docs/freerange-recipes.md` (en) / `docs/freerange-recipes.kr.md` (ko)
  — practical patterns and anti-recipes.
- `specs/SPEC-007-freerange-scope-override.md` (source repo only, not
  bundled) — full design and limits.
