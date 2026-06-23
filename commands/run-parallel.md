---
description: Run approved issues in parallel waves — dispatches ready issues up to max_parallel, halts at gates (merge-wait, branch-stale, exhausted)
argument-hint: ""
allowed-tools: Bash, Read
---

Run one parallel dispatch wave over the approved queue now. The scheduler owns wave dispatch; this command only launches it and reports the outcome.

Run:

```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/parallel_queue.py" start $ARGUMENTS
```

This command takes no issue-id argument: the scheduler dispatches ALL approved issues whose `depends_on` are fully terminal, up to `max_parallel` concurrently (default 2; configurable via `.harness/config.yml` `limits.max_parallel`). Each dispatched issue gets its own worktree via `runner.cmd_start`.

Print the scheduler's wave summary verbatim. Then map the outcome to exactly one next action — do not ask for confirmation, do not silently retry:

- `wave-dispatched` — A wave was dispatched; some issues remain ready (deferred by the cap) or a dep was satisfied while others run. Re-run `/laplace:run-parallel` to dispatch the next wave.
- `wave-dispatched:waiting` — A wave was dispatched; issues are in-flight and nothing else is ready. Drive each in-flight issue to a terminal state (the model owns PM/Dev/Review/Security per issue, same as `/laplace:run`), then re-run `/laplace:run-parallel`.
- `queue-exhausted` — No ready issues AND no in-flight issues. All approved work is complete.
- `start-failed:<id>:<rc>` — Dispatching `<id>` failed with exit code `<rc>`. The wave halted. Surface verbatim and recommend `/laplace:status`. Do not retry.

Re-invoke `/laplace:run-parallel` after each terminal transition to dispatch the next wave. The scheduler is synchronous: one wave per invocation, no long-running background process.
