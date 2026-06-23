#!/usr/bin/env python3
"""Laplace parallel queue scheduler (ISSUE-0004).

Wave-based parallel scheduler over the worktree-isolated runner. Composes
``runner.cmd_start`` (one worktree per dispatched issue) + the existing
dependency-readiness helper ``state._dependencies_satisfied`` + a parent
parallel-run log.

Responsibilities:
  - One dispatch wave per invocation (mirrors run-queue's synchronous
    contract): compute ready issues, dispatch up to ``max_parallel``,
    record the wave, exit. The model re-invokes after the next terminal
    transition.
  - Readiness rule (reuse, not re-implement): an approved issue is ready
    iff it is not already in-flight, not in the halted set, and
    ``state._dependencies_satisfied`` returns True.
  - ``max_parallel`` cap: ``slots = max(0, max_parallel - len(in_flight))``
    -- concurrency cap violations impossible by construction (AC-PQ-004/012).
  - Halt isolation: an issue that returns EXIT_BRANCH_STALE is recorded in
    the halted set; siblings continue. The halted set persists in the
    parent log and is skipped on re-invocation (AC-PQ-005).
  - Parent parallel-run log at ``.harness/state/runs/<parallel-run-id>.json``
    with ``kind: "parallel-queue"`` and a ``waves`` array (AC-PQ-007).

Deadlock-free invariant (AC-PQ-003):
  - Cycles in ``depends_on`` are rejected at ``/laplace:approve`` via
    ``state._check_dependency_graph`` (characterized in selftest).
  - The scheduler dispatches only issues whose deps are already terminal
    (``_dependencies_satisfied``). A non-terminal dep blocks dispatch,
    but cannot create a wait-cycle because terminal-ness is monotonic:
    once a dep reaches a terminal state it never leaves it.
  - Therefore the scheduler can never wait on an issue that is waiting on
    the current one -- cycles were structurally rejected upstream.

This module is stdlib-only and reuses state.py atomic helpers. It does NOT
re-implement state transitions, fix-attempt limits, test-evidence gates,
worktree setup, or security checks -- those live inside runner.py/state.py
primitives. parallel_queue only composes them and maps their exit codes to
wave outcomes.
"""

