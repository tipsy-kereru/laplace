---
description: Release a version: 8-check gate, bump 3 files, commit, tag, push (halt on failure)
argument-hint: "<X.Y.Z>"
allowed-tools: Bash, Read
---

Release a Laplace version. The 8-check gate runs first (branch, format, tests, sync, semver, tree-clean, tag-absent, remote-not-ahead, no-pending-approved); on any failure the command halts with a resolution message and no side effects. On all-pass, it atomically bumps `VERSION` + `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`, commits, tags `v<X.Y.Z>`, pushes main, and pushes the tag.

Invocation of `/laplace:release` IS the authorization for the push (Option A, mirrors `/laplace:create-pr`). Push is irreversible; the 8-check gate is the guardrail.

Run: `python3 "$CLAUDE_PLUGIN_ROOT/scripts/release.py" $ARGUMENTS`

Optional `--target <repo-root>` operates outside CWD. `--force` relaxes the downgrade (check 4) and pending-approved (check 8) checks ONLY; it NEVER skips format, tests, sync, tree-clean, tag-absent, or remote checks.
