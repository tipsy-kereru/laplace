#!/usr/bin/env python3
"""SPEC-006: cost-watcher decision script.

Reads run-log signals (tokens, runtime_minutes, files_changed) for an issue
run, compares against thresholds from config.yml `cost_watcher.thresholds`,
and emits a verdict: pass / warn / block. Block transitions the issue to
`human-approval-required` with reason `cost-block:<signal>:<value>`.

Sits in the state machine between `security-review` and `review-passed`:
    security-review -> cost-review -> review-passed (normal)
                                 -> human-approval-required (block)
                                 -> blocked

AC-LP-008 (review-passed test-evidence gate) still fires on the subsequent
`cost-review -> review-passed` transition; cost-review does not bypass it.

stdlib only. Exit codes:
    0  pass or warn (advance to review-passed)
    4  block (transition to human-approval-required)
    1  usage / not found
"""
import argparse
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

import state  # noqa: E402  (same-scripts dir on sys.path via runner)


def _harness_root(target: Optional[str]) -> str:
    return state._harness_root(target)


def _cost_reviews_path(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), ".harness", "logs",
                        "cost-reviews.jsonl")


def _run_log(run_id: str, target: Optional[str]) -> Optional[Dict[str, Any]]:
    path = os.path.join(state._runs_dir(target), f"{run_id}.json")
    run = state._read_json(path, default=None)
    return run if isinstance(run, dict) else None


def _aggregate_tokens(run: Dict[str, Any]) -> Tuple[int, str]:
    """Sum token counts recorded in the run log. Returns (count, source).

    Token entries are best-effort: they may be absent. When no token evidence
    is recorded, returns ("unknown", "no token evidence in run log").
    """
    total = 0
    found = False
    for e in run.get("evidence", []) or []:
        tok = e.get("tokens")
        if isinstance(tok, int) and tok > 0:
            total += tok
            found = True
    if not found:
        return (None, "unknown")
    return (total, "run-log evidence sum")


def _runtime_minutes(run: Dict[str, Any]) -> Optional[int]:
    """Whole minutes from started_at to ended_at (or now if open)."""
    started = run.get("started_at")
    if not isinstance(started, (int, float)):
        return None
    ended = run.get("ended_at") or time.time()
    if not isinstance(ended, (int, float)):
        ended = time.time()
    return int((ended - started) // 60)


def _files_touched(run: Dict[str, Any]) -> Optional[int]:
    """Count distinct files in the run's recorded diff/file evidence."""
    files = set()
    for e in run.get("evidence", []) or []:
        paths = e.get("files_changed") or e.get("files")
        if isinstance(paths, list):
            for p in paths:
                if isinstance(p, str) and p:
                    files.add(p)
    # Also count file paths recorded in transition summaries as a fallback.
    return len(files) if files else None


def _signals(run: Dict[str, Any]) -> Dict[str, Any]:
    tokens, _ = _aggregate_tokens(run)
    return {
        "tokens": tokens,
        "runtime_minutes": _runtime_minutes(run),
        "files_changed": _files_touched(run),
    }


def decide(signals: Dict[str, Any],
           thresholds: Dict[str, Dict[str, int]]) -> Tuple[str, Optional[str], Optional[int]]:
    """Returns (verdict, blocking_signal, blocking_value).

    verdict is "block" if any known signal >= its block threshold;
    "warn" if any known signal >= warn; else "pass". `unknown` signals
    never block on their own.
    """
    for sig, value in signals.items():
        if value is None:
            continue
        block = thresholds.get(sig, {}).get("block")
        if block is not None and value >= block:
            return ("block", sig, value)
    verdict = "pass"
    for sig, value in signals.items():
        if value is None:
            continue
        warn = thresholds.get(sig, {}).get("warn")
        if warn is not None and value >= warn:
            verdict = "warn"
    return (verdict, None, None)


def _record(issue_id: str, run_id: str, signals: Dict[str, Any],
            verdict: str, blocking: Optional[Tuple[str, int]],
            target: Optional[str]) -> None:
    path = _cost_reviews_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "ts": time.time(),
        "issue_id": state._redact_evidence(issue_id),
        "run_id": state._redact_evidence(run_id),
        "signals": {k: v for k, v in signals.items()},
        "verdict": verdict,
    }
    if blocking:
        entry["blocking_signal"] = blocking[0]
        entry["blocking_value"] = blocking[1]
    with open(path, "a", encoding="utf-8") as f:
        f.write(__import__("json").dumps(entry) + "\n")


def cmd_review(args: argparse.Namespace) -> int:
    cfg = state.load_config(args.target)
    cw = cfg.get("cost_watcher") or {}
    if not cw.get("enabled"):
        # Disabled: cost-review should not have been entered. Pass through.
        print("cost-review: disabled (pass)", file=sys.stderr)
        return 0
    thresholds = cw.get("thresholds") or {}

    tasks = state._load_tasks(args.target)
    meta = tasks.get(args.issue_id)
    if not meta:
        print(f"cost-review: issue not found: {args.issue_id}", file=sys.stderr)
        return 1
    run_id = meta.get("run_id")
    if not run_id:
        print(f"cost-review: no run_id for {args.issue_id}", file=sys.stderr)
        return 1
    run = _run_log(run_id, args.target)
    if not run:
        print(f"cost-review: run log not found: {run_id}", file=sys.stderr)
        return 1

    signals = _signals(run)
    verdict, block_sig, block_val = decide(signals, thresholds)
    _record(args.issue_id, run_id, signals, verdict,
            (block_sig, block_val) if block_sig else None, args.target)

    print(f"cost-review: {verdict} (signals={signals})")
    if verdict == "block":
        # Transition to human-approval-required via the state machine.
        ok, reason = state.validate_transition("cost-review", "human-approval-required")
        if ok:
            state._set_issue_state(
                args.issue_id, "human-approval-required", target=args.target,
                block_reason=f"cost-block:{block_sig}:{block_val}")
        else:
            print(f"cost-review: cannot transition: {reason}", file=sys.stderr)
            return 1
        return 4
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SPEC-006 cost watcher review")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="Evaluate an issue's cost signals")
    r.add_argument("issue_id")
    r.add_argument("--target", default=None)
    r.set_defaults(func=cmd_review)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
