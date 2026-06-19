---
description: Run approved issues as a queue — auto-advances on review-passed, halts at gates (merge-wait, conflict, approval-required)
argument-hint: "[issue-id]"
allowed-tools: Bash, Read
---

Run the approved-issue queue now. The runner owns iteration; this command only launches it and reports the outcome.

Run:

```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/queue_runner.py" start $ARGUMENTS
```

With no argument, starts at the head of the approved queue. With `<issue-id>`, starts at that issue (must itself be `approved`).

Print the runner's parent-run outcome verbatim (the result block, the queue-run id, and the `queue_steps` summary). Then map the terminal outcome to exactly one next action — do not ask for confirmation, do not silently retry:

- `merge-wait:<id>` — Issue `<id>` reached `review-passed`. Merge branch `laplace/<id>` into base, then re-run `/laplace:run-queue` to continue.
- `merge-conflict:<id>` — Auto-merge of `<id>` conflicted on integration branch `laplace/queue-<run>`. Resolve the conflict manually, then re-run `/laplace:run-queue`.
- `queue-exhausted` — All approved issues complete.
- `max-queue-run-reached:<n>` — Cap of `<n>` consecutive issues reached. Re-run `/laplace:run-queue` to continue.
- `terminal:<final>` (e.g. `terminal:blocked`, `terminal:human-approval-required`) — Issue did not reach `review-passed`. Surface the runner's reason verbatim. Resolve via the existing exception flow; do not retry here.
- Any other non-zero exit (`held-lock:<id>`, `fix-limit-exceeded:<id>`, `start-failed:<id>:<rc>`, non-terminal halt) — Report as an error and recommend `/laplace:status`. Do not retry.