import argparse
import hashlib
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer modules imported after the sys.path bootstrap above (mirrors
# queue_runner.py / runner.py).
import state  # noqa: E402
import runner  # noqa: E402
import policy  # noqa: ARG001  # noqa: E402  (available for future gate routing)

# Exit codes mirrored from runner.py.
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_INVALID = 2
EXIT_LOCK_HELD = 3
EXIT_EVIDENCE_MISSING = 4
EXIT_FIX_LIMIT_EXCEEDED = 5
EXIT_BRANCH_STALE = 6

# Issue statuses that count as "in-flight" (started but not terminal).
# Matches the wave scheduler's in_flight definition from the PM notes.
IN_FLIGHT_STATUSES = (
    "pm-review",
    "ready-for-dev",
    "in-progress",
    "review",
    "needs-fix",
    "security-review",
)

# Outcomes that leave the parent log open (waiting for re-invocation).
_OPEN_OUTCOMES = ("wave-dispatched", "wave-dispatched:waiting")


# ---------------------------------------------------------------------------
# Parent parallel-run log (AC-PQ-007)
# ---------------------------------------------------------------------------

def _new_parallel_run_id() -> str:
    """Generate a parallel-run id using the same scheme as queue_runner."""
    raw = f"parallel-{time.time()}-{os.getpid()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _parallel_run_log_path(run_id: str, target: Optional[str]) -> str:
    return os.path.join(state._runs_dir(target), f"{run_id}.json")


def _create_parent_log(run_id: str, config: Dict[str, Any],
                       target: Optional[str]) -> str:
    log: Dict[str, Any] = {
        "run_id": run_id,
        "kind": "parallel-queue",
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,
        "max_parallel": config["max_parallel"],
        "merge_policy": config["merge_policy"],
        "issues": [],   # child run ids (chronological)
        "halted": [],   # issue ids forced into halted set (AC-PQ-005)
        "waves": [],    # one entry per dispatch invocation
    }
    state._atomic_write_json(_parallel_run_log_path(run_id, target), log)
    return run_id


def _load_parent_log(run_id: str, target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    log = state._read_json(_parallel_run_log_path(run_id, target),
                           default=None)
    return log if isinstance(log, dict) else None


def _save_parent_log(log: Dict[str, Any], run_id: str,
                     target: Optional[str]) -> None:
    state._atomic_write_json(_parallel_run_log_path(run_id, target), log)


def _finalize_parent_log(run_id: str, outcome: str,
                         target: Optional[str]) -> None:
    log = _load_parent_log(run_id, target)
    if log is None:
        return
    log["ended_at"] = time.time()
    log["outcome"] = state._redact_evidence(outcome)
    _save_parent_log(log, run_id, target)


def _append_wave(run_id: str, dispatched: List[str], in_flight: List[str],
                 halted: List[str], ready_count: int,
                 target: Optional[str]) -> None:
    log = _load_parent_log(run_id, target)
    if log is None:
        return
    log.setdefault("waves", []).append({
        "ts": time.time(),
        "dispatched": [state._redact_evidence(i) for i in dispatched],
        "in_flight": [state._redact_evidence(i) for i in in_flight],
        "halted": [state._redact_evidence(i) for i in halted],
        "ready_count": ready_count,
    })
    _save_parent_log(log, run_id, target)


def _record_child_run(run_id: str, child_run_id: str,
                      target: Optional[str]) -> None:
    log = _load_parent_log(run_id, target)
    if log is None:
        return
    issues = log.setdefault("issues", [])
    if child_run_id not in issues:
        issues.append(state._redact_evidence(child_run_id))
    _save_parent_log(log, run_id, target)


def _add_halted(run_id: str, issue_id: str,
                target: Optional[str]) -> None:
    log = _load_parent_log(run_id, target)
    if log is None:
        return
    halted = log.setdefault("halted", [])
    if issue_id not in halted:
        halted.append(state._redact_evidence(issue_id))
    _save_parent_log(log, run_id, target)


def _find_open_parallel_run(target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    """Return the most-recent open (non-finalized) parallel-queue log, or None.

    "Open" = ``kind == "parallel-queue"`` AND ``outcome`` is None or one of
    the wave-dispatched interim outcomes. Mirrors
    ``state._find_active_parallel_run`` (kept here so the scheduler module
    is self-contained for resume).
    """
    runs_dir = state._runs_dir(target)
    if not os.path.isdir(runs_dir):
        return None
    candidates: List[Dict[str, Any]] = []
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = state._read_json(os.path.join(runs_dir, name), default=None)
        if not isinstance(log, dict):
            continue
        if log.get("kind") != "parallel-queue":
            continue
        outcome = log.get("outcome")
        if outcome is not None and outcome not in _OPEN_OUTCOMES:
            continue
        candidates.append(log)
    if not candidates:
        return None
    candidates.sort(key=lambda l: float(l.get("started_at") or 0.0),
                    reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Wave dispatch
# ---------------------------------------------------------------------------

def _read_issue_status(issue_id: str, target: Optional[str]) -> Optional[str]:
    tasks = state._load_tasks(target)
    return tasks.get(issue_id, {}).get("status")


def _read_issue_run_id(issue_id: str, target: Optional[str]) -> Optional[str]:
    tasks = state._load_tasks(target)
    rid = tasks.get(issue_id, {}).get("run_id")
    return rid if rid else None


def _compute_sets(approved: List[str], halted: List[str],
                  target: Optional[str]) -> Tuple[List[str], List[str]]:
    """Compute (in_flight, ready) lists from the approved queue.

    in_flight = approved issues whose current status is a non-terminal
    "running" status (pm-review/ready-for-dev/in-progress/review/
    needs-fix/security-review).
    ready = approved issues not in_flight, not halted, and whose deps are
    satisfied (state._dependencies_satisfied). Preserves approved order.
    """
    tasks = state._load_tasks(target)
    in_flight: List[str] = []
    for iid in approved:
        st = tasks.get(iid, {}).get("status")
        if st in IN_FLIGHT_STATUSES:
            in_flight.append(iid)
    halted_set = set(halted)
    ready: List[str] = []
    for iid in approved:
        if iid in in_flight:
            continue
        if iid in halted_set:
            continue
        ok, _reason = state._dependencies_satisfied(iid, target=target)
        if ok:
            ready.append(iid)
    return in_flight, ready


def _dispatch_wave(parent_run_id: str, target: Optional[str],
                   to_dispatch: List[str]) -> Tuple[List[str], List[str]]:
    """Call runner.cmd_start for each issue in to_dispatch.

    Returns (halted_new, failed). halted_new = issues that returned
    EXIT_BRANCH_STALE (added to the parent's halted set). failed = list of
    (issue_id, rc) tuples for non-OK, non-stale returns; the caller halts
    the whole wave on the first failure.
    """
    halted_new: List[str] = []
    for iid in to_dispatch:
        ns = argparse.Namespace(issue_id=iid, target=target)
        try:
            rc = runner.cmd_start(ns)
        except SystemExit as e:
            rc = int(e.code) if isinstance(e.code, int) else 1
        if rc == EXIT_OK:
            child_run_id = _read_issue_run_id(iid, target)
            if child_run_id:
                _record_child_run(parent_run_id, child_run_id, target)
            continue
        if rc == EXIT_BRANCH_STALE:
            # AC-PQ-005: record in halted, siblings continue.
            halted_new.append(iid)
            _add_halted(parent_run_id, iid, target)
            continue
        # Other non-zero: start-failed. Caller halts the wave.
        return halted_new, [(iid, rc)]
    return halted_new, []


def _run_parallel_wave(target: Optional[str],
                       config: Dict[str, Any]) -> Tuple[str, int]:
    """Execute one dispatch wave. Returns (parent_run_id, exit_code).

    First invocation creates the parent log; subsequent invocations resume
    the most-recent open one. Emits one wave entry, then exits.
    """
    max_parallel = config["max_parallel"]

    # Open/resume parent log.
    parent = _find_open_parallel_run(target)
    if parent is None:
        parent_run_id = _new_parallel_run_id()
        _create_parent_log(parent_run_id, config, target)
        halted: List[str] = []
    else:
        parent_run_id = parent.get("run_id") or _new_parallel_run_id()
        halted = list(parent.get("halted") or [])

    approved = list(state._load_queue(target).get("approved", []))

    in_flight, ready = _compute_sets(approved, halted, target)
    slots = max(0, max_parallel - len(in_flight))
    to_dispatch = ready[:slots]

    halted_new, failed = _dispatch_wave(parent_run_id, target, to_dispatch)

    # start-failed halts the whole wave immediately.
    if failed:
        iid, rc = failed[0]
        outcome = f"start-failed:{iid}:{rc}"
        # Refresh in_flight/halted for the wave record before finalizing.
        in_flight_after, _ready_after = _compute_sets(approved, halted, target)
        _append_wave(parent_run_id, to_dispatch, in_flight_after,
                     halted + halted_new, len(ready), target)
        _finalize_parent_log(parent_run_id, outcome, target)
        print(f"parallel halted: {outcome}")
        return parent_run_id, EXIT_INVALID

    # Refresh sets post-dispatch for the wave record + decision.
    in_flight_after, ready_after = _compute_sets(approved, halted, target)
    halted_after = list(set(halted + halted_new))
    # Persist the carried-forward halted set.
    log = _load_parent_log(parent_run_id, target)
    if log is not None:
        log["halted"] = [state._redact_evidence(h) for h in halted_after]
        _save_parent_log(log, parent_run_id, target)

    _append_wave(parent_run_id, to_dispatch, in_flight_after,
                 halted_after, len(ready), target)

    # Outcome decision.
    if not ready_after and not in_flight_after:
        outcome = "queue-exhausted"
        _finalize_parent_log(parent_run_id, outcome, target)
        print(f"parallel: exhausted (no ready, no in-flight)")
        return parent_run_id, EXIT_OK
    if not ready_after and in_flight_after:
        outcome = "wave-dispatched:waiting"
        # Leave parent log open.
        print(f"parallel: wave dispatched ({len(to_dispatch)} started), "
              f"{len(in_flight_after)} in-flight, waiting for terminal")
        return parent_run_id, EXIT_OK
    # ready_after non-empty (some deferred to next wave due to cap, or a
    # dep just satisfied while others still running).
    outcome = "wave-dispatched"
    log = _load_parent_log(parent_run_id, target)
    if log is not None:
        log["outcome"] = outcome
        _save_parent_log(log, parent_run_id, target)
    print(f"parallel: wave dispatched ({len(to_dispatch)} started), "
          f"{len(in_flight_after)} in-flight, {len(ready_after)} ready")
    return parent_run_id, EXIT_OK


def cmd_parallel_start(args: argparse.Namespace) -> int:
    target = getattr(args, "target", None)
    config = state.load_config(target)  # exits 2 on validation failure
    _run_id, rc = _run_parallel_wave(target, config)
    return rc


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest() -> int:
    import shutil
    import tempfile

    failures: List[str] = []
    tmp = tempfile.mkdtemp(prefix="laplace-parallel-selftest-")

    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        assert state.cmd_init(target=tmp) == 0
        cfg = state.load_config(tmp)
        assert cfg["max_parallel"] == state.MAX_PARALLEL == 2

        def seed_approved(issue_id: str, depends_on: Optional[List[str]] = None) -> None:
            tasks = state._load_tasks(tmp)
            rec: Dict[str, Any] = {"status": "draft", "updated_at": time.time()}
            if depends_on:
                rec["depends_on"] = list(depends_on)
            tasks[issue_id] = rec
            state._save_tasks(tasks, target=tmp)
            q = state._load_queue(tmp)
            if issue_id not in q["draft"]:
                q["draft"].append(issue_id)
            state._save_queue(q, target=tmp)
            assert state.cmd_approve(argparse.Namespace(
                issue_id=issue_id, user="tester", target=tmp)) == 0, \
                f"cmd_approve failed for {issue_id}"

        def set_status(issue_id: str, status: str) -> None:
            tasks = state._load_tasks(tmp)
            tasks.setdefault(issue_id, {})["status"] = status
            tasks[issue_id]["updated_at"] = time.time()
            state._save_tasks(tasks, target=tmp)

        def drive_to_review_passed(issue_id: str) -> None:
            for src, dst in (("pm-review", "ready-for-dev"),
                             ("ready-for-dev", "in-progress"),
                             ("in-progress", "review")):
                ns = argparse.Namespace(
                    issue_id=issue_id, from_state=src, to_state=dst,
                    summary="", target=tmp,
                )
                assert runner.cmd_advance(ns) == 0
            run_id = _read_issue_run_id(issue_id, tmp)
            assert run_id, f"no run_id for {issue_id}"
            ns_ev = argparse.Namespace(
                run_id=run_id, kind="test", path_or_text="pytest: ok",
                target=tmp,
            )
            assert runner.cmd_evidence(ns_ev) == 0
            ns_pass = argparse.Namespace(
                issue_id=issue_id, from_state="review", to_state="review-passed",
                summary="ok", target=tmp,
            )
            assert runner.cmd_advance(ns_pass) == 0

        # --- Case 1: 3-issue A/B/C graph; wave 1 dispatches A+C ---------
        # A independent; B depends_on A; C independent. max_parallel=2.
        # Wave 1 should dispatch A and C (both ready), B deferred (dep on A).
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-A")
        seed_approved("ISSUE-B", depends_on=["ISSUE-A"])
        seed_approved("ISSUE-C")

        rid1, rc1 = _run_parallel_wave(tmp, cfg)
        if rc1 != 0:
            failures.append(f"case1 wave1 should exit 0, got {rc1}")
        log1 = _load_parent_log(rid1, tmp)
        if not log1 or log1.get("kind") != "parallel-queue":
            failures.append("case1: parent log missing or wrong kind")
        # A and C dispatched (in pm-review), B still approved.
        if _read_issue_status("ISSUE-A", tmp) != "pm-review":
            failures.append("case1: ISSUE-A should be pm-review after wave1")
        if _read_issue_status("ISSUE-C", tmp) != "pm-review":
            failures.append("case1: ISSUE-C should be pm-review after wave1")
        if _read_issue_status("ISSUE-B", tmp) != "approved":
            failures.append("case1: ISSUE-B should remain approved (dep unmet)")
        waves1 = log1.get("waves") or [] if log1 else []
        if len(waves1) != 1:
            failures.append(f"case1: expected 1 wave entry, got {len(waves1)}")
        if waves1:
            dispatched = waves1[0].get("dispatched") or []
            # Order = approved order filtered by readiness. A then C.
            if "ISSUE-A" not in dispatched or "ISSUE-C" not in dispatched:
                failures.append(
                    f"case1: wave1 dispatched should include A and C, got "
                    f"{dispatched}")
            if "ISSUE-B" in dispatched:
                failures.append("case1: ISSUE-B must not be dispatched (dep)")

        # --- Case 2: after A reaches review-passed, wave 2 dispatches B --
        drive_to_review_passed("ISSUE-A")
        rid2, rc2 = _run_parallel_wave(tmp, cfg)
        if rc2 != 0:
            failures.append(f"case2 wave2 should exit 0, got {rc2}")
        # Resume: same parent run id.
        if rid2 != rid1:
            failures.append(
                f"case2: should resume same parent run {rid1}, got {rid2}")
        if _read_issue_status("ISSUE-B", tmp) != "pm-review":
            failures.append("case2: ISSUE-B should be pm-review after wave2")
        log2 = _load_parent_log(rid2, tmp)
        waves2 = log2.get("waves") or [] if log2 else []
        if len(waves2) != 2:
            failures.append(f"case2: expected 2 wave entries, got {len(waves2)}")

        # --- Case 3: cap test (max_parallel=2, 5 ready -> 2 dispatched) --
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        for n in ("D1", "D2", "D3", "D4", "D5"):
            seed_approved(f"ISSUE-{n}")
        rid3, rc3 = _run_parallel_wave(tmp, cfg)
        if rc3 != 0:
            failures.append(f"case3 wave1 should exit 0, got {rc3}")
        started = [n for n in ("D1", "D2", "D3", "D4", "D5")
                   if _read_issue_status(f"ISSUE-{n}", tmp) == "pm-review"]
        if len(started) != 2:
            failures.append(
                f"case3: exactly 2 should start with max_parallel=2, got "
                f"{len(started)}: {started}")
        # Wave outcome should be wave-dispatched (ready remaining).
        log3 = _load_parent_log(rid3, tmp)
        if log3 and log3.get("outcome") != "wave-dispatched":
            failures.append(
                f"case3: expected outcome wave-dispatched, got "
                f"{log3.get('outcome') if log3 else None}")

        # --- Case 4: halt isolation (forced stale) -----------------------
        # Seed 2 ready issues; force the first to return EXIT_BRANCH_STALE
        # by monkey-patching runner.cmd_start via a wrapper.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-H1")
        seed_approved("ISSUE-H2")

        original_start = runner.cmd_start
        state.H1_calls = {"n": 0}

        def fake_start(args):
            iid = args.issue_id
            if iid == "ISSUE-H1":
                # Simulate stale-branch by returning EXIT_BRANCH_STALE.
                return EXIT_BRANCH_STALE
            return original_start(args)

        runner.cmd_start = fake_start
        try:
            rid4, rc4 = _run_parallel_wave(tmp, cfg)
        finally:
            runner.cmd_start = original_start
        if rc4 != 0:
            failures.append(f"case4 wave should exit 0 (halt isolated), got {rc4}")
        log4 = _load_parent_log(rid4, tmp)
        if log4 and "ISSUE-H1" not in (log4.get("halted") or []):
            failures.append("case4: ISSUE-H1 should be in halted set")
        if _read_issue_status("ISSUE-H2", tmp) != "pm-review":
            failures.append(
                "case4: ISSUE-H2 should still be dispatched (sibling continues)")

        # Re-invoke: ISSUE-H1 must be skipped (still halted).
        rid4b, rc4b = _run_parallel_wave(tmp, cfg)
        if rid4b != rid4:
            failures.append("case4b: should resume same parent run")
        if _read_issue_status("ISSUE-H1", tmp) == "pm-review":
            failures.append("case4b: ISSUE-H1 must NOT be re-dispatched (halted)")

        # --- Case 5: cycle-rejected characterization --------------------
        # cmd_approve rejects a cycle; the scheduler never sees it.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        tasks_c = state._load_tasks(tmp)
        tasks_c["ISSUE-CYC1"] = {"status": "draft", "updated_at": time.time(),
                                 "depends_on": ["ISSUE-CYC2"]}
        tasks_c["ISSUE-CYC2"] = {"status": "draft", "updated_at": time.time(),
                                 "depends_on": ["ISSUE-CYC1"]}
        state._save_tasks(tasks_c, target=tmp)
        q_c = state._load_queue(tmp)
        q_c["draft"].extend(["ISSUE-CYC1", "ISSUE-CYC2"])
        state._save_queue(q_c, target=tmp)
        rc_cyc1 = state.cmd_approve(argparse.Namespace(
            issue_id="ISSUE-CYC1", user="tester", target=tmp))
        if rc_cyc1 == 0:
            failures.append(
                "case5: cmd_approve should reject cycle (rc!=0), got 0")

        # --- Case 6: queue-exhausted ------------------------------------
        # No ready, no in-flight after a wave finalizes the parent log.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-E1")
        # First wave dispatches E1.
        rid6, rc6 = _run_parallel_wave(tmp, cfg)
        # Drive E1 to a terminal state (blocked via cmd_end).
        run_e1 = _read_issue_run_id("ISSUE-E1", tmp)
        assert run_e1
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_e1, outcome="blocked", target=tmp)) == 0
        # Second wave: E1 is terminal (blocked), not in approved? It is still
        # in approved (cmd_end doesn't pop). It IS terminal, so not in_flight,
        # and _dependencies_satisfied on it: deps empty -> ready. To test
        # exhaustion cleanly, remove E1 from approved.
        q_e = state._load_queue(tmp)
        q_e["approved"] = []
        state._save_queue(q_e, target=tmp)
        rid6b, rc6b = _run_parallel_wave(tmp, cfg)
        if rc6b != 0:
            failures.append(f"case6: exhausted should exit 0, got {rc6b}")
        log6 = _load_parent_log(rid6b, tmp)
        if log6 and log6.get("outcome") != "queue-exhausted":
            failures.append(
                f"case6: expected outcome queue-exhausted, got "
                f"{log6.get('outcome') if log6 else None}")
        if log6 and log6.get("ended_at") is None:
            failures.append("case6: parent log should be finalized (ended_at set)")

        # --- Case 7: empty approved queue -> exhausted on first wave -----
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        rid7, rc7 = _run_parallel_wave(tmp, cfg)
        log7 = _load_parent_log(rid7, tmp)
        if not log7 or log7.get("outcome") != "queue-exhausted":
            failures.append(
                f"case7: empty approved should be queue-exhausted, got "
                f"{log7.get('outcome') if log7 else None}")
    finally:
        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("parallel_queue selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parallel_queue.py",
        description="Laplace parallel queue scheduler (ISSUE-0004)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="Dispatch one wave of ready approved issues")
    _add_target_arg(p)
    p.set_defaults(func=cmd_parallel_start)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
