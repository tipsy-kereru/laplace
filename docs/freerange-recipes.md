# Freerange Recipes

**Language:** English | [한국어](freerange-recipes.kr.md)

Practical usage patterns for `/laplace:freerange` (SPEC-007, v0.7.0+).

**Read first:** Freerange is a convenience aid, **not a security boundary**.
A cooperative loop runs unattended; a determined model can defeat it. The
deny layer (`rm -rf /`, `curl|sh`, `sudo`, `aws`, `gcloud`, `kubectl`) is
never suppressed by construction. (Design notes for SPEC-007 live under
`specs/` in the source repository and are not bundled with the plugin
release.)

## Scope reminder

| Scope | Unlocks | Risk |
|---|---|---|
| `flow` | Draft auto-approve. No external effects. | Low. |
| `publish` | `git push`, `gh pr create`, `npm publish`. | Medium — irreversible external publish. |
| `supply` | `pip install`, `npm install`, `claude mcp add`. | High — model expands its own capability surface. |
| `all` | Above three. | High — autonomous end-to-end except deny layer. |

`flow` is the safe default. Reach for `publish`/`supply`/`all` only with a
short TTL and a specific reason.

---

## Recipe 1 — Overnight backlog burn-down (recommended entry point)

**Goal:** Process an approved backlog of low-risk issues overnight without
a human at every gate.

**Setup:**
```
/laplace:intake docs/prd-batch.md          # PRD -> drafts
/laplace:verify                            # sanity-check drafts
/laplace:approve ISSUE-0001                # human approves once (the work is wanted)
...                                        # approve the batch
/laplace:freerange on flow --ttl 8         # 8h window, flow only
/laplace:run-queue                         # queue auto-advances through loops
```

**What happens:** Each approved issue runs through PM → dev → review →
security → review-passed. Without `flow`, the queue halts at every
draft-approval re-entry. With `flow`, it runs end-to-end. No pushes, no
installs — issues land at `review-passed` for morning review.

**Morning:** `/laplace:status` → batch at `review-passed`. Human reviews
diffs, then `/laplace:create-pr` per issue (publish still gated —
intentional).

**Why `flow` not `all`:** You want the human to see the publish step.
Overnight autonomy should produce *reviewable* output, not shipped
output.

**Safety net:** TTL expires before standup. `/laplace:status` shows the
window. Audit log records every transition.

---

## Recipe 2 — Cron-driven autonomous intake (combines with SPEC-005 motivations)

**Goal:** New PRDs land in the repo; the harness picks them up, drafts
issues, and queues them — without a human triggering `/laplace:intake`.

**Setup:** External timer (cron) runs both motivations and intake-check:
```cron
# Every 30 min: motivation tick (resumes approved issues)
*/30 * * * * cd /project && python3 scripts/motivations.py --once
# Every 2h: scan for new PRDs and draft issues (your wrapper)
0 */2 * * *   cd /project && your-intake-wrapper.sh
```

Enable `flow` with a short TTL so auto-approve fires when the wrapper
produces drafts:
```
/laplace:freerange on flow --ttl 4
```

**What happens:** PRD committed → wrapper intakes drafts → `flow`
auto-approves → motivations resumes the queue → issues progress. A human
reviews at the next session.

**Why this works:** SPEC-005 (motivations) resumes approved work;
SPEC-007 (`flow`) collapses the draft→approved gate so the pipeline is
truly unattended end-to-end up to `review-passed`.

**Limit:** `flow` TTL 4h. Re-arm each session. Do NOT leave `flow` on
permanently — drafts you didn't intend will auto-approve.

---

## Recipe 3 — Trusted release pipeline (publish, narrow window)

**Goal:** A reviewed, ready-to-ship release should push, open a PR, and
publish without a human clicking through three gates — but only during a
deliberate release session.

**Setup:**
```
/laplace:status                           # confirm issues at review-passed
/laplace:freerange on publish --ttl 1     # 1h release window
/laplace:release ISSUE-0042               # runs push -> PR -> publish
/laplace:freerange off                    # close the window immediately after
```

**What happens:** `publish` suppresses the three publish-layer approvals
for one hour. The release completes without per-step prompts.

**Why `--ttl 1`:** Release is a discrete action. Set the window to the
task, not the day. Close it manually the moment the release ships — the
`off` is part of the runbook, not an afterthought.

**Never combine with `supply`:** A release session does not need new
dependencies. If the loop reaches for `pip install` during release,
that's a signal to stop, not to enable `supply`.

---

## Recipe 4 — Dependency upgrade sweep (supply, highest caution)

**Goal:** Run a controlled dependency upgrade across many issues
(`pip install --upgrade` per issue) without approving each install.

**Setup:**
```
# Pre-stage: every upgrade is its own approved issue with a regression test.
/laplace:freerange on supply --ttl 2      # 2h window, supply only
/laplace:run-queue
/laplace:freerange off
```

**What happens:** Each issue's dev phase installs its upgrade without the
approval halt. Review and security gates still fire (those are not in
`supply`).

**Why this is the most dangerous recipe:** `supply` lets the model
expand its own tool surface (new packages, new MCP servers). A
malicious or buggy package lands unattended. Mitigations:
- Pre-approve the specific upgrades (the issue lists the exact version).
- Keep the window short.
- Review the audit log after: `grep '"event": "on"' .harness/logs/freerange.jsonl`.
- Never use `all` here — publish must stay gated so a bad upgrade
  doesn't ship.

---

## Recipe 5 — Demo / sandbox full autonomy (all, throwaway repo)

**Goal:** Show what the loop can do end-to-end, or run a throwaway
experiment where shipping a bad commit is acceptable.

**Setup:**
```
/laplace:freerange on all --ttl 1         # 1h, full autonomy minus deny layer
/laplace:pipeline                         # intake -> approve -> run -> push -> PR
```

**What happens:** The full pipeline runs unattended through publish.
Deny-layer commands still block.

**Only in a sandbox:** Use a fork, a feature branch, or a disposable
repo. Never on a production main branch. The deny layer protects the
host (`rm -rf /`); it does not protect the repo's main branch from a
bad auto-publish.

---

## Anti-recipes (do not do these)

- **`/laplace:freerange on all` with default TTL on main.** A 24h
  full-autonomy window on a production branch is asking for an unattended
  bad publish. Use Recipe 3 instead (narrow publish window).
- **Leaving `supply` on between sessions.** The model can install tools
  that expand its reach while you are away. Re-arm per task, close after.
- **Treating freerange as a sandbox.** It is not. It suppresses
  *approval*, not *execution*. A model that has decided to exfiltrate
  can edit `policy.py` directly. Freerange does not stop that and never
  claimed to.
- **Enabling freerange to work around a gate you do not understand.**
  If `pip install` keeps halting, find out why (which issue, which
  dependency) before suppressing the gate. Suppression hides the signal.

---

## Operational hygiene

- **Re-arm per session, close after.** Default TTL 24h is a ceiling for
  safety, not a target. Use `--ttl` to match the task.
- **Read the audit log.** `.harness/logs/freerange.jsonl` is append-only.
  `grep '"event": "on"'` shows every enable with scope and TTL.
- **Check `/laplace:status` first.** It shows the active scope and
  remaining time at the top. No surprise activations.
- **`/laplace:freerange off` is always safe.** No confirmation needed.
  Restoring gates has no gate.

## See also

- Motivation triggers (SPEC-005) — the cron-driven companion to `flow`.
  Documented in `CHANGELOG.md` under v0.6.0.
- Freerange design notes (SPEC-007) — live under `specs/` in the source
  repository; the scope catalog and limits in this recipe summarize them.
