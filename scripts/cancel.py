#!/usr/bin/env python3
"""Laplace cancel command (ISSUE-0008).

Responsibilities:
  - Single-issue cancel: end the issue's active run with outcome=cancelled,
    set issue state to `cancelled`, release the lock, append run history.
  - Queue-scope cancel: rewrite the resumable queue parent log's outcome from
    ``merge-<reason>:<id>`` to ``cancelled:<id>`` so it drops out of the
    resumable set (``state._find_resumable_queue_run`` filters on
    ``outcome.startswith("merge-")``).

stdlib-only. Reuses runner.cmd_end for lock release + run-log finalization
and state helpers for direct state writes. Does NOT duplicate lock logic.
Does NOT delete branches or artifacts. Does NOT push.

Detection priority (AC-QR-020-cancel-detect):
  1. issue arg provided            -> single-issue path on that issue.
  2. no arg + active in-progress   -> single-issue path on the active issue.
  3. no arg + resumable queue      -> queue-scope path (only if no single run).
  4. none of the above             -> "nothing to cancel", exit 0.
"""

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer modules imported after sys.path bootstrap (mirrors runner.py).
import state  # noqa: E402
import runner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_active_run_for_issue(issue_id: str, target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    """Return the active run log for ``issue_id`` if it has an in-progress
    run recorded in tasks.json, else None. Mirrors the detection used by
    ``state._format_status`` for the "Active run" block."""
    tasks = state._load_tasks(target)
    meta = tasks.get(issue_id)
    if not meta:
        return None
    if meta.get("status") != "in-progress" or not meta.get("run_id"):
        return None
    run_path = os.path.join(state._runs_dir(target), f"{meta['run_id']}.json")
    run = state._read_json(run_path, default=None)
    return run if isinstance(run, dict) else None


def _find_active_single_issue_run(target: Optional[str]) \
        -> Optional[Tuple[str, Dict[str, Any]]]:
    """Scan tasks.json for any issue with status=in-progress and a run_id.
    Returns (issue_id, run_log) for the first match, else None."""
    tasks = state._load_tasks(target)
    runs_dir = state._runs_dir(target)
    for tid, meta in tasks.items():
        if meta.get("status") == "in-progress" and meta.get("run_id"):
            run_path = os.path.join(runs_dir, f"{meta['run_id']}.json")
            run = state._read_json(run_path, default=None)
            if isinstance(run, dict):
                return tid, run
    return None


def _set_issue_cancelled(issue_id: str, run_id: str,
                         target: Optional[str]) -> None:
    """Force the issue state to ``cancelled``.

    ``in-progress -> cancelled`` is not a legal state-machine transition
    (the legal path is in-progress -> blocked -> human-resolution ->
    cancelled). Cancel is a user-initiated terminal exception (SPEC-002
    §State Machine exception flow), so we set the state directly via
    ``state._set_issue_state`` and append a run-history entry recording the
    cancel. This mirrors how runner.py end handles terminal outcomes that
    bypass the normal phase graph.
    """
    state._set_issue_state(issue_id, "cancelled", target=target, run_id=run_id)


def _finalize_queue_cancel(run_id: str, target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    """Rewrite the resumable queue parent log so it is no longer resumable.

    Changes ``outcome`` from ``merge-<reason>:<id>`` to ``cancelled:<id>``
    (preserving the ``:<id>`` suffix so the merge-waited issue stays
    identifiable) and bumps ``ended_at``. Writes atomically via
    ``state._atomic_write_json``. Preserves ``queue_steps`` and ``issues``.

    Returns the rewritten log, or None if the log was missing / malformed.
    """
    path = os.path.join(state._runs_dir(target), f"{run_id}.json")
    log = state._read_json(path, default=None)
    if not isinstance(log, dict):
        return None
    outcome = log.get("outcome") or ""
    suffix = ""
    if isinstance(outcome, str) and ":" in outcome:
        suffix = outcome.split(":", 1)[1]
    new_outcome = f"cancelled:{suffix}" if suffix else "cancelled"
    log["outcome"] = state._redact_evidence(new_outcome)
    log["ended_at"] = time.time()
    state._atomic_write_json(path, log)
    return log


def _parallel_in_flight_child_runs(log: Dict[str, Any],
                                   target: Optional[str]) \
        -> List[Tuple[str, str]]:
    """Return (issue_id, run_id) for in-flight children of a parallel log.

    In-flight = child run's issue is in a non-terminal status. Reads each
    child run log for its ``issue_id`` and uses the issue's recorded
    ``run_id`` from tasks.json.
    """
    tasks = state._load_tasks(target)
    runs_dir = state._runs_dir(target)
    out: List[Tuple[str, str]] = []
    for child_run_id in (log.get("issues") or []):
        child_path = os.path.join(runs_dir, f"{child_run_id}.json")
        child = state._read_json(child_path, default=None)
        if not isinstance(child, dict):
            continue
        issue_id = child.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id:
            continue
        status = tasks.get(issue_id, {}).get("status")
        if status in state.TERMINAL_STATES:
            continue
        rid = tasks.get(issue_id, {}).get("run_id") or child_run_id
        out.append((issue_id, rid))
    return out


def _finalize_parallel_cancel(run_id: str, target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    """Finalize the active parallel parent log as cancelled.

    Sets ``outcome`` to ``cancelled`` and bumps ``ended_at``. Records the
    wave position for resume by leaving the ``waves`` array intact. Writes
    atomically. Returns the rewritten log, or None if missing/malformed.
    """
    path = os.path.join(state._runs_dir(target), f"{run_id}.json")
    log = state._read_json(path, default=None)
    if not isinstance(log, dict):
        return None
    log["outcome"] = state._redact_evidence("cancelled")
    log["ended_at"] = time.time()
    state._atomic_write_json(path, log)
    return log


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def _print_single_result(issue_id: str, run_id: str,
                         target: Optional[str]) -> None:
    print("Cancelled single-issue run.")
    print(f"  Issue: {issue_id}")
    print(f"  Run: {run_id}")
    print(f"  State: cancelled")
    print(f"  Lock: released")
    print(f"\nArtifacts:")
    print(f"  - .harness/state/runs/{run_id}.json")
    print(f"\nNext:")
    print(f"  /laplace:status")


def _print_queue_result(run_id: str, merge_waited_issue: str,
                        target: Optional[str]) -> None:
    print("Cancelled queue run (was merge-waiting).")
    print(f"  Queue run: {run_id}")
    print(f"  Merge-waited issue: {merge_waited_issue}")
    print(f"  Outcome: cancelled:{merge_waited_issue}")
    print(f"\nNext:")
    print(f"  /laplace:status  (Queue run: block is gone)")
    print(f"  /laplace:run-queue  (starts fresh from approved head)")


def _print_parallel_result(run_id: str, cancelled_issues: List[str],
                           wave_count: int) -> None:
    print("Cancelled parallel run.")
    print(f"  Parallel run: {run_id}")
    print(f"  Waves at cancel: {wave_count}")
    print(f"  In-flight issues torn down ({len(cancelled_issues)}):")
    for iid in cancelled_issues:
        print(f"    {iid}")
    print(f"  Outcome: cancelled")
    print(f"\nNext:")
    print(f"  /laplace:status  (Parallel run: block is gone)")
    print(f"  /laplace:run-parallel  (starts fresh wave from approved queue)")


def cmd_cancel(args: argparse.Namespace) -> int:
    target = getattr(args, "target", None)
    issue_arg = getattr(args, "issue_id", None)

    # Path 1: explicit issue arg -> single-issue cancel.
    if issue_arg:
        run = _find_active_run_for_issue(issue_arg, target)
        if not run:
            print(f"no active run for {issue_arg}", file=sys.stderr)
            return 1
        run_id = run.get("run_id") or ""
        if not run_id:
            print(f"active run for {issue_arg} has no run_id", file=sys.stderr)
            return 1
        rc = runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="cancelled", evidence=None, target=target))
        if rc != 0:
            return rc
        _set_issue_cancelled(issue_arg, run_id, target)
        runner._append_run_history_to_issue(
            issue_id=issue_arg, line=f"cancel: {run_id} -> cancelled",
            target=target)
        _print_single_result(issue_arg, run_id, target)
        return 0

    # Path 2: no arg + active single-issue run -> cancel the single run.
    active = _find_active_single_issue_run(target)
    if active is not None:
        issue_id, run = active
        run_id = run.get("run_id") or ""
        if not run_id:
            print(f"active run for {issue_id} has no run_id", file=sys.stderr)
            return 1
        rc = runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="cancelled", evidence=None, target=target))
        if rc != 0:
            return rc
        _set_issue_cancelled(issue_id, run_id, target)
        runner._append_run_history_to_issue(
            issue_id=issue_id, line=f"cancel: {run_id} -> cancelled",
            target=target)
        _print_single_result(issue_id, run_id, target)
        return 0

    # Path 2b: no arg + active parallel run -> tear down all in-flight
    # children, finalize the parent parallel log as cancelled (AC-PQ-010).
    # Inserted between single-issue and sequential-queue detection so the
    # parallel lifecycle (multi-child) takes precedence over the sequential
    # queue's single-merge-wait resume when a parallel run is live.
    parallel = state._find_active_parallel_run(target)
    if parallel is not None:
        p_run_id = parallel.get("run_id") or ""
        children = _parallel_in_flight_child_runs(parallel, target)
        cancelled_issues: List[str] = []
        for issue_id, child_run_id in children:
            rc = runner.cmd_end(argparse.Namespace(
                run_id=child_run_id, outcome="cancelled", evidence=None,
                target=target))
            if rc != 0:
                # Surface the child failure but continue tearing down the
                # rest (best-effort cancel). The parent log is still
                # finalized below so it drops out of the active set.
                print(f"warning: child cancel failed for {issue_id} "
                      f"(rc={rc})", file=sys.stderr)
            else:
                _set_issue_cancelled(issue_id, child_run_id, target)
                runner._append_run_history_to_issue(
                    issue_id=issue_id,
                    line=f"parallel-cancel: {child_run_id} -> cancelled",
                    target=target)
            cancelled_issues.append(issue_id)
        rewritten = _finalize_parallel_cancel(p_run_id, target)
        if rewritten is None:
            print(f"parallel run log not found: {p_run_id}", file=sys.stderr)
            return 1
        wave_count = len(rewritten.get("waves") or [])
        _print_parallel_result(p_run_id, cancelled_issues, wave_count)
        return 0

    # Path 3: no active single run + resumable queue -> cancel the queue run.
    resumable = state._find_resumable_queue_run(target)
    if resumable is not None:
        q_run_id = resumable.get("run_id") or ""
        rewritten = _finalize_queue_cancel(q_run_id, target)
        if rewritten is None:
            print(f"queue run log not found: {q_run_id}", file=sys.stderr)
            return 1
        merge_waited = state._resumable_queue_current_issue(resumable, target)
        _print_queue_result(q_run_id, merge_waited, target)
        return 0

    # Path 4: nothing to cancel.
    print("nothing to cancel")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cancel.py",
        description="Laplace cancel: stop a single-issue run or a resumable "
                    "queue run safely (ISSUE-0008).")
    _add_target_arg(parser)
    parser.add_argument("issue_id", nargs="?", default=None,
                        help="Optional issue id whose active run to cancel. "
                             "If omitted, cancels the active single-issue run, "
                             "else the resumable queue run, else no-op.")
    parser.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
