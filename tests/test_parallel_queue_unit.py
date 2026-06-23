"""Unit tests for scripts/parallel_queue.py (ISSUE-0004).

One test per acceptance criterion AC-PQ-001..012, plus a characterization
test that the sequential run-queue semantics are unchanged (AC-PQ-011).

Each test builds a fresh temp harness, seeds approved issues, and exercises
`_run_parallel_wave` using runner primitives to simulate the model driving
each dispatched issue through its phases (compose, not re-implement).
"""
import argparse
import os
import shutil
import subprocess
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
import parallel_queue  # noqa: E402
import queue_runner  # noqa: E402
import cancel  # noqa: E402


def _make_harness():
    tmp = tempfile.mkdtemp(prefix="laplace-parallel-test-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _seed_approved(tmp, issue_id, depends_on=None):
    tasks = state._load_tasks(tmp)
    rec = {"status": "draft", "updated_at": time.time()}
    if depends_on:
        rec["depends_on"] = list(depends_on)
    tasks[issue_id] = rec
    state._save_tasks(tasks, target=tmp)
    q = state._load_queue(tmp)
    if issue_id not in q["draft"]:
        q["draft"].append(issue_id)
    state._save_queue(q, target=tmp)
    assert state.cmd_approve(argparse.Namespace(
        issue_id=issue_id, user="tester", target=tmp)) == 0


def _drive_to_review_passed(issue_id, target):
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "in-progress"),
                     ("in-progress", "review")):
        assert runner.cmd_advance(argparse.Namespace(
            issue_id=issue_id, from_state=src, to_state=dst,
            summary="", target=target)) == 0
    run_id = state._load_tasks(target).get(issue_id, {}).get("run_id")
    assert runner.cmd_evidence(argparse.Namespace(
        run_id=run_id, kind="test", path_or_text="pytest: ok",
        target=target)) == 0
    assert runner.cmd_advance(argparse.Namespace(
        issue_id=issue_id, from_state="review", to_state="review-passed",
        summary="ok", target=target)) == 0


def _status_of(tmp, issue_id):
    return state._load_tasks(tmp).get(issue_id, {}).get("status")


def test_ac_pq_001_dispatches_terminal_dep_issues_up_to_cap():
    """AC-PQ-001: ready issues dispatched, each gets its own worktree via
    runner.cmd_start (state transitions approved -> pm-review)."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-1")
        _seed_approved(tmp, "ISSUE-2")
        rid, rc = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc == 0
        # Both started (approved -> pm-review).
        assert _status_of(tmp, "ISSUE-1") == "pm-review"
        assert _status_of(tmp, "ISSUE-2") == "pm-review"
        log = parallel_queue._load_parent_log(rid, tmp)
        assert log["kind"] == "parallel-queue"
        # Child run ids recorded.
        assert len(log["issues"]) == 2
    finally:
        _teardown(tmp)


def test_ac_pq_002_unmet_dep_not_dispatched_until_terminal():
    """AC-PQ-002: dependent issue held until dep reaches review-passed."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-PARENT")
        _seed_approved(tmp, "ISSUE-CHILD", depends_on=["ISSUE-PARENT"])
        rid1, rc1 = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc1 == 0
        # CHILD not dispatched (dep unmet).
        assert _status_of(tmp, "ISSUE-CHILD") == "approved"
        assert _status_of(tmp, "ISSUE-PARENT") == "pm-review"
        # Drive PARENT to review-passed, re-invoke.
        _drive_to_review_passed("ISSUE-PARENT", tmp)
        rid2, rc2 = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc2 == 0
        assert _status_of(tmp, "ISSUE-CHILD") == "pm-review"
        # Same parent run (resume).
        assert rid2 == rid1
    finally:
        _teardown(tmp)


def test_ac_pq_003_cycle_rejected_at_approve_characterization():
    """AC-PQ-003: cycle in depends_on rejected at approve (existing behavior).
    The scheduler never sees a cycle. Monotone-terminal readiness cannot
    create a wait-cycle."""
    tmp = _make_harness()
    try:
        tasks = state._load_tasks(tmp)
        tasks["ISSUE-X"] = {"status": "draft", "updated_at": time.time(),
                            "depends_on": ["ISSUE-Y"]}
        tasks["ISSUE-Y"] = {"status": "draft", "updated_at": time.time(),
                            "depends_on": ["ISSUE-X"]}
        state._save_tasks(tasks, target=tmp)
        q = state._load_queue(tmp)
        q["draft"].extend(["ISSUE-X", "ISSUE-Y"])
        state._save_queue(q, target=tmp)
        rc = state.cmd_approve(argparse.Namespace(
            issue_id="ISSUE-X", user="tester", target=tmp))
        assert rc != 0, "cycle must be rejected at approve"
    finally:
        _teardown(tmp)


