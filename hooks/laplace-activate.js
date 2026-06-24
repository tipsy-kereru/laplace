#!/usr/bin/env node
// SPEC-008: Laplace SessionStart activation hook (Node, cross-platform).
//
// Mirrors ponytail-activate.js portability: pure Node, no sh dependency,
// so it fires identically on Claude Code and Codex (macOS, Linux, Windows).
//
// Reads .harness/ state from the project root and emits a compact context
// summary that the host injects into the session. Fail-open: any read
// error exits 0 with no context (the harness is still usable; this hook
// only adds convenience context).
//
// Hook contract: stdout JSON with
//   { "hookSpecificOutput": { "hookEventName": "SessionStart",
//                             "additionalContext": "<markdown>" } }
//
// $CLAUDE_PROJECT_DIR or cwd is the project root.

'use strict';

const fs = require('fs');
const path = require('path');

function projectRoot() {
  if (process.env.CLAUDE_PROJECT_DIR) return process.env.CLAUDE_PROJECT_DIR;
  return process.cwd();
}

function readJson(p) {
  try {
    const raw = fs.readFileSync(p, 'utf8');
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

function fmtRemaining(expiresAt) {
  const ms = (expiresAt - Date.now() / 1000) * 1000;
  if (ms <= 0) return 'expired';
  const h = ms / 3600000;
  if (h >= 1) return h.toFixed(1) + 'h';
  return (ms / 60000).toFixed(0) + 'm';
}

function summarize() {
  const root = projectRoot();
  const harness = path.join(root, '.harness');
  if (!fs.existsSync(harness)) return null;

  const lines = [];
  lines.push('Laplace harness active under `.harness/`. Procedure: context before decomposition, scoped changes, evidence before claim, stop at approval gates. See AGENTS.md.');

  // Queue counts from tasks.json
  const tasks = readJson(path.join(harness, 'state', 'tasks.json'));
  if (tasks && typeof tasks === 'object') {
    const counts = {};
    let activeRun = null;
    for (const [id, rec] of Object.entries(tasks)) {
      const st = (rec && rec.status) || 'unknown';
      counts[st] = (counts[st] || 0) + 1;
      if (rec && rec.run_id && rec.status &&
          ['pm-review', 'ready-for-dev', 'in-progress', 'review',
           'needs-fix', 'security-review', 'cost-review'].includes(rec.status)) {
        activeRun = activeRun || id;
      }
    }
    const summary = Object.entries(counts)
      .map(([k, v]) => `${k}=${v}`).join(', ');
    if (summary) lines.push(`Issues: ${summary}`);
    if (activeRun) lines.push(`Active run: ${activeRun}`);
  }

  // Freerange status
  const fr = readJson(path.join(harness, 'state', 'freerange.json'));
  if (fr && fr.enabled === true && fr.scope &&
      typeof fr.expires_at === 'number' && fr.expires_at > Date.now() / 1000) {
    lines.push(`Freerange ON: scope=${fr.scope}, ${fmtRemaining(fr.expires_at)} remaining. NOT a security boundary.`);
  }

  // Next-action hint
  lines.push('Next: `/laplace:status` for detail, `/laplace:run <ISSUE>` to drive one loop.');

  return lines.join('\n');
}

function main() {
  const ctx = summarize();
  if (!ctx) {
    // No harness — emit nothing, exit clean.
    process.exit(0);
  }
  const out = {
    hookSpecificOutput: {
      hookEventName: 'SessionStart',
      additionalContext: ctx,
    },
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

main();
