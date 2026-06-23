"""SPEC-004: upstream blocker propagation.

Covers AC-001..AC-006. Exercises state.propagate_upstream_blocks against
a temp harness with synthetic dependency graphs.
"""
import argparse
import os
import sys
import tempfile
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import state  # noqa: E402


def _fresh_harness() -> str:
    tmp = tempfile.mkdtemp(prefix="laplace-spec004-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _seed(target: str, issues):
    """issues: list of (id, status, depends_on_list[, run_id])."""
    tasks = {}
    for row in issues:
        iid, status, deps = row[0], row[1], row[2]
        run_id = row[3] if len(row) > 3 else None
        rec = {"status": status, "updated_at": time.time(), "depends_on": deps}
        if run_id:
            rec["run_id"] = run_id
        tasks[iid] = rec
    state._save_tasks(tasks, target=target)
    q = state._load_queue(target=target)
    for s in state.QUEUE_STATES:
        q[s] = []
    for iid, rec in tasks.items():
        if rec["status"] in state.QUEUE_STATES:
            q[rec["status"]].append(iid)
    state._save_queue(q, target=target)


def _status(target: str, iid: str) -> str:
    return state._load_tasks(target=target).get(iid, {}).get("status")


def _block_reason(target: str, iid: str) -> str:
    return state._load_tasks(target=target).get(iid, {}).get("block_reason", "")


def test_chain_blocks_transitively():
    """AC-001/AC-002: A<-B<-C, block A -> B blocked, then C blocked."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "blocked", []),
        ("ISSUE-B", "approved", ["ISSUE-A"]),
        ("ISSUE-C", "approved", ["ISSUE-B"]),
    ])
    applied = state.propagate_upstream_blocks(tmp)
    assert _status(tmp, "ISSUE-B") == "blocked"
    assert _block_reason(tmp, "ISSUE-B") == "upstream:ISSUE-A:blocked"
    assert _status(tmp, "ISSUE-C") == "blocked"
    assert _block_reason(tmp, "ISSUE-C") == "upstream:ISSUE-B:blocked"
    assert len(applied) == 2


def test_success_terminal_does_not_propagate():
    """AC-003: A<-B, A=review-passed -> B NOT blocked."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "review-passed", []),
        ("ISSUE-B", "approved", ["ISSUE-A"]),
    ])
    applied = state.propagate_upstream_blocks(tmp)
    assert applied == []
    assert _status(tmp, "ISSUE-B") == "approved"


def test_terminal_dependent_not_mutated():
    """AC-004: A<-B, B already release-candidate when A blocks -> B unchanged."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "blocked", []),
        ("ISSUE-B", "release-candidate", ["ISSUE-A"]),
    ])
    applied = state.propagate_upstream_blocks(tmp)
    assert applied == []
    assert _status(tmp, "ISSUE-B") == "release-candidate"


def test_human_approval_required_propagates():
    """AC-005: A<-B, A=human-approval-required -> B blocked (NEW behavior).
    Previously B was dispatched because human-approval-required is in
    TERMINAL_STATES and _dependencies_satisfied treated it as satisfied."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "human-approval-required", []),
        ("ISSUE-B", "approved", ["ISSUE-A"]),
    ])
    applied = state.propagate_upstream_blocks(tmp)
    assert len(applied) == 1
    assert _status(tmp, "ISSUE-B") == "blocked"
    assert _block_reason(tmp, "ISSUE-B") == "upstream:ISSUE-A:human-approval-required"


def test_idempotent_rerun():
    """Re-running propagation on an already-propagated state yields no new blocks."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "blocked", []),
        ("ISSUE-B", "approved", ["ISSUE-A"]),
    ])
    state.propagate_upstream_blocks(tmp)
    second = state.propagate_upstream_blocks(tmp)
    assert second == []
    assert _status(tmp, "ISSUE-B") == "blocked"


def test_persistence_after_block():
    """AC-006: blocked dependents persist across a reload (no re-dispatch)."""
    tmp = _fresh_harness()
    _seed(tmp, [
        ("ISSUE-A", "cancelled", []),
        ("ISSUE-B", "approved", ["ISSUE-A"]),
    ])
    state.propagate_upstream_blocks(tmp)
    # Reload from disk explicitly.
    tasks = state._load_tasks(target=tmp)
    assert tasks["ISSUE-B"]["status"] == "blocked"
    # B is no longer in approved queue.
    q = state._load_queue(target=tmp)
    assert "ISSUE-B" not in q["approved"]
    assert "ISSUE-B" in q["blocked"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} SPEC-004 tests passed")
