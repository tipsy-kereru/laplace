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
import fnmatch
import hashlib
import math
import os
import subprocess
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
# ISSUE-0011: reuse queue_runner's integration->main merge helper so both
# the serial and parallel queue runners share the same protected-ref guard
# and policy.check_command routing.
import queue_runner  # noqa: E402

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
# ISSUE-0014: ``wave-deferred:high-load:<ratio>`` is resumable -- the model
# re-invokes after system load drops, so the parent log must stay open.
_OPEN_OUTCOMES = ("wave-dispatched", "wave-deferred:high-load")


def _is_open_outcome(outcome: Optional[str]) -> bool:
    """A parent-log outcome is "open" (resumable) when it is None or one of
    the wave-dispatched / wave-deferred interim outcomes (ISSUE-0014).

    The deferred outcome carries a load ratio suffix, so it is matched by
    prefix rather than exact equality.
    """
    if outcome is None:
        return True
    for prefix in _OPEN_OUTCOMES:
        if outcome == prefix or outcome.startswith(prefix + ":"):
            return True
    return False


# Module-level one-shot flag for the Windows (no getloadavg) warning so we
# warn at most once per process (AC-RL-005).
_LOAD_WARNED = False


def _load_headroom(target: Optional[str],
                   config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sample pre-dispatch system load and compute the wave's dispatch cap
    (ISSUE-0014, AC-RL-001..005).

    Returns a dict with:
      - ``ratio``: float (load1 / cpu_count), or None on Windows.
      - ``cap``: int effective dispatch cap (0 means defer the wave).
      - ``deferred``: True when ``cap == 0`` (load at/above load_severe).

    Returns ``None`` when the load check is unavailable (Windows: no
    ``os.getloadavg``). In that case the caller dispatches at the static
    ``max_parallel`` (AC-RL-005).

    Headroom rules (AC-RL-002..004):
      - ratio < load_threshold  -> cap = max_parallel (full, unchanged).
      - load_threshold <= ratio < load_severe -> reduced cap
        ``max(1, max_parallel - ceil((ratio - load_threshold) * max_parallel))``.
      - ratio >= load_severe -> cap = 0 (defer wave).
    """
    global _LOAD_WARNED
    if not hasattr(os, "getloadavg"):
        if not _LOAD_WARNED:
            print("parallel: os.getloadavg unavailable on this platform; "
                  "skipping load check (dispatching at static max_parallel)",
                  file=sys.stderr)
            _LOAD_WARNED = True
        return None
    cpu = os.cpu_count() or 1
    load1 = os.getloadavg()[0]
    ratio = load1 / cpu
    max_parallel = config["max_parallel"]
    load_threshold = config.get("load_threshold", 0.7)
    load_severe = config.get("load_severe", 1.5)
    if ratio < load_threshold:
        cap = max_parallel
    elif ratio < load_severe:
        cap = max(1, max_parallel - math.ceil(
            (ratio - load_threshold) * max_parallel))
    else:
        cap = 0
    return {"ratio": ratio, "cap": cap, "deferred": cap == 0}


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
                 target: Optional[str],
                 overlap_warning: Optional[List[Tuple[str, str, str]]] = None,
                 load_cap: Optional[int] = None,
                 ) -> None:
    log = _load_parent_log(run_id, target)
    if log is None:
        return
    entry = {
        "ts": time.time(),
        "dispatched": [state._redact_evidence(i) for i in dispatched],
        "in_flight": [state._redact_evidence(i) for i in in_flight],
        "halted": [state._redact_evidence(i) for i in halted],
        "ready_count": ready_count,
    }
    # AC-FO-002: advisory overlap warning only emitted when non-empty, so the
    # wave entry is byte-identical to the pre-feature shape when there is no
    # overlap (parity for characterization tests on legacy waves).
    if overlap_warning:
        entry["overlap_warning"] = [
            (state._redact_evidence(a), state._redact_evidence(b),
             state._redact_evidence(g))
            for (a, b, g) in overlap_warning
        ]
    # ISSUE-0014 / AC-RL-003: only record ``load_cap`` when the load check
    # actually reduced the dispatch count below the requested cap, so the
    # wave entry stays byte-identical to v0.4.0 under low load (AC-RL-007).
    if load_cap is not None:
        entry["load_cap"] = load_cap
    log.setdefault("waves", []).append(entry)
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
    safe = state._redact_evidence(issue_id)
    if safe not in halted:
        halted.append(safe)
    # Record when the halt happened so _refresh_halted can drop the entry
    # if the human re-approves the issue (which bumps tasks[updated_at]).
    # Security finding 2: without this, a re-approved issue stays stuck.
    halted_at = log.setdefault("halted_at", {})
    halted_at[safe] = time.time()
    _save_parent_log(log, run_id, target)


def _refresh_halted(log: Dict[str, Any],
                    target: Optional[str]) -> List[str]:
    """Drop halted entries whose issue was touched after the halt.

    Re-approve bumps tasks[iid]['updated_at']; if that is newer than the
    halt-at timestamp, the human resolved the stale branch and re-approved
    — drop the halt so the issue can dispatch again (security finding 2).
    Returns the refreshed halted list (also persisted into the log).
    """
    halted = list(log.get("halted") or [])
    halted_at = log.get("halted_at") or {}
    tasks = state._load_tasks(target)
    kept: List[str] = []
    for iid in halted:
        ts = halted_at.get(iid)
        rec = tasks.get(iid, {}) if isinstance(tasks, dict) else {}
        updated = float(rec.get("updated_at") or 0)
        if ts is not None and updated > float(ts):
            # Re-approved after halt — drop.
            continue
        kept.append(iid)
    log["halted"] = kept
    # Clean halted_at to match.
    log["halted_at"] = {iid: t for iid, t in halted_at.items() if iid in kept}
    return kept


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
        if not _is_open_outcome(outcome):
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
    """Compute (in_flight, ready) lists.

    in_flight = ALL issues (regardless of current queue) whose status is a
    non-terminal "running" status (pm-review/ready-for-dev/in-progress/
    review/needs-fix/security-review) AND whose run is not finalized. This
    is read from tasks.json, NOT the approved queue, because a dispatched
    issue transitions approved -> pm-review and leaves the approved queue;
    counting in-flight from the approved list would miss it and un-enforce
    the max_parallel cap on the next wave (ISSUE-0010).

    The run-log-finalized guard handles the case where cmd_end finalizes a
    run (outcome=blocked/cancelled/completed, ended_at set) without
    updating tasks[issue].status -- such an issue is effectively terminal
    even though its workflow status is still e.g. pm-review. Without this
    guard it would be wrongly counted as in-flight forever.

    ready = approved issues not in_flight, not halted, and whose deps are
    satisfied (state._dependencies_satisfied). Only approved issues can be
    dispatched, so ready still iterates the approved queue. Preserves
    approved order.
    """
    tasks = state._load_tasks(target)
    in_flight: List[str] = []
    in_flight_set: set = set()
    for iid, rec in tasks.items():
        if rec.get("status") not in IN_FLIGHT_STATUSES:
            continue
        # Defense: skip issues whose run has been finalized (ended_at set).
        run_id = rec.get("run_id")
        if run_id:
            run_log = state._read_json(
                runner._run_log_path(run_id, target), default=None)
            if isinstance(run_log, dict) and run_log.get("ended_at") is not None:
                continue
        in_flight.append(iid)
        in_flight_set.add(iid)
    halted_set = set(halted)
    ready: List[str] = []
    for iid in approved:
        if iid in in_flight_set:
            continue
        if iid in halted_set:
            continue
        ok, _reason = state._dependencies_satisfied(iid, target=target)
        if ok:
            ready.append(iid)
    return in_flight, ready


def _compute_overlap_warnings(
        to_dispatch: List[str],
        target: Optional[str]) -> List[Tuple[str, str, str]]:
    """Advisory file-overlap detection over the ready set (ISSUE-0012).

    For each unordered pair (a, b) in ``to_dispatch`` and each pair of globs
    (ga in touches(a), gb in touches(b)), if ``ga`` matches ``gb`` or vice
    versa (fnmatch), record ``(a, b, ga)``. Self-pairs are excluded. Returns
    ``[]`` when no issue carries ``touches`` or no glob overlaps.

    Advisory only: the caller dispatches regardless of the result
    (AC-FO-003).
    """
    if len(to_dispatch) < 2:
        return []
    tasks = state._load_tasks(target)
    touches_by_issue: Dict[str, List[str]] = {}
    for iid in to_dispatch:
        rec = tasks.get(iid, {}) if isinstance(tasks, dict) else {}
        globs = rec.get("touches") or []
        if isinstance(globs, list) and globs:
            touches_by_issue[iid] = [str(g) for g in globs]
    if not touches_by_issue:
        return []
    warnings: List[Tuple[str, str, str]] = []
    for i in range(len(to_dispatch)):
        a = to_dispatch[i]
        ga_list = touches_by_issue.get(a)
        if not ga_list:
            continue
        for j in range(i + 1, len(to_dispatch)):
            b = to_dispatch[j]
            gb_list = touches_by_issue.get(b)
            if not gb_list:
                continue
            for ga in ga_list:
                for gb in gb_list:
                    if fnmatch.fnmatch(ga, gb) or fnmatch.fnmatch(gb, ga):
                        warnings.append((a, b, ga))
    return warnings


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
        # Drop halted entries the human has since re-approved (bumps
        # tasks[updated_at] past halt-at). Security finding 2.
        halted = _refresh_halted(parent, target)
        _save_parent_log(parent, parent_run_id, target)

    approved = list(state._load_queue(target).get("approved", []))

    in_flight, ready = _compute_sets(approved, halted, target)

    # ISSUE-0014: load-aware rate limiter. Sample the system load before
    # computing ``to_dispatch`` and clamp the dispatch cap to ``load_cap``.
    # When ``_load_headroom`` returns None (Windows, AC-RL-005) or reports
    # ``ratio < load_threshold`` (AC-RL-002), the cap equals ``max_parallel``
    # and the code path below is byte-identical to v0.4.0 (AC-RL-007).
    headroom = _load_headroom(target, config)
    load_cap: Optional[int] = None
    if headroom is not None:
        load_cap = headroom["cap"]
        if headroom["deferred"]:
            # AC-RL-004: ratio >= load_severe. Defer the whole wave:
            # dispatch nothing, record the wave, leave the parent log open
            # (resumable), exit 0.
            ratio = headroom["ratio"]
            outcome = f"wave-deferred:high-load:{ratio}"
            _append_wave(parent_run_id, [], in_flight, halted, len(ready),
                         target)
            log = _load_parent_log(parent_run_id, target)
            if log is not None:
                log["outcome"] = outcome
                _save_parent_log(log, parent_run_id, target)
            print(f"parallel: wave deferred (load ratio {ratio} >= "
                  f"{config.get('load_severe', 1.5)}); re-invoke after load "
                  f"drops")
            return parent_run_id, EXIT_OK

    static_slots = max(0, max_parallel - len(in_flight))
    if load_cap is not None and load_cap < max_parallel:
        # Reduced cap: clamp by the load-derived cap as well as in-flight.
        slots = max(0, min(load_cap, max_parallel) - len(in_flight))
    else:
        slots = static_slots
    to_dispatch = ready[:slots]

    # AC-RL-003: record ``load_cap`` only when the load check actually reduced
    # the cap below ``max_parallel``. Under low load (``cap == max_parallel``)
    # this stays None so the wave entry is byte-identical to v0.4.0 (AC-RL-007).
    recorded_load_cap: Optional[int] = load_cap if (
        load_cap is not None and load_cap < max_parallel) else None

    # AC-FO-002: advisory file-overlap warning over the ready set. Computed
    # before dispatch; dispatch proceeds regardless of the result.
    overlap_warning = _compute_overlap_warnings(to_dispatch, target)

    halted_new, failed = _dispatch_wave(parent_run_id, target, to_dispatch)

    # start-failed halts the whole wave immediately.
    if failed:
        iid, rc = failed[0]
        outcome = f"start-failed:{iid}:{rc}"
        # Refresh in_flight/halted for the wave record before finalizing.
        in_flight_after, _ready_after = _compute_sets(approved, halted, target)
        _append_wave(parent_run_id, to_dispatch, in_flight_after,
                     halted + halted_new, len(ready), target,
                     overlap_warning=overlap_warning,
                     load_cap=recorded_load_cap)
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
                 halted_after, len(ready), target,
                 overlap_warning=overlap_warning,
                 load_cap=recorded_load_cap)

    # Outcome decision.
    if not ready_after and not in_flight_after:
        outcome = "queue-exhausted"
        # ISSUE-0011: attempt integration -> main merge. Parallel queue does
        # not stack an integration branch, so _apply_main_merge is a no-op
        # (skip) there; under a serial auto-merge-branch run that shared the
        # branch it would advance to main-merged:<sha>.
        outcome = queue_runner._apply_main_merge(parent_run_id, outcome, target)
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
# Orphan worktree reconcile (ISSUE-0013)
# ---------------------------------------------------------------------------
#
# Security finding 4 (low): a crash between `git worktree add` and parent-log
# append leaves a worktree on disk with no live run-log reference. `git worktree
# prune` only removes worktrees whose DIRECTORY is gone, not the reverse, so it
# does NOT recover this case. This command scans run logs for `worktree_path`
# and reconciles against `git worktree list --porcelain`.
#
# Category rules (AC-OW-001..004):
#   - live           : on disk AND some NON-finalized run log references it.
#                      NEVER touched (AC-OW-002).
#   - orphan         : on disk AND only FINALIZED run log(s) reference it.
#                      Recoverable: the issue_id is read from the most-recent
#                      finalized log. Sweepable with --sweep.
#   - manual recovery: on disk AND NO run log references it at all (the log
#                      is missing/corrupt). Reported, NEVER auto-swept (AC-OW-004).

def _collect_worktree_refs(target: Optional[str]) \
        -> Dict[str, List[Tuple[str, bool, str]]]:
    """Map worktree_path -> list of (run_id, is_live, issue_id).

    Scans EVERY ``.harness/state/runs/*.json`` (parent parallel-queue logs AND
    single-issue child logs) for a top-level ``worktree_path``. A reference is
    "live" when the referencing log is non-finalized (``ended_at`` is None AND
    ``outcome`` is None or an open interim outcome).
    """
    runs_dir = state._runs_dir(target)
    refs: Dict[str, List[Tuple[str, bool, str]]] = {}
    if not os.path.isdir(runs_dir):
        return refs
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = state._read_json(os.path.join(runs_dir, name), default=None)
        if not isinstance(log, dict):
            continue
        wt = log.get("worktree_path")
        if not isinstance(wt, str) or not wt:
            continue
        outcome = log.get("outcome")
        ended_at = log.get("ended_at")
        # Open interim outcomes on parent logs (wave-dispatched*) keep children
        # live even before ended_at is set.
        is_open_parallel = (
            log.get("kind") == "parallel-queue"
            and (outcome is None
                 or (isinstance(outcome, str)
                     and outcome.startswith("wave-dispatched")))
        )
        is_live = (ended_at is None) and (
            outcome is None or is_open_parallel
        )
        issue_id = log.get("issue_id")
        if not isinstance(issue_id, str):
            issue_id = ""
        refs.setdefault(wt, []).append(
            (log.get("run_id") or name[:-5], is_live, issue_id))
    return refs


def _git_worktree_list(target: Optional[str]) -> List[str]:
    """Return absolute worktree paths known to git.

    Runs ``git worktree list --porcelain`` (routed through
    policy.check_command) and parses the ``worktree <path>`` stanzas. Returns
    ``[]`` when git is unavailable or the command is policy-denied (fail-safe:
    nothing to reconcile against, so the command reports zero orphans and
    exits 0).
    """
    if not runner._in_git_repo(target):
        return []
    list_cmd = "git worktree list --porcelain"
    ok, _reason = policy.check_command(list_cmd)
    if not ok:
        return []
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=target or os.getcwd(),
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    paths: List[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
    return paths


def _classify_worktrees(target: Optional[str]) -> Tuple[List[Dict[str, Any]],
                                                       List[Dict[str, Any]],
                                                       List[str]]:
    """Return (orphans, manual, live_paths).

    Each orphan/manual entry is a dict ``{path, issue_id, run_id}``. For
    orphans the issue_id/run_id come from the most-recent finalized log that
    referenced the path (started_at tie-break). Manual entries have an empty
    issue_id (no log to recover from).

    Only Laplace-managed worktrees (those living under
    ``<target>/.harness/worktrees/``) are considered. The repo's main
    worktree and any foreign worktrees are excluded so reconcile never
    reports or sweeps them.
    """
    refs = _collect_worktree_refs(target)
    on_disk = _git_worktree_list(target)
    # Restrict to Laplace-managed worktrees (runner._worktree_path puts them
    # under <target>/.harness/worktrees/<id>/). The main repo worktree and
    # any foreign worktrees are never orphans from Laplace's perspective.
    wt_root = os.path.normpath(
        os.path.join(state._harness_root(target), ".harness", "worktrees"))
    on_disk = [p for p in on_disk
               if os.path.normpath(p).startswith(wt_root + os.sep)
               or os.path.normpath(p) == wt_root]

    orphans: List[Dict[str, Any]] = []
    manual: List[Dict[str, Any]] = []
    live_paths: List[str] = []

    # First pass: classify every referenced path.
    refs_norm: Dict[str, List[Tuple[str, bool, str]]] = {}
    for wt, entries in refs.items():
        refs_norm[os.path.normpath(wt)] = entries

    for path in on_disk:
        norm = os.path.normpath(path)
        entries = refs_norm.get(norm)
        if not entries:
            # On disk, no run log references it -> manual recovery (AC-OW-004).
            manual.append({"path": path, "issue_id": "", "run_id": ""})
            continue
        any_live = any(e[1] for e in entries)
        if any_live:
            live_paths.append(path)
            continue
        # Only finalized references -> orphan. Recover issue_id from the
        # most-recent referencing log (heuristic: last in scan order).
        rid = entries[-1][0]
        iid = ""
        for _rid, _live, cand in reversed(entries):
            if cand:
                iid = cand
                break
        orphans.append({"path": path, "issue_id": iid, "run_id": rid})

    return orphans, manual, live_paths


def _reconcile_report(orphans: List[Dict[str, Any]],
                      manual: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    if orphans:
        lines.append(f"Orphan worktrees ({len(orphans)}):")
        for e in orphans:
            iid = e["issue_id"] or "?"
            lines.append(f"  {e['path']} (last issue: {iid})")
    if manual:
        lines.append(f"Manual recovery ({len(manual)}):")
        for e in manual:
            lines.append(
                f"  {e['path']} (no run log; remove manually if safe)")
    if not orphans and not manual:
        lines.append("No orphan worktrees.")
    return lines


def _remove_worktree(path: str, target: Optional[str]) -> Tuple[bool, str]:
    """`git worktree remove --force <path>` (policy-checked)."""
    rm_cmd = f"git worktree remove --force {path}"
    ok, reason = policy.check_command(rm_cmd)
    if not ok:
        return False, f"policy-denied: {reason}"
    try:
        r = subprocess.run(
            ["git", "worktree", "remove", "--force", path],
            cwd=state._harness_root(target),
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git-error: {exc}"
    if r.returncode != 0:
        return False, f"git-error: {(r.stderr or '').strip()}"
    return True, "removed"


def cmd_reconcile_worktrees(args: argparse.Namespace) -> int:
    """List and optionally sweep orphan worktrees (AC-OW-001..004)."""
    target = getattr(args, "target", None)
    sweep = bool(getattr(args, "sweep", False))
    yes = bool(getattr(args, "yes", False))

    orphans, manual, live_paths = _classify_worktrees(target)

    for line in _reconcile_report(orphans, manual):
        print(line)

    if not sweep:
        return 0

    if not orphans:
        # Nothing sweepable. Manual entries are never auto-swept (AC-OW-004).
        return 0

    # Confirmation prompt unless --yes.
    if not yes:
        print("")
        print(f"About to remove {len(orphans)} orphan worktree(s):")
        for e in orphans:
            print(f"  {e['path']}")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted")
            return 1

    removed = 0
    failures: List[str] = []
    for e in orphans:
        ok, reason = _remove_worktree(e["path"], target)
        if ok:
            removed += 1
        else:
            failures.append(f"{e['path']}: {reason}")
    print(f"swept {removed} orphan worktree(s)")
    for f in failures:
        print(f"  failed: {f}", file=sys.stderr)
    return 0 if not failures else 1


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

        # --- Reconcile (ISSUE-0013): pure-logic classification cases -------
        # These exercise _collect_worktree_refs + _classify_worktrees with
        # synthetic run logs (no git required); the git-side helpers are
        # covered by the pytest unit suite using a real repo.
        rec_tmp = tempfile.mkdtemp(prefix="laplace-reconcile-selftest-")
        try:
            assert state.cmd_init(target=rec_tmp) == 0
            runs = state._runs_dir(rec_tmp)

            def _write_log(run_id, *, kind="single", issue_id="",
                           worktree_path=None, outcome=None,
                           ended_at=None, started_at=None):
                log = {
                    "run_id": run_id,
                    "kind": kind,
                    "issue_id": issue_id,
                    "started_at": started_at if started_at is not None
                    else time.time(),
                    "ended_at": ended_at,
                    "outcome": outcome,
                    "worktree_path": worktree_path,
                }
                state._atomic_write_json(
                    os.path.join(runs, f"{run_id}.json"), log)
                return log

            # AC-OW-002: live child log (non-finalized) -> live, never swept.
            wt_base = os.path.join(
                state._harness_root(rec_tmp), ".harness", "worktrees")
            live_path = os.path.join(wt_base, "wt-live")
            _write_log("c-live", issue_id="ISSUE-LIVE",
                       worktree_path=live_path, outcome=None, ended_at=None)
            # Orphan: finalized child log (ended_at set) -> recoverable.
            orph_path = os.path.join(wt_base, "wt-orph")
            _write_log("c-orph", issue_id="ISSUE-ORPH",
                       worktree_path=orph_path, outcome="blocked",
                       ended_at=time.time())
            # Manual recovery: no run log references this path at all.
            manual_path = os.path.join(wt_base, "wt-manual")
            # Open parallel parent log referencing a child path is live.
            par_path = os.path.join(wt_base, "wt-par")
            _write_log("p-open", kind="parallel-queue",
                       worktree_path=par_path, outcome=None, ended_at=None)

            # Monkey-patch _git_worktree_list to return our synthetic on-disk
            # set so classification is testable without a real git repo.
            disk = [live_path, orph_path, manual_path, par_path,
                    os.path.join(wt_base, "wt-extra-no-log")]
            orig_list = _git_worktree_list

            def fake_list(t, _disk=disk):
                return list(_disk)

            globals()["_git_worktree_list"] = fake_list
            try:
                orphans, manual, live = _classify_worktrees(rec_tmp)
            finally:
                globals()["_git_worktree_list"] = orig_list

            live_set = {os.path.normpath(p) for p in live}
            if os.path.normpath(live_path) not in live_set:
                failures.append(
                    "reconcile: live (non-finalized) child must be live")
            if os.path.normpath(par_path) not in live_set:
                failures.append(
                    "reconcile: open parallel parent ref must be live")
            orph_paths = {os.path.normpath(e["path"]) for e in orphans}
            if os.path.normpath(orph_path) not in orph_paths:
                failures.append("reconcile: finalized-only ref must be orphan")
            else:
                entry = next(e for e in orphans
                             if os.path.normpath(e["path"])
                             == os.path.normpath(orph_path))
                if entry["issue_id"] != "ISSUE-ORPH":
                    failures.append(
                        f"reconcile: orphan issue_id recovery wrong, got "
                        f"{entry['issue_id']}")
            man_paths = {os.path.normpath(e["path"]) for e in manual}
            if os.path.normpath(manual_path) not in man_paths:
                failures.append(
                    "reconcile: ref-by-no-log must be manual recovery")
            # Extra path with NO log at all is also manual (AC-OW-004).
            if os.path.normpath(
                    os.path.join(wt_base, "wt-extra-no-log")) not in man_paths:
                failures.append(
                    "reconcile: path with no log must be manual recovery")

            # Finalized parent log (parallel-queue, queue-exhausted) is NOT
            # live -> its referenced path becomes orphan.
            fin_par_path = os.path.join(wt_base, "wt-finpar")
            _write_log("p-fin", kind="parallel-queue",
                       worktree_path=fin_par_path, outcome="queue-exhausted",
                       ended_at=time.time())
            disk2 = disk + [fin_par_path]

            def fake_list2(t, _disk=disk2):
                return list(_disk)

            globals()["_git_worktree_list"] = fake_list2
            try:
                orphans2, _manual2, _live2 = _classify_worktrees(rec_tmp)
            finally:
                globals()["_git_worktree_list"] = orig_list
            fin_orph = {os.path.normpath(e["path"]) for e in orphans2}
            if os.path.normpath(fin_par_path) not in fin_orph:
                failures.append(
                    "reconcile: finalized parallel-queue ref must be orphan")
        finally:
            shutil.rmtree(rec_tmp, ignore_errors=True)

        # --- Case 8-11: load-aware rate limiter (ISSUE-0014) ------------
        # Mock os.getloadavg to drive each headroom branch. cpu_count is
        # monkey-patched so ratios are deterministic across hosts. Each case
        # uses a fresh parent log: the prior case's open run is finalized
        # (queue-exhausted) and its locks/child runs cleared so the next
        # case starts clean (mirrors how the original cases isolate state).
        def _reset_for_load_case(prefix: str, n: int = 5) -> None:
            state._save_tasks({}, target=tmp)
            state._save_queue(state.DEFAULT_QUEUE, target=tmp)
            # Wipe run logs + locks so prior waves' children don't hold
            # locks that would make cmd_start fail for re-used issue ids.
            locks_dir = os.path.join(state._state_dir(tmp), "locks")
            for d in (state._runs_dir(tmp), locks_dir):
                if os.path.isdir(d):
                    for fn in os.listdir(d):
                        if fn.endswith(".json") or fn.endswith(".lock"):
                            try:
                                os.remove(os.path.join(d, fn))
                            except OSError:
                                pass
            for i in range(n):
                seed_approved(f"ISSUE-{prefix}{i}")

        orig_cpu = os.cpu_count
        orig_loadavg = getattr(os, "getloadavg", None)

        def _force_load(ratio: float, cpu: int = 4):
            os.cpu_count = lambda: cpu
            os.getloadavg = lambda: (ratio * cpu, 0.0, 0.0)

        try:
            # Case 8: low load (ratio < threshold) -> full cap, no load_cap
            # recorded, wave entry byte-identical to v0.4.0 (AC-RL-002/007).
            _reset_for_load_case("L")
            _force_load(0.1, cpu=4)
            rid8, rc8 = _run_parallel_wave(tmp, cfg)
            if rc8 != 0:
                failures.append(f"case8: low load should exit 0, got {rc8}")
            log8 = _load_parent_log(rid8, tmp)
            waves8 = (log8 or {}).get("waves") or []
            if waves8:
                if "load_cap" in waves8[-1]:
                    failures.append(
                        "case8: low load must NOT record load_cap (AC-RL-007)")
                if len(waves8[-1].get("dispatched") or []) != 2:
                    failures.append(
                        "case8: low load should dispatch full max_parallel=2")

            # Case 9: mid load (threshold <= ratio < severe) -> reduced cap.
            # max_parallel=2, threshold=0.7, severe=1.5, ratio=1.0 ->
            # ceil((1.0-0.7)*2)=ceil(0.6)=1 -> cap=max(1,2-1)=1. load_cap=1.
            _reset_for_load_case("M")
            _force_load(1.0, cpu=4)
            rid9, rc9 = _run_parallel_wave(tmp, cfg)
            if rc9 != 0:
                failures.append(f"case9: mid load should exit 0, got {rc9}")
            log9 = _load_parent_log(rid9, tmp)
            waves9 = (log9 or {}).get("waves") or []
            if waves9:
                if waves9[-1].get("load_cap") != 1:
                    failures.append(
                        f"case9: expected load_cap=1, got "
                        f"{waves9[-1].get('load_cap')}")
                if len(waves9[-1].get("dispatched") or []) != 1:
                    failures.append(
                        f"case9: expected 1 dispatched under reduced cap, got "
                        f"{len(waves9[-1].get('dispatched') or [])}")

            # Case 10: severe load (ratio >= severe) -> defer wave, nothing
            # dispatched, outcome wave-deferred:high-load:<ratio>, exit 0
            # (AC-RL-004).
            _reset_for_load_case("N")
            _force_load(2.0, cpu=4)
            rid10, rc10 = _run_parallel_wave(tmp, cfg)
            if rc10 != 0:
                failures.append(f"case10: severe load should exit 0, got {rc10}")
            log10 = _load_parent_log(rid10, tmp)
            outcome10 = (log10 or {}).get("outcome")
            if not (isinstance(outcome10, str)
                    and outcome10.startswith("wave-deferred:high-load:")):
                failures.append(
                    f"case10: expected wave-deferred:high-load:<ratio>, got "
                    f"{outcome10}")
            waves10 = (log10 or {}).get("waves") or []
            if waves10 and (waves10[-1].get("dispatched") or []):
                failures.append(
                    "case10: severe load must dispatch nothing")
            # No issue should have left the approved state.
            for i in range(5):
                if _read_issue_status(f"ISSUE-N{i}", tmp) != "approved":
                    failures.append(
                        f"case10: ISSUE-N{i} must stay approved under severe load")
            # Parent log stays resumable: _find_open_parallel_run finds it.
            reopened = _find_open_parallel_run(tmp)
            if not reopened or reopened.get("run_id") != rid10:
                failures.append(
                    "case10: deferred wave must remain resumable (open)")

            # Case 11: severe -> mid transition resumes and dispatches.
            # Re-invoke the deferred run under mid load: it should now
            # dispatch at the reduced cap (AC-RL-004 resumability).
            _force_load(1.0, cpu=4)
            rid11, rc11 = _run_parallel_wave(tmp, cfg)
            if rid11 != rid10:
                failures.append(
                    "case11: should resume same parent run after load drops")
            if rc11 != 0:
                failures.append(f"case11: resumed wave should exit 0, got {rc11}")
            log11 = _load_parent_log(rid11, tmp)
            waves11 = (log11 or {}).get("waves") or []
            if not waves11 or not (waves11[-1].get("dispatched") or []):
                failures.append(
                    "case11: resumed wave should dispatch under mid load")
        finally:
            os.cpu_count = orig_cpu
            if orig_loadavg is not None:
                os.getloadavg = orig_loadavg
            else:
                del os.getloadavg
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

    p = sub.add_parser(
        "reconcile-worktrees",
        help="List and optionally sweep orphan worktrees (ISSUE-0013)")
    _add_target_arg(p)
    p.add_argument("--sweep", action="store_true",
                   help="Remove orphan worktrees (never live or manual)")
    p.add_argument("--yes", action="store_true",
                   help="Skip confirmation prompt when used with --sweep")
    p.set_defaults(func=cmd_reconcile_worktrees)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
