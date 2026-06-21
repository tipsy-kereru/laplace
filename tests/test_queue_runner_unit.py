"""Unit tests for scripts/queue_runner.py (ISSUE-0003).

Covers the decision matrix, parent run-log shape (AC-QR-009), the cap
(AC-QR-008), held-lock handling (AC-QR-010), dependency gate (AC-QR-DEPS),
the noop path (AC-QR-NOOP), and the merge-policy stub.

Each test builds a fresh temp harness, seeds approved issues, and exercises
`_run_queue` with an `issue_driver` callback that simulates the skill/agent
intra-issue phase loop using runner primitives (compose, not re-implement).
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


def _make_harness():
    """Create a temp harness dir with config.yml + empty state."""
    tmp = tempfile.mkdtemp(prefix="laplace-queue-test-")
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


def _drive_to_blocked(issue_id, target):
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "blocked")):
        assert runner.cmd_advance(argparse.Namespace(
            issue_id=issue_id, from_state=src, to_state=dst,
            summary="", target=target)) == 0


def _policy_advance(issue_id, target):  # noqa: ARG001
    return ("advance", "")


def _log(tmp, run_id):
    return state._read_json(
        os.path.join(state._runs_dir(tmp), f"{run_id}.json"), default=None)


# ---------------------------------------------------------------------------
# Merge policy (ISSUE-0004: wait-for-human-merge)
# ---------------------------------------------------------------------------

def test_handle_merge_policy_empty_issue_returns_halt():
    assert queue_runner._handle_merge_policy("", None, "qr-0") \
        == ("halt", "merge-wait")


def test_handle_merge_policy_non_repo_returns_halt():
    # A plain temp dir with no .git is fail-safe -> halt (merge-wait under
    # the default wait-for-human-merge policy).
    tmp = tempfile.mkdtemp(prefix="laplace-mp-nonrepo-")
    try:
        assert queue_runner._handle_merge_policy("ISSUE-X", tmp, "qr-0") \
            == ("halt", "merge-wait")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # None target -> resolves to CWD which is not the issue branch's repo;
    # fail-safe -> halt without raising.
    assert queue_runner._handle_merge_policy("ISSUE-X", None, "qr-0") \
        == ("halt", "merge-wait")


def _git(args, cwd):
    r = subprocess.run(["git", "-C", cwd] + args,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"git {args} in {cwd} failed: {r.stderr}"
    return r


def _make_repo(base_branch):
    repo = tempfile.mkdtemp(prefix="laplace-mp-repo-")
    _git(["init", "-q", f"--initial-branch={base_branch}"], repo)
    _git(["config", "user.email", "unit@test"], repo)
    _git(["config", "user.name", "unit"], repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def test_issue_branch_is_merged_true_on_main():
    repo = _make_repo("main")
    try:
        _git(["checkout", "-q", "-b", "laplace/ISSUE-M"], repo)
        with open(os.path.join(repo, "f.txt"), "w") as f:
            f.write("change\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-q", "-m", "change"], repo)
        _git(["checkout", "-q", "main"], repo)
        _git(["merge", "-q", "--no-ff", "laplace/ISSUE-M", "-m", "merge"], repo)
        assert queue_runner._issue_branch_is_merged("ISSUE-M", repo) is True
        assert queue_runner._handle_merge_policy("ISSUE-M", repo, "qr-1") \
            == ("advance", "")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_issue_branch_is_merged_false_when_unmerged():
    repo = _make_repo("main")
    try:
        _git(["checkout", "-q", "-b", "laplace/ISSUE-U"], repo)
        with open(os.path.join(repo, "g.txt"), "w") as f:
            f.write("change\n")
        _git(["add", "g.txt"], repo)
        _git(["commit", "-q", "-m", "change"], repo)
        assert queue_runner._issue_branch_is_merged("ISSUE-U", repo) is False
        assert queue_runner._handle_merge_policy("ISSUE-U", repo, "qr-2") \
            == ("halt", "merge-wait")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_issue_branch_is_merged_falls_back_to_master():
    repo = _make_repo("master")
    try:
        _git(["checkout", "-q", "-b", "laplace/ISSUE-MS"], repo)
        with open(os.path.join(repo, "h.txt"), "w") as f:
            f.write("change\n")
        _git(["add", "h.txt"], repo)
        _git(["commit", "-q", "-m", "change"], repo)
        _git(["checkout", "-q", "master"], repo)
        _git(["merge", "-q", "--no-ff", "laplace/ISSUE-MS", "-m", "merge"], repo)
        assert queue_runner._issue_branch_is_merged("ISSUE-MS", repo) is True
        assert queue_runner._handle_merge_policy("ISSUE-MS", repo, "qr-3") \
            == ("advance", "")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_issue_branch_is_merged_missing_branch_returns_false():
    repo = _make_repo("main")
    try:
        # Branch never created -> not an ancestor.
        assert queue_runner._issue_branch_is_merged("ISSUE-NOPE", repo) \
            is False
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# Merge policy (ISSUE-0005: auto-merge-branch)
# ---------------------------------------------------------------------------

def test_integration_branch_name_is_protected_ref_guarded():
    """The integration branch name is hardcoded from the queue run id only.

    AC-QR-014 (structural): main/master can NEVER be a merge target because
    the target name is derived solely from the queue run id.
    """
    name = queue_runner._integration_branch_name("abc123")
    assert name == "laplace/queue-abc123"
    # Independent of any target/config/user input -- pure function of run id.
    assert queue_runner._integration_branch_name("def456") \
        == "laplace/queue-def456"


def test_auto_merge_issue_clean_merge_advances():
    """AC-QR-013: clean merge into integration branch -> advance."""
    repo = _make_repo("main")
    try:
        # Create an issue branch with a commit (forward of main).
        _git(["checkout", "-q", "-b", "laplace/ISSUE-C"], repo)
        with open(os.path.join(repo, "c.txt"), "w") as f:
            f.write("c\n")
        _git(["add", "c.txt"], repo)
        _git(["commit", "-q", "-m", "c"], repo)
        _git(["checkout", "-q", "main"], repo)
        qr = "am-clean-1"
        result = queue_runner._auto_merge_issue("ISSUE-C", qr, repo)
        assert result == ("advance", ""), result
        # Integration branch exists.
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify",
             queue_runner._integration_branch_name(qr)],
            capture_output=True)
        assert r.returncode == 0, "integration branch not created"
        # HEAD is on the integration branch.
        r_head = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True)
        assert r_head.stdout.strip() == queue_runner._integration_branch_name(qr)
        # main was NOT modified (no fast-forward of protected ref).
        r_main = subprocess.run(
            ["git", "-C", repo, "rev-parse", "main"],
            capture_output=True, text=True)
        r_int = subprocess.run(
            ["git", "-C", repo, "rev-parse",
             queue_runner._integration_branch_name(qr)],
            capture_output=True, text=True)
        # main's tip and integration's tip differ (main untouched).
        assert r_main.stdout != r_int.stdout
        # merged content is on integration branch.
        assert os.path.exists(os.path.join(repo, "c.txt"))
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_auto_merge_issue_conflict_halts_with_abort():
    """AC-QR-013: conflict -> halt + merge-conflict; merge aborted, no force."""
    repo = _make_repo("main")
    try:
        # Issue branch edits README.md to "issue".
        _git(["checkout", "-q", "-b", "laplace/ISSUE-CF"], repo)
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("issue\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-q", "-m", "cf"], repo)
        # Pre-create the integration branch with a conflicting edit.
        _git(["checkout", "-q", "main"], repo)
        qr = "am-conflict-1"
        int_branch = queue_runner._integration_branch_name(qr)
        _git(["branch", int_branch], repo)
        _git(["checkout", "-q", int_branch], repo)
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("integration\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-q", "-m", "int edit"], repo)
        _git(["checkout", "-q", "main"], repo)
        result = queue_runner._auto_merge_issue("ISSUE-CF", qr, repo)
        assert result == ("halt", "merge-conflict"), result
        # No stuck merge state (abort cleaned up).
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True)
        assert r.returncode == 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_auto_merge_issue_non_repo_halts():
    """Non-repo -> halt, merge-not-a-git-repo (no fallback)."""
    tmp = tempfile.mkdtemp(prefix="laplace-am-nr-")
    try:
        result = queue_runner._auto_merge_issue("ISSUE-NR", "am-nr", tmp)
        assert result == ("halt", "merge-not-a-git-repo"), result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_handle_merge_policy_dispatches_auto_merge():
    """_handle_merge_policy dispatches on merge_policy to auto-merge path."""
    repo = _make_repo("main")
    try:
        _git(["checkout", "-q", "-b", "laplace/ISSUE-D"], repo)
        with open(os.path.join(repo, "d.txt"), "w") as f:
            f.write("d\n")
        _git(["add", "d.txt"], repo)
        _git(["commit", "-q", "-m", "d"], repo)
        _git(["checkout", "-q", "main"], repo)
        qr = "am-dispatch-1"
        result = queue_runner._handle_merge_policy(
            "ISSUE-D", repo, qr, merge_policy="auto-merge-branch")
        assert result == ("advance", ""), result
        # Integration branch created.
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify",
             queue_runner._integration_branch_name(qr)],
            capture_output=True)
        assert r.returncode == 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_handle_merge_policy_auto_non_repo_no_fallback():
    """auto-merge-branch on non-repo halts merge-not-a-git-repo; does NOT
    fall back to wait-for-human-merge."""
    tmp = tempfile.mkdtemp(prefix="laplace-am-nr-dispatch-")
    try:
        result = queue_runner._handle_merge_policy(
            "ISSUE-NR", tmp, "am-nr-2", merge_policy="auto-merge-branch")
        assert result == ("halt", "merge-not-a-git-repo"), result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_queue_auto_merge_branch_advances_through_queue():
    """Integration: auto-merge-branch policy chains two review-passed issues
    via the integration branch and queue-exhausts (AC-QR-013)."""
    repo = _make_repo("main")
    try:
        assert state.cmd_init(target=repo) == 0
        cfg = {"max_queue_run": 5, "merge_policy": "auto-merge-branch"}
        _seed_approved(repo, "ISSUE-A1")
        _seed_approved(repo, "ISSUE-A2")

        def drive_with_commit(issue_id, target):
            marker = f"{issue_id}.txt"
            with open(os.path.join(target, marker), "w") as f:
                f.write(f"{issue_id}\n")
            _git(["add", marker], target)
            _git(["commit", "-q", "-m", f"work {issue_id}"], target)
            _drive_to_review_passed(issue_id, target)

        rid, rc = queue_runner._run_queue(None, repo, cfg, drive_with_commit)
        assert rc == 0
        log = _log(repo, rid)
        assert log["outcome"] == "queue-exhausted", log["outcome"]
        # Integration branch exists with both merged files.
        int_branch = queue_runner._integration_branch_name(rid)
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", int_branch],
            capture_output=True)
        assert r.returncode == 0
        assert os.path.exists(os.path.join(repo, "ISSUE-A1.txt"))
        assert os.path.exists(os.path.join(repo, "ISSUE-A2.txt"))
        # One queue_step recorded (A1 -> A2 advance).
        assert len(log["queue_steps"]) == 1
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_queue_halt_merge_wait_then_resume_advances():
    """AC-QR-011 + AC-QR-012 in a real git repo.

    First run: ISSUE-A reaches review-passed, its branch (with one commit)
    is not merged into main -> halt with merge-wait:ISSUE-A. ISSUE-B stays
    approved. After human merges laplace/ISSUE-A into main: second run skips
    ISSUE-A (terminal, no longer in approved) and advances to ISSUE-B,
    which (its branch unmerged) halts merge-wait:ISSUE-B.
    """
    repo = _make_repo("main")
    try:
        assert state.cmd_init(target=repo) == 0
        cfg = state.load_config(repo)
        _seed_approved(repo, "ISSUE-A")
        _seed_approved(repo, "ISSUE-B")

        def drive_with_commit(issue_id, target):
            # Add a distinct commit to the issue branch so it is NOT a
            # trivial ancestor of main (cmd_start only creates the branch).
            marker = f"{issue_id}.txt"
            with open(os.path.join(target, marker), "w") as f:
                f.write(f"{issue_id}\n")
            _git(["add", marker], target)
            _git(["commit", "-q", "-m", f"work {issue_id}"], target)
            _drive_to_review_passed(issue_id, target)

        # First run: ISSUE-A's branch has a commit, not merged -> halt.
        rid1, rc1 = queue_runner._run_queue(
            None, repo, cfg, drive_with_commit)
        assert rc1 == 0
        log1 = _log(repo, rid1)
        assert log1["outcome"] == "merge-wait:ISSUE-A", log1["outcome"]
        assert log1["queue_steps"] == []
        assert state._load_tasks(repo)["ISSUE-B"]["status"] == "approved"
        assert queue_runner._issue_branch_is_merged("ISSUE-A", repo) is False

        # Human merges ISSUE-A's branch into main.
        _git(["checkout", "-q", "main"], repo)
        _git(["merge", "-q", "--no-ff", "laplace/ISSUE-A", "-m", "merge A"], repo)
        assert queue_runner._issue_branch_is_merged("ISSUE-A", repo) is True

        # Second run (ISSUE-0001 fix): the merge-gate pre-pass sees ISSUE-A
        # review-passed + now-merged, records the A -> B queue_step, then
        # the main loop starts ISSUE-B whose branch is unmerged -> halt
        # merge-wait:ISSUE-B. (Pre-fix this run recorded no queue_step
        # because the gate never fired on resume -- the gate was the bug.)
        q_after = state._load_queue(repo)
        assert "ISSUE-A" not in q_after.get("approved", [])
        assert "ISSUE-B" in q_after.get("approved", [])
        rid2, rc2 = queue_runner._run_queue(
            None, repo, cfg, drive_with_commit)
        assert rc2 == 0
        log2 = _log(repo, rid2)
        assert log2["outcome"] == "merge-wait:ISSUE-B", log2["outcome"]
        steps2 = log2["queue_steps"]
        assert len(steps2) == 1, steps2
        assert steps2[0]["from_issue"] == "ISSUE-A"
        assert steps2[0]["to_issue"] == "ISSUE-B"
        assert steps2[0]["from_terminal_state"] == "review-passed"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC-QR-NOOP
# ---------------------------------------------------------------------------

def test_noop_empty_queue_exits_zero():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        rid, rc = queue_runner._run_queue(None, tmp, cfg, None)
        assert rc == 0
        log = _log(tmp, rid)
        assert log is not None
        assert log["outcome"] == "noop-empty-queue"
        assert log["queue_steps"] == []
    finally:
        _teardown(tmp)


def test_noop_start_issue_not_approved_exits_zero():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        # Seed one approved issue so the queue is non-empty; the named
        # start issue must then be checked against the approved list.
        _seed_approved(tmp, "ISSUE-A")
        rid, rc = queue_runner._run_queue("ISSUE-NOPE", tmp, cfg, None)
        assert rc == 0
        log = _log(tmp, rid)
        assert log["outcome"].startswith("noop-start-not-approved")
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-007: decision matrix
# ---------------------------------------------------------------------------

def test_review_passed_with_stub_halt_merges_wait():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        _seed_approved(tmp, "ISSUE-B")
        rid, rc = queue_runner._run_queue(
            None, tmp, cfg, _drive_to_review_passed)
        assert rc == 0
        log = _log(tmp, rid)
        assert log["outcome"] == "merge-wait:ISSUE-A"
        # ISSUE-B must remain approved (never started).
        assert state._load_tasks(tmp)["ISSUE-B"]["status"] == "approved"
        # No advance happened -> no queue_step.
        assert log["queue_steps"] == []
        # Parent log kind + issues trail.
        assert log["kind"] == "queue"
        assert log["issues"], "issues trail should record child run id"
    finally:
        _teardown(tmp)


def test_review_passed_with_advance_policy_continues_to_next():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        _seed_approved(tmp, "ISSUE-B")
        rid, rc = queue_runner._run_queue(
            None, tmp, cfg, _drive_to_review_passed,
            policy_override=_policy_advance)
        assert rc == 0
        log = _log(tmp, rid)
        # Both ran; queue exhausted.
        assert log["outcome"] == "queue-exhausted"
        assert state._load_tasks(tmp)["ISSUE-B"]["status"] == "review-passed"
    finally:
        _teardown(tmp)


def test_blocked_terminal_halts():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        _seed_approved(tmp, "ISSUE-B")
        rid, rc = queue_runner._run_queue(None, tmp, cfg, _drive_to_blocked)
        assert rc != 0
        log = _log(tmp, rid)
        assert log["outcome"] == "terminal:blocked"
        assert state._load_tasks(tmp)["ISSUE-B"]["status"] == "approved"
    finally:
        _teardown(tmp)


def test_non_terminal_final_state_halts():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")

        def _leave_in_review(issue_id, target):
            # Drive to ready-for-dev only -> non-terminal final state.
            for src, dst in (("pm-review", "ready-for-dev"),):
                runner.cmd_advance(argparse.Namespace(
                    issue_id=issue_id, from_state=src, to_state=dst,
                    summary="", target=target))

        rid, rc = queue_runner._run_queue(None, tmp, cfg, _leave_in_review)
        assert rc != 0
        log = _log(tmp, rid)
        assert log["outcome"].startswith("non-terminal:")
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-008: max_queue_run cap
# ---------------------------------------------------------------------------

def test_max_queue_run_cap_halts_after_one():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        _seed_approved(tmp, "ISSUE-B")
        cfg_cap = {"max_queue_run": 1, "merge_policy": cfg["merge_policy"]}
        rid, rc = queue_runner._run_queue(
            None, tmp, cfg_cap, _drive_to_review_passed,
            policy_override=_policy_advance)
        log = _log(tmp, rid)
        assert log["outcome"].startswith("max-queue-run-reached")
        # ISSUE-B never started.
        assert state._load_tasks(tmp)["ISSUE-B"]["status"] == "approved"
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-009: queue_steps shape
# ---------------------------------------------------------------------------

def test_queue_step_recorded_on_advance():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-P1")
        _seed_approved(tmp, "ISSUE-P2")
        rid, rc = queue_runner._run_queue(
            None, tmp, cfg, _drive_to_review_passed,
            policy_override=_policy_advance)
        log = _log(tmp, rid)
        steps = log["queue_steps"]
        assert len(steps) == 1
        s = steps[0]
        assert set(s.keys()) >= {"ts", "from_issue", "to_issue",
                                 "from_terminal_state", "evidence_run_id"}
        assert s["from_issue"] == "ISSUE-P1"
        assert s["to_issue"] == "ISSUE-P2"
        assert s["from_terminal_state"] == "review-passed"
        p1_run = state._load_tasks(tmp)["ISSUE-P1"].get("run_id")
        assert s["evidence_run_id"] == p1_run
        assert isinstance(s["ts"], float)
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-010: held lock
# ---------------------------------------------------------------------------

def test_held_lock_halts_and_leaves_lock_file():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        ok, _ = state.acquire_lock("ISSUE-A", target=tmp)
        assert ok
        lock_path = state._lock_path("ISSUE-A", tmp)
        rid, rc = queue_runner._run_queue(None, tmp, cfg, None)
        log = _log(tmp, rid)
        assert log["outcome"].startswith("held-lock")
        # Lock file must NOT have been deleted/released by queue_runner.
        assert os.path.exists(lock_path)
        assert rc != 0
    finally:
        # cleanup lock so teardown is clean
        state.release_lock("ISSUE-A", target=tmp) \
            if os.path.exists(state._lock_path("ISSUE-A", tmp)) else None
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-DEPS
# ---------------------------------------------------------------------------

def test_unmet_dependency_halts():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-DEP")
        # ISSUE-CHILD depends on ISSUE-DEP (still in approved, non-terminal).
        tasks = state._load_tasks(tmp)
        tasks["ISSUE-CHILD"] = {
            "status": "approved", "updated_at": time.time(),
            "depends_on": ["ISSUE-DEP"],
        }
        state._save_tasks(tasks, target=tmp)
        q = state._load_queue(tmp)
        q["approved"] = ["ISSUE-CHILD"]
        state._save_queue(q, target=tmp)
        rid, rc = queue_runner._run_queue("ISSUE-CHILD", tmp, cfg, None)
        log = _log(tmp, rid)
        assert log["outcome"].startswith("unmet-dependency:ISSUE-CHILD")
        assert rc != 0
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Parent run log shape (AC-QR-009)
# ---------------------------------------------------------------------------

def test_parent_log_shape_fields():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        rid, rc = queue_runner._run_queue(
            None, tmp, cfg, _drive_to_review_passed)
        log = _log(tmp, rid)
        assert log["kind"] == "queue"
        assert log["run_id"] == rid
        assert log["started_at"] is not None
        assert log["ended_at"] is not None
        assert log["outcome"] is not None
        assert log["max_queue_run"] == 5
        assert log["merge_policy"] == "wait-for-human-merge"
        assert log["start_issue"] is None
        assert isinstance(log["queue_steps"], list)
        assert isinstance(log["issues"], list)
    finally:
        _teardown(tmp)


def test_parent_log_records_start_issue():
    tmp = _make_harness()
    try:
        cfg = state.load_config(tmp)
        _seed_approved(tmp, "ISSUE-A")
        rid, rc = queue_runner._run_queue(
            "ISSUE-A", tmp, cfg, _drive_to_review_passed)
        log = _log(tmp, rid)
        assert log["start_issue"] == "ISSUE-A"
    finally:
        _teardown(tmp)
