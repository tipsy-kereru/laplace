---
description: Check Laplace plugin health — manifest, hooks, config, tooling, Moon Cell profile
argument-hint: ""
allowed-tools: Bash, Read, Grep, Glob
---

Run the Laplace doctor diagnostic now and report results. Do not ask for confirmation — this is read-only.

The plugin root is available as `$CLAUDE_PLUGIN_ROOT` in your shell. Use it directly in Bash commands.

Execute each check, capture pass/warn/fail, and print the report in this exact format:

```
Laplace doctor.

1. plugin.json             <pass|warn|fail>
2. hooks.json              <...>
3. skill frontmatter       <...> (N skills)
4. agent frontmatter       <...> (N agents)
5. state selftest          <...>
6. policy selftest         <...>
7. redaction selftest      <...>
8. python3                 <...> (version)
9. .harness/config.yml     <...>
10. Moon Cell profile      <...>

Overall: <PASS | PASS WITH WARNINGS | FAIL>

Next:
  <recommended next command>
```

Checks (run all, do not abort on failure):

1. Plugin manifest: `python3 -c "import json; d=json.load(open('$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json')); assert d.get('name') and d.get('version') and d.get('description')"`. `pass` if exit 0.
2. Hooks manifest: `python3 -c "import json; json.load(open('$CLAUDE_PLUGIN_ROOT/hooks/hooks.json'))"`. `pass` if exit 0.
3. Skill frontmatter: for each `$CLAUDE_PLUGIN_ROOT/skills/*/SKILL.md`, confirm it starts with `---` and frontmatter has `name` and `description`, and `name` equals the parent directory name.
4. Agent frontmatter: for each `$CLAUDE_PLUGIN_ROOT/agents/*.md`, confirm it starts with `---` and has `name` and `description`.
5. State engine: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/state.py" selftest`. `pass` if exit 0.
6. Policy engine: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/policy.py" selftest`. `pass` if exit 0.
7. Redaction engine: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/redaction.py" selftest`. `pass` if exit 0.
8. Python: `python3 --version`. `pass` if 3.7+.
9. `.harness/config.yml`: `pass` if present with loop limits, `warn` if missing (recommend `/laplace:init`).
10. Moon Cell: `pass` if `.moon-cell/` present, else `warn` and print:
   ```
   Moon Cell profile not found.
   Laplace can run with default local policy.
   Recommended: use Moon Cell to generate a project-specific harness profile.
   ```

MUST NOT modify any state. MUST NOT run anything on the policy deny list (no sudo, ssh, network). Report every check even if one fails.
