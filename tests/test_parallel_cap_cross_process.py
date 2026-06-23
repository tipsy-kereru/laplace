"""Cross-process integration test for the parallel scheduler cap (ISSUE-0010).

This test exists because the in-process selftest (test_ac_pq_012 and
parallel_queue.selftest case 3) drives a SINGLE _run_parallel_wave call and
never re-invokes the scheduler while issues are still in-flight. That left a
cap-enforcement bug undetected: ``_compute_sets`` computed ``in_flight`` from
the approved queue, but dispatched issues leave the approved queue
(approved -> pm-review), so on the next wave in_flight was empty and
max_parallel was un-enforced.

This test exercises the real cross-process re-invocation path: it spawns
``parallel_queue.py start`` as a subprocess twice against the same temp
harness and asserts the cap survives the process boundary.

AC-CAPFIX-001/002/003.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import state  # noqa: E402


def _make_harness():
    tmp = tempfile.mkdtemp(prefix="laplace-parallel-xproc-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _seed_approved(tmp, issue_id):
    """Seed a draft issue and approve it (draft -> approved)."""
    tasks = state._load_tasks(tmp)
    tasks[issue_id] = {"status": "draft", "updated_at": __import__("time").time()}
    state._save_tasks(tasks, target=tmp)
    q = state._load_queue(tmp)
    if issue_id not in q["draft"]:
        q["draft"].append(issue_id)
    state._save_queue(q, target=tmp)
    assert state.cmd_approve(
        __import__("argparse").Namespace(
            issue_id=issue_id, user="tester", target=tmp
        )
    ) == 0


def _status_of(tmp, issue_id):
    return state._load_tasks(tmp).get(issue_id, {}).get("status")


def _run_parallel_subprocess(tmp):
    """Invoke ``parallel_queue.py start --target <tmp>`` as a real subprocess.

    Returns the CompletedProcess. This is the cross-process re-invocation
    path the bug hides in: each call is a fresh Python process that re-reads
    tasks.json / queue.json from disk.
    """
    return subprocess.run(
        [sys.executable,
         os.path.join(SCRIPTS, "parallel_queue.py"),
         "start", "--target", tmp],
        capture_output=True, text=True, timeout=120, cwd=PLUGIN_ROOT,
    )


def _advance_subprocess(tmp, issue_id, from_state, to_state, summary="ok"):
    """Invoke ``runner.py advance`` as a subprocess."""
    return subprocess.run(
        [sys.executable,
         os.path.join(SCRIPTS, "runner.py"),
         "advance", "--target", tmp,
         issue_id, from_state, to_state, "--summary", summary],
        capture_output=True, text=True, timeout=120, cwd=PLUGIN_ROOT,
    )


def _evidence_subprocess(tmp, run_id, kind="test", text="pytest: ok"):
    return subprocess.run(
        [sys.executable,
         os.path.join(SCRIPTS, "runner.py"),
         "evidence", "--target", tmp, run_id, kind, text],
        capture_output=True, text=True, timeout=120, cwd=PLUGIN_ROOT,
    )


def _drive_to_review_passed(tmp, issue_id):
    """Drive an issue pm-review -> review-passed via runner subprocesses."""
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "in-progress"),
                     ("in-progress", "review")):
        r = _advance_subprocess(tmp, issue_id, src, dst)
        assert r.returncode == 0, (
            f"advance {src}->{dst} for {issue_id} failed (rc={r.returncode}):\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
    run_id = state._load_tasks(tmp).get(issue_id, {}).get("run_id")
    assert run_id, f"no run_id for {issue_id}"
    r = _evidence_subprocess(tmp, run_id)
    assert r.returncode == 0, (
        f"evidence for {issue_id} failed (rc={r.returncode}):\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
    r = _advance_subprocess(tmp, issue_id, "review", "review-passed")
    assert r.returncode == 0, (
        f"advance review->review-passed for {issue_id} failed "
        f"(rc={r.returncode}):\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")


def test_cross_process_cap_survives_wave_re_invocation():
    """AC-CAPFIX-001/002/003: max_parallel enforced across subprocess waves.

    4 independent approved issues, max_parallel=2.

    Wave 1 (subprocess): dispatches exactly 2 -> pm-review. 2 remain approved.
    Wave 2 (second subprocess): the 2 in-flight have left the approved queue;
      the cap must still see them as in-flight (via tasks.json statuses) and
      dispatch 0 -> outcome wave-dispatched:waiting, in_flight=2.
    Drive one to review-passed (terminal). Wave 3 dispatches exactly 1
      (slots = 2 - 1 in-flight = 1).

    Pre-fix this fails at wave 2: in_flight=0 (computed from approved queue,
    which no longer contains the dispatched issues), so the scheduler
    dispatches 2 more, violating the cap.
    """
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        assert cfg["max_parallel"] == 2
        for n in ("C1", "C2", "C3", "C4"):
            _seed_approved(tmp, f"ISSUE-{n}")

        # --- Wave 1 (subprocess) -----------------------------------------
        r1 = _run_parallel_subprocess(tmp)
        assert r1.returncode == 0, (
            f"wave1 subprocess failed (rc={r1.returncode}):\n"
            f"STDOUT:\n{r1.stdout}\nSTDERR:\n{r1.stderr}")
        started_w1 = [n for n in ("C1", "C2", "C3", "C4")
                      if _status_of(tmp, f"ISSUE-{n}") == "pm-review"]
        assert len(started_w1) == 2, (
            f"wave1: exactly 2 issues should be pm-review, got "
            f"{len(started_w1)}: {started_w1}")

        # The 2 dispatched have LEFT the approved queue (approved -> pm-review).
        q_after_w1 = state._load_queue(tmp)
        assert len(q_after_w1["approved"]) == 2, (
            f"wave1: 2 issues should remain approved, got "
            f"{len(q_after_w1['approved'])}")

        # --- Wave 2 (second subprocess) ----------------------------------
        # This is the regression: a fresh process re-reads state. The cap
        # must be enforced using tasks.json statuses, NOT approved-queue
        # membership (which no longer contains the in-flight issues).
        r2 = _run_parallel_subprocess(tmp)
        assert r2.returncode == 0, (
            f"wave2 subprocess failed (rc={r2.returncode}):\n"
            f"STDOUT:\n{r2.stdout}\nSTDERR:\n{r2.stderr}")
        started_w2 = [n for n in ("C1", "C2", "C3", "C4")
                      if _status_of(tmp, f"ISSUE-{n}") == "pm-review"]
        assert len(started_w2) == 2, (
            f"wave2: cap violated - should still be exactly 2 in-flight "
            f"(0 newly dispatched), got {len(started_w2)} pm-review: "
            f"{started_w2}. The in_flight set was computed from the approved "
            f"queue, which misses issues that already transitioned "
            f"approved -> pm-review (ISSUE-0010).")

        # Outcome: 0 started this wave, 2 in-flight, 2 ready (deferred by cap).
        # The cap held -> this is the regression assertion. The exact outcome
        # string is "wave-dispatched" (ready_after non-empty: the 2 deferred
        # approved issues remain ready); the load-bearing check is that
        # exactly 0 were dispatched this wave.
        assert "0 started" in r2.stdout, (
            f"wave2: cap should have allowed 0 new dispatches, got "
            f"STDOUT:\n{r2.stdout}")
        assert "2 in-flight" in r2.stdout, (
            f"wave2: in-flight count must be 2 (read from tasks.json), got "
            f"STDOUT:\n{r2.stdout}")

        # --- Drive one issue to terminal, wave 3 dispatches 1 ------------
        first_started = started_w1[0]
        _drive_to_review_passed(tmp, f"ISSUE-{first_started}")
        r3 = _run_parallel_subprocess(tmp)
        assert r3.returncode == 0, (
            f"wave3 subprocess failed (rc={r3.returncode}):\n"
            f"STDOUT:\n{r3.stdout}\nSTDERR:\n{r3.stderr}")
        # Exactly one new issue should now be pm-review (slots = 2 - 1).
        pm_review_after_w3 = [n for n in ("C1", "C2", "C3", "C4")
                              if _status_of(tmp, f"ISSUE-{n}") == "pm-review"]
        assert len(pm_review_after_w3) == 2, (
            f"wave3: after one terminal, exactly 2 should be pm-review "
            f"(1 still in-flight + 1 newly dispatched), got "
            f"{len(pm_review_after_w3)}: {pm_review_after_w3}")
    finally:
        _teardown(tmp)
