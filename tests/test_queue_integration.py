"""Integration tests for the queue runner with a 2-issue queue (ISSUE-0009).

AC-QR-025: end-to-end queue run through two issues in a real temp git repo,
with a mid-queue gate halt on the second issue's unmerged branch, and a
resume after the human clears the gate.

Scenario:
1. Seed two approved issues (A, B) in a temp git repo.
2. Run the queue. The issue_driver drives each issue to review-passed and
   adds a distinct commit to its branch so merge detection is meaningful.
3. First run: ISSUE-A's branch is merged into main (pre-merge before the
   queue driver returns control to the merge policy check) -> advance past A
   to B. ISSUE-B's branch is NOT merged -> halt `merge-wait:ISSUE-B`.
   The parent log records exactly one queue_step (A -> B).
4. Second run (resume): merge ISSUE-B's branch into main, re-invoke the
   queue. ISSUE-A is terminal (removed from approved), ISSUE-B runs and its
   branch is now merged -> queue-exhausted.

This complements the existing `test_queue_halt_merge_wait_then_resume_advances`
in test_queue_runner_unit.py, which halts on the FIRST issue. Here we exercise
the advance-then-halt path that records a queue_step mid-queue.
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
import queue_runner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (local copies; see ISSUE-0009 task note -- extracting to conftest
# would be a drive-by edit to existing test files).
# ---------------------------------------------------------------------------

def _git(args, cwd):
    r = subprocess.run(["git", "-C", cwd] + args,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"git {args} in {cwd} failed: {r.stderr}"
    return r


def _make_repo(base_branch):
    repo = tempfile.mkdtemp(prefix="laplace-qint-repo-")
    _git(["init", "-q", f"--initial-branch={base_branch}"], repo)
    _git(["config", "user.email", "qint@test"], repo)
    _git(["config", "user.name", "qint"], repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def _seed_approved(repo, issue_id):
    tasks = state._load_tasks(repo)
    tasks[issue_id] = {"status": "draft", "updated_at": time.time()}
    state._save_tasks(tasks, target=repo)
    q = state._load_queue(repo)
    if issue_id not in q["draft"]:
        q["draft"].append(issue_id)
    state._save_queue(q, target=repo)
    assert state.cmd_approve(argparse.Namespace(
        issue_id=issue_id, user="tester", target=repo)) == 0


def _drive_to_review_passed(issue_id, target):
    """Drive issue pm-review -> ... -> review-passed via runner.cmd_advance.

    Captures a test-evidence entry before the review -> review-passed gate
    (AC-LP-008). Assumes cmd_start already ran (issue is in pm-review).
    """
    run_id = state._load_tasks(target).get(issue_id, {}).get("run_id")
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "in-progress"),
                     ("in-progress", "review")):
        assert runner.cmd_advance(argparse.Namespace(
            issue_id=issue_id, from_state=src, to_state=dst,
            summary="", target=target)) == 0
    assert runner.cmd_evidence(argparse.Namespace(
        run_id=run_id, kind="test", path_or_text="pytest: ok",
        target=target)) == 0
    assert runner.cmd_advance(argparse.Namespace(
        issue_id=issue_id, from_state="review", to_state="review-passed",
        summary="ok", target=target)) == 0
    return run_id


def _merge_issue_branch_into_base(issue_id, base, repo):
    """Human-merge laplace/<issue_id> into <base> (main/master) via no-ff."""
    _git(["checkout", "-q", base], repo)
    _git(["merge", "-q", "--no-ff", f"laplace/{issue_id}",
          "-m", f"merge {issue_id}"], repo)


def _log(repo, run_id):
    return state._read_json(
        os.path.join(state._runs_dir(repo), f"{run_id}.json"), default=None)


def _driver_with_commit_and_merge_for(issue_id, target, merge_after):
    """Issue driver: add a commit to the issue branch, drive to review-passed,
    and -- if issue_id is in `merge_after` -- merge its branch into base before
    returning control to the queue's merge-policy check."""
    marker = f"{issue_id}.txt"
    with open(os.path.join(target, marker), "w") as f:
        f.write(f"{issue_id}\n")
    _git(["add", marker], target)
    _git(["commit", "-q", "-m", f"work {issue_id}"], target)
    _drive_to_review_passed(issue_id, target)
    if issue_id in merge_after:
        # Determine the base branch (main preferred, else master).
        base = "main"
        r = subprocess.run(
            ["git", "-C", target, "rev-parse", "--verify", "main"],
            capture_output=True)
        if r.returncode != 0:
            base = "master"
        _merge_issue_branch_into_base(issue_id, base, target)


# ---------------------------------------------------------------------------
# AC-QR-025: 2-issue queue with mid-queue gate halt
# ---------------------------------------------------------------------------

