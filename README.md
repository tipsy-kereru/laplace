# Laplace

Laplace is a Claude Code plugin for local AI engineering loop execution. It enforces procedure, not model capability: context before decomposition, local issue state before execution, scoped changes before review, verification before completion, review/security gates before release-candidate, and human approval before irreversible or external side effects.

## Status

Draft. MVP scope: P0-P6. See `specs/SPEC-002-laplace-claude-code-plugin.md` for the full specification.

## Requirements

- [Claude Code](https://claude.com/claude-code) v2.x or later
- Python 3.7+ (stdlib only — Laplace uses `os.replace`, f-strings, and subprocess for `git`)
- `git` on `PATH` (used by the run loop for branch state and PR creation)
- `gh` CLI (only required for `/laplace:create-pr`; must be authenticated via `gh auth login`)

## Installation

Install from the public GitHub repository. Pick one path.

### Path A — Marketplace (recommended)

Add this repository as a plugin marketplace, then install:

```
/plugin marketplace add tipsy-kereru/laplace
/plugin install laplace@laplace
```

Updates are resolved against the marketplace. Bump the `version` field in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, tag a release, and users can `/plugin update laplace`.

### Path B — Direct install (no marketplace)

Install straight from the repository URL:

```
/plugin install tipsy-kereru/laplace
```

Or by full URL:

```
/plugin install https://github.com/tipsy-kereru/laplace
```

### Verify the install

After installing, run the doctor skill from any Claude Code session in your project:

```
/laplace:doctor
```

`doctor` checks the plugin JSON, hooks, Python version, git, and `gh` auth. Then initialize the runtime workspace:

```
/laplace:init
```

This creates `.harness/` (owned by Laplace). Add `.harness/` to your project `.gitignore` if you do not want to commit runtime state.

### Uninstall

```
/plugin uninstall laplace
```

Optionally remove the marketplace:

```
/plugin marketplace remove tipsy-kereru/laplace
```

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