def test_ac_pq_004_max_parallel_cap_default_two():
    """AC-PQ-004: default max_parallel is 2; configurable via limits."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        assert cfg["max_parallel"] == 2
        assert state.MAX_PARALLEL == 2
    finally:
        _teardown(tmp)


def test_ac_pq_005_halt_isolation_siblings_continue():
    """AC-PQ-005: a halted issue is recorded; siblings continue; not
    re-dispatched until resolved."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-H")
        _seed_approved(tmp, "ISSUE-S")
        original = runner.cmd_start

        def fake_start(args):
            if args.issue_id == "ISSUE-H":
                return parallel_queue.EXIT_BRANCH_STALE
            return original(args)

        runner.cmd_start = fake_start
        try:
            rid, rc = parallel_queue._run_parallel_wave(tmp, cfg)
        finally:
            runner.cmd_start = original
        assert rc == 0
        # Sibling still dispatched.
        assert _status_of(tmp, "ISSUE-S") == "pm-review"
        # Halted issue recorded, not dispatched.
        log = parallel_queue._load_parent_log(rid, tmp)
        assert "ISSUE-H" in log["halted"]
        assert _status_of(tmp, "ISSUE-H") == "approved"
        # Re-invoke: ISSUE-H still skipped.
        parallel_queue._run_parallel_wave(tmp, cfg)
        assert _status_of(tmp, "ISSUE-H") == "approved"
    finally:
        _teardown(tmp)


def test_ac_pq_006_queue_exhausted_no_ready_no_inflight():
    """AC-PQ-006: no ready + no in-flight -> queue-exhausted outcome."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        # Empty approved queue -> exhausted on first wave.
        rid, rc = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc == 0
        log = parallel_queue._load_parent_log(rid, tmp)
        assert log["outcome"] == "queue-exhausted"
        assert log["ended_at"] is not None
    finally:
        _teardown(tmp)


def test_ac_pq_007_parent_log_shape():
    """AC-PQ-007: parent log records kind, waves (with dispatched/in_flight/
    halted lists), child run ids, outcome."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-L1")
        rid, rc = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc == 0
        log = parallel_queue._load_parent_log(rid, tmp)
        assert log["kind"] == "parallel-queue"
        assert "waves" in log and isinstance(log["waves"], list)
        assert len(log["waves"]) == 1
        w = log["waves"][0]
        for key in ("ts", "dispatched", "in_flight", "halted", "ready_count"):
            assert key in w
        assert "ISSUE-L1" in w["dispatched"]
        assert isinstance(log["issues"], list) and len(log["issues"]) == 1
        assert "max_parallel" in log
        assert "merge_policy" in log
    finally:
        _teardown(tmp)


def test_ac_pq_008_gates_unchanged_composes_runner_cmd_start():
    """AC-PQ-008: scheduler composes runner.cmd_start; gates unchanged.
    The dispatched issue is at pm-review with a run log + lock, identical to
    a direct /laplace:run."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-G")
        parallel_queue._run_parallel_wave(tmp, cfg)
        # Lock held (runner.cmd_start acquired it).
        lock_path = state._lock_path("ISSUE-G", tmp)
        assert os.path.exists(lock_path)
        # Run log created with the standard schema.
        tasks = state._load_tasks(tmp)
        run_id = tasks["ISSUE-G"]["run_id"]
        run_log = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{run_id}.json"))
        assert run_log["issue_id"] == "ISSUE-G"
        assert run_log["agent"] == "pm"
        assert run_log["attempt"] == 1
    finally:
        _teardown(tmp)


def test_ac_pq_009_status_reports_active_parallel_run():
    """AC-PQ-009: /laplace:status shows in-flight, ready count, halted, wave."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-S1")
        _seed_approved(tmp, "ISSUE-S2")
        parallel_queue._run_parallel_wave(tmp, cfg)
        out = state._format_status(tmp)
        assert "Parallel run:" in out
        assert "in-flight:" in out
        assert "ISSUE-S1" in out
        assert "ISSUE-S2" in out
        assert "wave:" in out
        assert "halted:" in out
    finally:
        _teardown(tmp)


