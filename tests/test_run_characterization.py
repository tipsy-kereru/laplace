"""Characterization tests for the single-issue `/laplace:run` flow (ISSUE-0009).

AC-QR-024: prove that the single-issue run lifecycle (`runner.cmd_start` ->
`runner.cmd_advance`* -> `runner.cmd_end`) is unchanged after the queue-runner
work (ISSUE-0001..ISSUE-0008). These tests exercise the runner primitives
directly via `argparse.Namespace`, mirroring how `runner.py main` dispatches,
and assert the run-log shape, lock release, and absence of any queue artifact.

No production code is imported from queue_runner here -- the point is that a
single-issue run must NOT produce a queue-kind parent log. We scan the runs
directory to prove it.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import state  # noqa: E402
import runner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (local copies to keep this file self-contained; see ISSUE-0009 task
# note -- extracting to conftest would be a drive-by edit to existing tests).
# ---------------------------------------------------------------------------

def _make_harness():
    """Create a temp harness dir with config.yml + empty state."""
    tmp = tempfile.mkdtemp(prefix="laplace-char-test-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _seed_approved(tmp, issue_id):
    tasks = state._load_tasks(tmp)
    tasks[issue_id] = {"status": "draft", "updated_at": time.time()}
    state._save_tasks(tasks, target=tmp)
    q = state._load_queue(tmp)
    if issue_id not in q["draft"]:
        q["draft"].append(issue_id)
    state._save_queue(q, target=tmp)
    assert state.cmd_approve(argparse.Namespace(
        issue_id=issue_id, user="tester", target=tmp)) == 0


def _drive_to_review_passed(issue_id, target):
    """Drive an issue from pm-review through review-passed via runner.cmd_advance.

    Mirrors the legal transition chain used by the `/laplace:run` skill. Captures
    a test-evidence entry before the review -> review-passed gate (AC-LP-008).
    """
    tasks = state._load_tasks(target)
    run_id = tasks.get(issue_id, {}).get("run_id")
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "in-progress"),
                     ("in-progress", "review")):
        assert runner.cmd_advance(argparse.Namespace(
            issue_id=issue_id, from_state=src, to_state=dst,
            summary="", target=target)) == 0
    # AC-LP-008: review-passed requires a test-evidence entry in the run log.
    assert runner.cmd_evidence(argparse.Namespace(
        run_id=run_id, kind="test", path_or_text="pytest: ok",
        target=target)) == 0
    assert runner.cmd_advance(argparse.Namespace(
        issue_id=issue_id, from_state="review", to_state="review-passed",
        summary="ok", target=target)) == 0
    return run_id


def _run_log(tmp, run_id):
    return state._read_json(
        os.path.join(state._runs_dir(tmp), f"{run_id}.json"), default=None)


def _all_run_logs(tmp):
    """Return every run-log dict in the harness runs dir."""
    runs_dir = state._runs_dir(tmp)
    if not os.path.isdir(runs_dir):
        return []
    out = []
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = state._read_json(os.path.join(runs_dir, name), default=None)
        if isinstance(log, dict):
            out.append(log)
    return out


# ---------------------------------------------------------------------------
# AC-QR-024: single-issue run lifecycle unchanged
# ---------------------------------------------------------------------------

def test_single_issue_run_start_advance_end_flow():
    """Full single-issue run: start -> advances -> end.

    Asserts:
    - run log has the expected fields (issue_id, evidence, transitions, branch).
    - the issue lock is released after cmd_end.
    - the task status is terminal (review-passed).
    - NO queue-kind run log is produced (single-issue run must not spawn a
      parent queue log).
    """
    tmp = _make_harness()
    try:
        issue_id = "ISSUE-CHAR-1"
        _seed_approved(tmp, issue_id)

        # start: approved -> pm-review, creates branch (skipped: temp dir is
        # not a git repo), acquires lock, writes run log.
        assert runner.cmd_start(argparse.Namespace(
            issue_id=issue_id, target=tmp)) == 0

        tasks = state._load_tasks(tmp)
        run_id = tasks[issue_id]["run_id"]
        assert run_id, "cmd_start must set run_id on the task"

        # Lock file must exist while the run is active.
        lock_path = state._lock_path(issue_id, tmp)
        assert os.path.exists(lock_path), "lock must be held during run"

        # Drive through the legal transitions to review-passed.
        _drive_to_review_passed(issue_id, tmp)

        # end: finalize run log, release lock.
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=tmp)) == 0

        # --- Run log shape ---
        log = _run_log(tmp, run_id)
        assert log is not None, "run log must exist"
        assert log["issue_id"] == issue_id
        assert log["run_id"] == run_id
        assert log["started_at"] is not None
        assert log["ended_at"] is not None
        assert log["outcome"] == "completed"
        # evidence: at least the test entry captured before review-passed.
        assert isinstance(log["evidence"], list)
        assert any(e.get("kind") == "test" for e in log["evidence"]), \
            "run log must contain the test evidence entry"
        # transitions: start + 4 advances recorded.
        assert isinstance(log["transitions"], list)
        transitions = [(t["from"], t["to"]) for t in log["transitions"]]
        assert ("pm-review", "ready-for-dev") in transitions
        assert ("ready-for-dev", "in-progress") in transitions
        assert ("in-progress", "review") in transitions
        assert ("review", "review-passed") in transitions
        # branch field present (skipped status since temp dir is non-repo).
        assert "branch" in log
        assert log["branch"]["status"] == "skipped"

        # --- Lock released ---
        assert not os.path.exists(lock_path), "cmd_end must release the lock"

        # --- Task status terminal ---
        tasks = state._load_tasks(tmp)
        assert tasks[issue_id]["status"] == "review-passed"

        # --- NO queue-kind artifact ---
        all_logs = _all_run_logs(tmp)
        assert all(log.get("kind") != "queue" for log in all_logs), \
            "single-issue run must not produce a queue parent log"
        # Exactly one run log (the single-issue one).
        assert len(all_logs) == 1, \
            f"expected exactly one run log, got {len(all_logs)}"
    finally:
        _teardown(tmp)


def test_single_issue_run_log_shape_unchanged():
    """Snapshot the run-log dict keys and value types for a single-issue run.

    Regression guard: if the run-log schema drifts (a field is renamed, a type
    changes), this test fails and forces a deliberate update. The set of keys
    and their value types are the contract the queue runner, status command,
    and report command depend on.
    """
    tmp = _make_harness()
    try:
        issue_id = "ISSUE-CHAR-2"
        _seed_approved(tmp, issue_id)
        assert runner.cmd_start(argparse.Namespace(
            issue_id=issue_id, target=tmp)) == 0
        run_id = state._load_tasks(tmp)[issue_id]["run_id"]
        _drive_to_review_passed(issue_id, tmp)
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=tmp)) == 0

        log = _run_log(tmp, run_id)
        assert log is not None

        # Expected key -> value type contract. `kind` is absent on single-issue
        # run logs (only queue parent logs carry kind=="queue"); its absence is
        # itself part of the characterization. `worktree_path` is hoisted to
        # top-level (ISSUE-0002 / AC-WT-009); it is None in this non-repo
        # harness (BRANCH_SKIPPED) and a str when a worktree was created.
        # `worktree_teardown` is recorded by `runner.cmd_end` after teardown
        # ("removed" | "dirty-halt" | "skipped").
        expected = {
            "run_id": str,
            "issue_id": str,
            "started_at": float,
            "ended_at": float,
            "outcome": str,
            "agent": str,
            "attempt": int,
            "evidence": list,
            "transitions": list,
            "branch": dict,
            "worktree_path": type(None),
            "worktree_teardown": str,
        }
        assert set(log.keys()) == set(expected.keys()), \
            f"run log keys drifted: {set(log.keys())} != {set(expected.keys())}"
        for key, typ in expected.items():
            assert isinstance(log[key], typ), \
                f"run log {key!r} type drifted: expected {typ.__name__}, " \
                f"got {type(log[key]).__name__}"

        # Single-issue run must NOT carry queue-only keys.
        for queue_key in ("kind", "queue_steps", "issues", "max_queue_run",
                          "merge_policy", "start_issue"):
            assert queue_key not in log, \
                f"single-issue run log must not carry queue key {queue_key!r}"

        # Transitions entry shape.
        t = log["transitions"][0]
        assert set(t.keys()) == {"ts", "from", "to", "summary"}
        assert isinstance(t["ts"], float)
        assert isinstance(t["from"], str)
        assert isinstance(t["to"], str)
        assert isinstance(t["summary"], str)

        # Evidence entry shape.
        e = log["evidence"][0]
        assert "ts" in e and isinstance(e["ts"], float)
        assert "kind" in e and isinstance(e["kind"], str)
        assert "summary" in e and isinstance(e["summary"], str)

        # Branch dict shape: name + status always; reason present iff non-empty.
        b = log["branch"]
        assert "name" in b and isinstance(b["name"], str)
        assert "status" in b and isinstance(b["status"], str)
        if "reason" in b:
            assert isinstance(b["reason"], str)
    finally:
        _teardown(tmp)
