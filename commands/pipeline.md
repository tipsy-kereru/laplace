---
description: Run the end-to-end checkpoint pipeline (intake -> verify -> approve-gate -> parallel -> release-gate). Halts at every gate; re-invoke to resume.
argument-hint: "<prd> [--release <ver>] [--auto-approve-low-risk] [--max-parallel N] [--resume]"
allowed-tools: Bash, Read
---

Run the Laplace checkpoint pipeline over a PRD. This composes `/laplace:intake`, `/laplace:verify`, `/laplace:approve`, `/laplace:run-parallel`, and `/laplace:release` into a single end-to-end flow with checkpoint resume. It does NOT remove any gate — every gate still fires; the pipeline only sequences them and halts at each human checkpoint.

Run:

```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/pipeline.py" $ARGUMENTS
```

The pipeline is a phase machine. On a fresh run it starts at `intake`. Each phase either auto-advances or halts with a specific sub-state and a printed "Next:" action. Re-invoking `/laplace:pipeline --resume` (or `/laplace:pipeline <prd>` again) reads the active pipeline log and continues from the recorded phase.

Print the pipeline halt message verbatim. Then map the halt sub-state to exactly one next action — do not ask for confirmation, do not silently retry:

- `intake-failed` — intake failed (PRD parse error, missing `.harness/`). Fix the cause then `/laplace:pipeline --resume`.
- `verify-failed` — verify reported FAIL. Fix the draft issues then `/laplace:pipeline --resume`, OR re-run with `--force-verify` as the documented escape hatch.
- `verify-usage` — verify returned a usage error. Inspect the PRD path.
- `approve-gate` — the human approval gate. Review the verify report above (PASS/WARN), read the per-issue `issue=risk` table, then re-run `/laplace:pipeline --resume` to batch-approve all drafts and proceed to the parallel phase.
- `approve-gate:<ids>` (with `--auto-approve-low-risk`) — low-risk drafts were auto-approved; the listed medium+/high drafts still need manual `/laplace:approve <id>`, then `/laplace:pipeline --resume`.
- `parallel:wave-dispatched:waiting` — a wave was dispatched and issues are in-flight. Drive each in-flight issue to a terminal state, then `/laplace:pipeline --resume`.
- `parallel:merge-wait:<id>` — issue `<id>` is waiting on a human merge. Merge it (or `/laplace:cancel <id>`), then `/laplace:pipeline --resume`.
- `parallel:cancel-failed:<id>` — a stranded child needs cleanup. `/laplace:cancel <id>`, then `/laplace:pipeline --resume`.
- `parallel-blocked:<id>` — issue `<id>` is blocked/human-approval-required or hit start-failed. Resolve it, then `/laplace:pipeline --resume`.
- `release-gate` — the release gate. Either invoke `/laplace:release <X.Y.Z>` separately, OR re-run `/laplace:pipeline --release <X.Y.Z> --resume` to have the pipeline call it after queue-exhausted.
- `release-failed` — `release.cmd_release` halted on one of its 8 checks. Resolve the failing check (see message above), then `/laplace:pipeline --resume`.
- `Pipeline complete.` — all phases done, pipeline log finalized.

If `/laplace:pipeline <other-prd>` is invoked while a pipeline for a different PRD is active, the pipeline refuses with `active pipeline for <other>; cancel it first or use --resume`.

Flags:
- `--release <X.Y.Z>` — at the release-gate (after queue-exhausted, no halted issues), call `release.cmd_release` instead of halting.
- `--auto-approve-low-risk` — at the approve-gate, auto-approve drafts whose Risk Level is `low`; halt if any draft is medium+. Default OFF (the approve gate always halts).
- `--max-parallel N` — override `.harness/config.yml` `limits.max_parallel` for the parallel phase.
- `--resume` — explicitly resume the active pipeline from its recorded phase.
- `--force-verify` — escape hatch: proceed past a verify FAIL verdict.