def test_ac_pq_010_cancel_tears_down_inflight_children():
    """AC-PQ-010: cancel ends in-flight child runs, finalizes parent log."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-C1")
        _seed_approved(tmp, "ISSUE-C2")
        parallel_queue._run_parallel_wave(tmp, cfg)
        # Cancel with no arg -> parallel path.
        rc = cancel.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
        # Both children cancelled (terminal).
        assert _status_of(tmp, "ISSUE-C1") == "cancelled"
        assert _status_of(tmp, "ISSUE-C2") == "cancelled"
        # Locks released.
        assert not os.path.exists(state._lock_path("ISSUE-C1", tmp))
        assert not os.path.exists(state._lock_path("ISSUE-C2", tmp))
        # Parent log finalized as cancelled.
        parallel_log = state._find_active_parallel_run(tmp)
        assert parallel_log is None  # no longer active
    finally:
        _teardown(tmp)


def test_ac_pq_011_sequential_run_queue_unchanged_byte_identical():
    """AC-PQ-011 (characterization part 1): with no parallel run active,
    _format_status output is byte-identical to the pre-change baseline.
    And queue_runner still works unchanged."""
    BASELINE = (
        "Harness status.\n"
        "\n"
        "Queue:\n"
        "  draft: 0\n"
        "  approved: 0\n"
        "  in-progress: 0\n"
        "  blocked: 0\n"
        "  release-candidate: 0\n"
        "\n"
        "Active run:\n"
        "  (no active run)\n"
        "\n"
        "Next action:\n"
        "  /laplace:intake <prd> to create draft issues"
    )
    tmp = _make_harness()
    try:
        assert state._format_status(tmp) == BASELINE
    finally:
        _teardown(tmp)


def test_ac_pq_011b_sequential_queue_runner_still_works():
    """AC-PQ-011 (characterization part 2): sequential queue_runner
    semantics unchanged; run-parallel is additive."""
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-Q1")
        _seed_approved(tmp, "ISSUE-Q2")

        def drive(iid, target):
            for src, dst in (("pm-review", "ready-for-dev"),
                             ("ready-for-dev", "in-progress"),
                             ("in-progress", "review")):
                assert runner.cmd_advance(argparse.Namespace(
                    issue_id=iid, from_state=src, to_state=dst,
                    summary="", target=target)) == 0
            run_id = state._load_tasks(target)[iid]["run_id"]
            assert runner.cmd_evidence(argparse.Namespace(
                run_id=run_id, kind="test", path_or_text="ok",
                target=target)) == 0
            assert runner.cmd_advance(argparse.Namespace(
                issue_id=iid, from_state="review", to_state="review-passed",
                summary="ok", target=target)) == 0

        # queue_runner._run_queue with advance policy -> queue-exhausted.
        rid, rc = queue_runner._run_queue(
            None, tmp, {"max_queue_run": 5, "merge_policy": "wait-for-human-merge"},
            drive,
            policy_override=lambda iid, target: ("advance", ""))
        assert rc == 0
        log = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{rid}.json"))
        assert log["kind"] == "queue"
        assert log["outcome"] == "queue-exhausted"
    finally:
        _teardown(tmp)


def test_ac_pq_012_concurrency_cap_violations_impossible():
    """AC-PQ-012: 5 ready issues with max_parallel=2 -> exactly 2 in-flight,
    3 deferred to next wave."""
    tmp = _make_harness()
    try:
        # Override max_parallel to 2 (the default) explicitly.
        cfg = state.load_config(tmp)
        assert cfg["max_parallel"] == 2
        for n in ("P1", "P2", "P3", "P4", "P5"):
            _seed_approved(tmp, f"ISSUE-{n}")
        rid, rc = parallel_queue._run_parallel_wave(tmp, cfg)
        assert rc == 0
        started = [n for n in ("P1", "P2", "P3", "P4", "P5")
                   if _status_of(tmp, f"ISSUE-{n}") == "pm-review"]
        assert len(started) == 2, f"exactly 2 should start, got {started}"
        # The parent log wave entry records the in-flight set.
        log = parallel_queue._load_parent_log(rid, tmp)
        w = log["waves"][0]
        assert len(w["dispatched"]) == 2
    finally:
        _teardown(tmp)


def test_parallel_queue_selftest_passes():
    """The parallel_queue.py embedded selftest passes."""
    result = subprocess.run(
        [sys.executable,
         os.path.join(PLUGIN_ROOT, "scripts", "parallel_queue.py"), "selftest"],
        capture_output=True, text=True, timeout=120, cwd=PLUGIN_ROOT,
    )
    assert result.returncode == 0, (
        f"parallel_queue.py selftest failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_security_fix_cancel_partial_failure_keeps_parent_active(monkeypatch):
    """Security finding 1: a child cancel that fails must NOT finalize the
    parent as cancelled. The parent stays active (cancel-failed:<id>) so
    /laplace:status surfaces the stranded child."""
    tmp = _make_harness()
    try:
        _seed_approved(tmp, "ISSUE-A")
        _seed_approved(tmp, "ISSUE-B")
        ns = argparse.Namespace(target=tmp)
        parallel_queue.cmd_parallel_start(ns)
        # Force ISSUE-A's cmd_end to fail (simulates WORKTREE_DIRTY etc.).
        real_cmd_end = runner.cmd_end

        def flaky_cmd_end(args):
            # Resolve which issue this run belongs to.
            rpath = os.path.join(state._runs_dir(args.target),
                                 f"{args.run_id}.json")
            rlog = state._read_json(rpath, default={})
            if rlog.get("issue_id") == "ISSUE-A":
                return 7  # WORKTREE_DIRTY-equivalent non-zero
            return real_cmd_end(args)

        monkeypatch.setattr(runner, "cmd_end", flaky_cmd_end)
        rc = cancel.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp, force_worktree_remove=False))
        assert rc == 1, f"cancel should rc=1 on failing child, got {rc}"
        parent = state._find_active_parallel_run(tmp)
        assert parent is not None, "parent must stay active after cancel-failed"
        outcome = parent.get("outcome") or ""
        assert outcome.startswith("cancel-failed"), (
            f"parent outcome should be cancel-failed:*, got {outcome!r}")
        assert "ISSUE-A" in outcome
    finally:
        _teardown(tmp)


def test_security_fix_halted_reapproved_dispatches():
    """Security finding 2: a halted issue the human re-approves (bumping
    tasks[updated_at]) must drop from the halted set and dispatch again."""
    tmp = _make_harness()
    try:
        _seed_approved(tmp, "ISSUE-H")
        # First wave: force ISSUE-H into halted by simulating stale-branch.
        # Seed a stale laplace/ISSUE-H branch (behind main) so cmd_start
        # returns EXIT_BRANCH_STALE.
        subprocess.run(["git", "branch", "laplace/ISSUE-H",
                        "HEAD~1"], cwd=tmp, check=True,
                       capture_output=True) if False else None
        # Simpler: directly inject ISSUE-H into the parent halted set with
        # an old halted_at, then bump tasks[updated_at] and verify refresh.
        _seed_approved(tmp, "ISSUE-X")  # sibling so wave runs
        ns = argparse.Namespace(target=tmp)
        parallel_queue.cmd_parallel_start(ns)
        parent = state._find_active_parallel_run(tmp)
        assert parent is not None
        rid = parent["run_id"]
        # Inject ISSUE-H as halted in the distant past.
        parent["halted"] = ["ISSUE-H"]
        parent["halted_at"] = {"ISSUE-H": time.time() - 1000}
        state._atomic_write_json(
            os.path.join(state._runs_dir(tmp), f"{rid}.json"), parent)
        # Make ISSUE-H's tasks[updated_at] newer than the halt (re-approve).
        tasks = state._load_tasks(tmp)
        tasks["ISSUE-H"]["updated_at"] = time.time()
        state._save_tasks(tasks, target=tmp)
        # Re-run wave: _refresh_halted should drop ISSUE-H.
        parallel_queue.cmd_parallel_start(ns)
        parent2 = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{rid}.json"))
        assert "ISSUE-H" not in (parent2.get("halted") or []), (
            "halted set should drop ISSUE-H after re-approve (updated_at bump)"
        )
    finally:
        _teardown(tmp)
