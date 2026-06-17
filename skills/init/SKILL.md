---
name: init
description: Initialize the Laplace .harness/ runtime workspace. Creates config, routing rules, agent policy, directory tree, and .gitignore (mixed tracking policy) without modifying source code.
---

# /laplace:init

## Intent

Create the `.harness/` runtime workspace per `specs/SPEC-002-laplace-claude-code-plugin.md` §Runtime State Layout. This command is the one-time bootstrap that prepares Laplace to run issues. It is idempotent.

## When to Run

- First time Laplace is used in a repository.
- After a fresh clone where `.harness/` is not present (it is local runtime state).
- When `.harness/config.yml` or directory tree is missing or corrupt (init will not overwrite existing tracked files; it only creates missing entries).

## What It Does

1. Runs the state engine init command:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/state.py init
   ```

   Optionally pass `--target <repo-root>` to operate outside CWD.

2. The state engine creates the full `.harness/` tree:

   ```
   .harness/
   ├── config.yml
   ├── routing-rules.yml
   ├── agent-policy.yml
   ├── .gitignore
   ├── issues/
   ├── state/
   │   ├── tasks.json
   │   ├── queue.json
   │   ├── approvals.jsonl
   │   ├── profile-snapshot.json
   │   ├── locks/
   │   └── runs/
   ├── memory/
   │   ├── project.md
   │   ├── decisions.md
   │   ├── constraints.md
   │   └── known-failures.md
   ├── logs/
   │   ├── harness.log
   │   ├── agent-runs/
   │   └── test-runs/
   └── artifacts/
       ├── patches/
       ├── pr-drafts/
       ├── reports/
       └── release/
   ```

3. Writes `.harness/.gitignore` enforcing mixed tracking policy per SPEC-002 and QUALITY_GATES P1:
   - Tracked: `config.yml`, `routing-rules.yml`, `agent-policy.yml`, `memory/*.md`
   - Ignored: `state/`, `logs/`, `artifacts/`, `issues/`

4. Detects `.moon-cell/` presence:
   - If absent: prints the SPEC-002 §Moon Cell Integration recommendation verbatim:

     ```
     Moon Cell profile not found.
     Laplace can run with default local policy.
     Recommended: use Moon Cell to generate a project-specific harness profile.
     ```

   - If present: writes a placeholder `.harness/state/profile-snapshot.json` with `status: "not-yet-consumed"`. Full profile snapshot is deferred to P6 `profile.py`.

## Constraints

- MUST NOT modify any source code outside `.harness/`.
- MUST NOT install dependencies, create git branches, push, or run network commands.
- MUST NOT require Moon Cell.
- Loop limits in `.harness/config.yml` are the hard-safety floor; lower-precedence layers cannot weaken them.

## Output Format

Per SPEC-002 §Output Format, report:

1. What state changed: `.harness/` created (or which entries were created if idempotent re-run).
2. What state is current now: `not-initialized -> initialized`.
3. Evidence: tree listing under `.harness/` (paths).
4. Artifacts: paths to `config.yml`, `routing-rules.yml`, `agent-policy.yml`, `.gitignore`.
5. Next safe action: `/laplace:doctor` to verify health, or `/laplace:intake <prd>` if the user is ready to create draft issues.

Example:

```
Laplace result: initialized

Issue: (repository)
State: not-initialized -> initialized

Evidence:
  - .harness/ tree created (27 entries)
  - config.yml written with loop limits
  - .gitignore enforces mixed tracking policy

Artifacts:
  - .harness/config.yml
  - .harness/routing-rules.yml
  - .harness/agent-policy.yml
  - .harness/.gitignore

Risks:
  - none (init does not modify source)

Next:
  /laplace:doctor
```

If `.moon-cell/` is absent, also emit the Moon Cell recommendation block above.