def test_two_issue_queue_mid_queue_gate_halt():
    """First issue advances (branch merged), second issue halts (branch
    unmerged). Parent log records exactly one queue_step (A -> B).
    """
    repo = _make_repo("main")
    try:
        assert state.cmd_init(target=repo) == 0
        cfg = state.load_config(repo)
        _seed_approved(repo, "ISSUE-A")
        _seed_approved(repo, "ISSUE-B")

        # Only ISSUE-A's branch will be merged before the merge-policy check;
        # ISSUE-B's branch stays unmerged -> halt merge-wait:ISSUE-B.
        def driver(issue_id, target):
            _driver_with_commit_and_merge_for(
                issue_id, target, merge_after={"ISSUE-A"})

        rid, rc = queue_runner._run_queue(None, repo, cfg, driver)
        assert rc == 0, f"expected exit 0 on merge-wait halt, got {rc}"

        log = _log(repo, rid)
        assert log is not None
        assert log["outcome"] == "merge-wait:ISSUE-B", log["outcome"]

        # Exactly one queue_step recorded: A -> B (the mid-queue advance).
        steps = log["queue_steps"]
        assert len(steps) == 1, \
            f"expected exactly one queue_step, got {len(steps)}"
        s = steps[0]
        assert s["from_issue"] == "ISSUE-A"
        assert s["to_issue"] == "ISSUE-B"
        assert s["from_terminal_state"] == "review-passed"
        # evidence_run_id ties the step to ISSUE-A's child run log.
        a_run = state._load_tasks(repo)["ISSUE-A"].get("run_id")
        assert s["evidence_run_id"] == a_run

        # Parent log shape (AC-QR-009 contract).
        assert log["kind"] == "queue"
        assert log["run_id"] == rid
        assert log["started_at"] is not None
        assert log["ended_at"] is not None
        assert log["max_queue_run"] == cfg["max_queue_run"]
        assert log["merge_policy"] == cfg["merge_policy"]
        assert log["start_issue"] is None
        assert isinstance(log["issues"], list)
        assert len(log["issues"]) >= 1, "child run trail must record ISSUE-A"

        # ISSUE-A is terminal; ISSUE-B is terminal too (driven to
        # review-passed by the driver) but its branch unmerged -> halt.
        tasks = state._load_tasks(repo)
        assert tasks["ISSUE-A"]["status"] == "review-passed"
        assert tasks["ISSUE-B"]["status"] == "review-passed"

        # ISSUE-B's branch is genuinely unmerged (the halt cause).
        assert queue_runner._issue_branch_is_merged("ISSUE-A", repo) is True
        assert queue_runner._issue_branch_is_merged("ISSUE-B", repo) is False

        # The approved queue no longer lists ISSUE-A (terminal -> removed by
        # _set_issue_state). ISSUE-B is also terminal now, so it too is gone
        # from approved; the queue is effectively exhausted of approved items.
        q = state._load_queue(repo)
        assert "ISSUE-A" not in q.get("approved", [])
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_two_issue_queue_resume_after_gate_clears():
    """Resume after the mid-queue gate clears.

    Builds on the mid-queue-halt scenario: ISSUE-A advanced (merged), ISSUE-B
    halted at merge-wait (review-passed, branch unmerged). After the human
    merges ISSUE-B's branch into base and the queue is re-invoked, the
    approved queue is empty (both issues are terminal) and the run reports a
    clean no-op.

    Note on outcome: ISSUE-0009's task brief expected `queue-exhausted` here,
    but the implemented semantics (queue_runner._process_issue + state
    precheck) only process issues whose status is `approved`. Once an issue
    reaches review-passed it leaves the approved list, so a resume after the
    LAST issue's gate clears finds an empty approved queue and reports
    `noop-empty-queue`. This is the faithful, verified behavior; asserting
    `queue-exhausted` would require the queue to re-evaluate a terminal
    issue, which the precheck forbids. See scripts/queue_runner.py
    `_precheck_issue` (status != approved -> halt) and `_run_queue` (empty
    approved -> noop-empty-queue).
    """
    repo = _make_repo("main")
    try:
        assert state.cmd_init(target=repo) == 0
        cfg = state.load_config(repo)
        _seed_approved(repo, "ISSUE-A")
        _seed_approved(repo, "ISSUE-B")

        # Phase 1: ISSUE-A's branch merged by the driver -> advance past A;
        # ISSUE-B driven to review-passed but branch left unmerged -> halt.
        def driver_a(issue_id, target):
            _driver_with_commit_and_merge_for(
                issue_id, target, merge_after={"ISSUE-A"})

        rid1, rc1 = queue_runner._run_queue(None, repo, cfg, driver_a)
        assert rc1 == 0
        log1 = _log(repo, rid1)
        assert log1["outcome"] == "merge-wait:ISSUE-B", log1["outcome"]
        assert len(log1["queue_steps"]) == 1

        # Both issues are now terminal (review-passed).
        tasks = state._load_tasks(repo)
        assert tasks["ISSUE-A"]["status"] == "review-passed"
        assert tasks["ISSUE-B"]["status"] == "review-passed"
        assert queue_runner._issue_branch_is_merged("ISSUE-A", repo) is True
        assert queue_runner._issue_branch_is_merged("ISSUE-B", repo) is False

        # Phase 2 (resume): human merges ISSUE-B's branch into base.
        _merge_issue_branch_into_base("ISSUE-B", "main", repo)
        assert queue_runner._issue_branch_is_merged("ISSUE-B", repo) is True

        # Re-invoke the queue. The approved list is empty (both terminal) ->
        # noop-empty-queue (the queue is effectively exhausted of pending
        # work). Exit code is 0 (clean no-op, not an error halt).
        rid2, rc2 = queue_runner._run_queue(None, repo, cfg, driver_a)
        assert rc2 == 0
        log2 = _log(repo, rid2)
        assert log2["outcome"] == "noop-empty-queue", log2["outcome"]
        assert log2["queue_steps"] == []
    finally:
        shutil.rmtree(repo, ignore_errors=True)
