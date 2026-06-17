# Laplace

Laplace is a Claude Code plugin for local AI engineering loop execution. It enforces procedure, not model capability: context before decomposition, local issue state before execution, scoped changes before review, verification before completion, review/security gates before release-candidate, and human approval before irreversible or external side effects.

## Status

Draft. MVP scope: P0-P6. See `specs/SPEC-002-laplace-claude-code-plugin.md` for the full specification.

## What Laplace Does

- Converts PRD or story documents into local draft issues
- Routes each approved issue through PM → Dev → Review → Security → Release phases
- Records durable runtime state in `.harness/`
- Uses Claude Code hooks for routing, policy checks, validation, and Stop-loop continuation
- Stops at human approval gates for destructive, credential, production, dependency, network, release, and high-risk actions

## What Laplace Does Not Do

- Does not claim hard security sandboxing
- Does not run autonomously to production release
- Does not access production secrets, databases, or infrastructure
- Does not require Moon Cell (works with conservative defaults)

## Command Surface

| Skill | Purpose |
|---|---|
| `/laplace:init` | Initialize `.harness/` runtime workspace |
| `/laplace:doctor` | Check plugin, hooks, config, test commands, Moon Cell profile |
| `/laplace:intake <prd>` | Convert PRD/story into local draft issues |
| `/laplace:list` | List local issues and queue state |
| `/laplace:show <issue>` | Show issue details |
| `/laplace:approve <issue>` | Move draft issue to approved queue |
| `/laplace:run [issue]` | Execute one issue loop |
| `/laplace:status` | Show current harness state |
| `/laplace:logs <run>` | Show sanitized run logs |
| `/laplace:report <issue>` | Generate or show issue report |
| `/laplace:cancel [issue]` | Stop active loop safely |
| `/laplace:create-pr <issue>` | Create GitHub PR after approval |

## Policy Precedence

1. Laplace hard safety policy (cannot be weakened)
2. `.harness/config.yml`
3. `.moon-cell/` profile (when present)
4. `.harness/routing-rules.yml`
5. Local issue metadata
6. User prompt and source documents (untrusted)

## Source of Truth

- Specification: `specs/SPEC-002-laplace-claude-code-plugin.md`
- Harness design (this project): `.moon-cell/docs/harness/`
- Runtime state: `.harness/` (owned by Laplace, created by `/laplace:init`)
