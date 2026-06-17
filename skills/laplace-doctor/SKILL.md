---
name: laplace-doctor
description: Check Laplace plugin health: plugin files, hooks bindings, config presence, test command availability, Moon Cell profile status. Read-only diagnostic.
---

# /laplace:doctor

## Intent

Read-only diagnostic that verifies the Laplace plugin is correctly installed and ready to run. Per `specs/SPEC-002-laplace-claude-code-plugin.md` AC-LP-003, doctor MUST report default-policy mode and the Moon Cell recommendation when `.moon-cell/` is absent.

## When to Run

- After `/laplace:init` to verify health.
- After upgrading or reinstalling the plugin.
- When a hook or skill behaves unexpectedly.
- Before invoking `/laplace:run` on a real issue.

## Checklist

The diagnostic walks each item and reports `pass`, `warn`, or `fail` per check, plus an overall status. All checks are read-only.

1. Plugin manifest parseable:
   ```
   python3 -c "import json; json.load(open('${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json'))"
   ```
   `pass` if JSON loads and contains `name`, `version`, `description`.

2. Hooks manifest parseable:
   ```
   python3 -c "import json; json.load(open('${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json'))"
   ```
   `pass` if JSON loads. `warn` if any hook entry references a script that does not exist.

3. Skill frontmatter parseable (for each `skills/*/SKILL.md`):
   - File exists and starts with `---` YAML frontmatter.
   - Frontmatter contains `name` and `description`.
   - `name` matches the parent directory name.

4. Agent frontmatter parseable (for each `agents/*.md`):
   - File exists and starts with `---` YAML frontmatter.
   - Frontmatter contains `name` and `description`.

5. State engine available:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/state.py selftest
   ```
   `pass` if exit code 0.

6. Policy engine available:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/policy.py selftest
   ```
   `pass` if exit code 0.

7. Redaction engine available:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/redaction.py selftest
   ```
   `pass` if exit code 0.

8. Python 3 available:
   ```
   python3 --version
   ```
   `pass` if version is 3.7 or higher (state.py uses only stdlib but relies on f-strings and `os.replace`).

9. `.harness/config.yml` present (post-init only):
   - `pass` if `.harness/config.yml` exists and contains the G-LP-004 loop limits.
   - `warn` if `.harness/` is missing — instruct the user to run `/laplace:init`.

10. Moon Cell profile status:
    - If `.moon-cell/` is present: `pass`, report `Moon Cell profile detected. Snapshot is managed by profile.py (P6).`.
    - If `.moon-cell/` is absent: `warn`, print the SPEC-002 §Moon Cell Integration recommendation verbatim:

      ```
      Moon Cell profile not found.
      Laplace can run with default local policy.
      Recommended: use Moon Cell to generate a project-specific harness profile.
      ```

## Constraints

- MUST NOT modify any state.
- MUST NOT run any command on the policy deny list (no `sudo`, `ssh`, network calls, etc.).
- MUST NOT require Moon Cell to pass — Moon Cell absence is a `warn`, not a `fail`.
- Failure on any single check does not abort the diagnostic; all checks run and an aggregate status is reported.

## Output Format

Aggregate status is one of:

- `overall: PASS` — all checks passed.
- `overall: PASS WITH WARNINGS` — no failures, at least one warning.
- `overall: FAIL` — at least one check failed.

Example output:

```
Laplace doctor.

1. plugin.json                pass
2. hooks.json                 pass
3. skill frontmatter          pass (6 skills)
4. agent frontmatter          pass (5 agents)
5. state selftest             pass
6. policy selftest            pass
7. redaction selftest         pass
8. python3                    pass (3.11.x)
9. .harness/config.yml        warn (not initialized; run /laplace:init)
10. Moon Cell profile         warn

Moon Cell profile not found.
Laplace can run with default local policy.
Recommended: use Moon Cell to generate a project-specific harness profile.

Overall: PASS WITH WARNINGS

Next:
  /laplace:init
```
