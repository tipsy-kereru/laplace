"""Unit tests for runner.py worktree-per-issue isolation (ISSUE-0002).

One test per acceptance criterion AC-WT-001..AC-WT-010, against a real temp
git repo fixture. These complement the embedded `runner.py selftest` block
(which also exercises worktree lifecycle) by giving per-AC granularity and
pytest-grade failure reporting.

The fixture repo mirrors a production layout: `.harness/` is gitignored so
worktree creation under `.harness/worktrees/<issue-id>/` does not dirty the
main tree (AC-WT-002).
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


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _git(args, cwd):
    r = subprocess.run(["git", "-C", cwd] + args,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"git {args} in {cwd} failed: {r.stderr}"
    return r


def _make_repo():
    """A temp git repo with `main` branch + .harness/ gitignored.

    Mirrors a production repo layout: `.harness/` holds ephemeral runtime
    state (worktrees, runs, locks) and is gitignored so the main tree stays
    clean when worktrees are created under `.harness/worktrees/<id>/`.
    """
    repo = tempfile.mkdtemp(prefix="laplace-wt-repo-")
    _git(["init", "-q", "--initial-branch=main"], repo)
    _git(["config", "user.email", "wt@test"], repo)
    _git(["config", "user.name", "wt"], repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("base\n")
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write(".harness/\n")
    _git(["add", "README.md", ".gitignore"], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    assert state.cmd_init(target=repo) == 0
    return repo


def _teardown(repo):
    shutil.rmtree(repo, ignore_errors=True)


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


def _run_log(repo, run_id):
    return state._read_json(
        os.path.join(state._runs_dir(repo), f"{run_id}.json"), default=None)


def _drive_to_in_progress(issue_id, repo):
    """pm-review -> ready-for-dev -> in-progress (legal chain)."""
    for src, dst in (("pm-review", "ready-for-dev"),
                     ("ready-for-dev", "in-progress")):
        assert runner.cmd_advance(argparse.Namespace(
            issue_id=issue_id, from_state=src, to_state=dst,
            summary="", target=repo)) == 0


# ---------------------------------------------------------------------------
# AC-WT-001: start creates a worktree at .harness/worktrees/<id>/ branched
# from main; the main tree is NOT switched to the issue branch.
# ---------------------------------------------------------------------------

def test_ac_wt_001_start_creates_worktree_branched_from_main():
    repo = _make_repo()
    try:
        base_sha = _git(["rev-parse", "main"], repo).stdout.strip()
        _seed_approved(repo, "ISSUE-WT-1")

        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-1", target=repo)) == 0

        run_id = state._load_tasks(repo)["ISSUE-WT-1"]["run_id"]
        log = _run_log(repo, run_id)
        assert log is not None
        wt = log["worktree_path"]
        assert wt and os.path.isdir(wt), \
            f"worktree dir not created at {wt}"
        # The worktree path is under .harness/worktrees/<safe-id>/.
        assert ".harness/worktrees/ISSUE-WT-1" in wt.replace("\\", "/")
        # The worktree HEAD is on laplace/ISSUE-WT-1, branched from main.
        wt_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
        assert wt_branch == "laplace/ISSUE-WT-1", \
            f"worktree branch is {wt_branch!r}, expected laplace/ISSUE-WT-1"
        # Main tree is NOT switched to the issue branch.
        main_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
        assert main_branch == "main", \
            f"main tree switched to {main_branch!r}; AC-WT-001 violation"
        # The issue branch starts at main's HEAD (base).
        branch_sha = _git(["rev-parse", "laplace/ISSUE-WT-1"], repo).stdout.strip()
        assert branch_sha == base_sha, \
            f"laplace/ISSUE-WT-1 ({branch_sha}) != main base ({base_sha})"
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-002: during the dev phase the main tree remains on `main` and carries
# no dev changes; issue changes exist only in the worktree.
# ---------------------------------------------------------------------------

def test_ac_wt_002_main_tree_unmodified_during_dev():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-2")
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-2", target=repo)) == 0
        run_id = state._load_tasks(repo)["ISSUE-WT-2"]["run_id"]
        wt = _run_log(repo, run_id)["worktree_path"]

        # Simulate the dev agent: write + commit INSIDE the worktree only.
        with open(os.path.join(wt, "dev.txt"), "w") as f:
            f.write("dev work\n")
        _git(["add", "dev.txt"], wt)
        _git(["commit", "-q", "-m", "dev (ISSUE-WT-2)"], wt)

        # Main tree stays on `main`, clean.
        main_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
        assert main_branch == "main"
        main_status = _git(["status", "--porcelain"], repo).stdout.strip()
        assert main_status == "", \
            f"main tree dirty after dev: {main_status!r}"
        # The dev file exists ONLY in the worktree, not in the main tree.
        assert os.path.exists(os.path.join(wt, "dev.txt"))
        assert not os.path.exists(os.path.join(repo, "dev.txt"))
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-003: end removes the worktree but leaves laplace/<id> intact.
# ---------------------------------------------------------------------------

def test_ac_wt_003_end_removes_worktree_preserves_branch():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-3")
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-3", target=repo)) == 0
        run_id = state._load_tasks(repo)["ISSUE-WT-3"]["run_id"]
        wt = _run_log(repo, run_id)["worktree_path"]
        assert os.path.isdir(wt)

        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=repo,
            force_worktree_remove=False)) == 0

        assert not os.path.isdir(wt), \
            "worktree dir still present after end"
        # Branch is preserved.
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "laplace/ISSUE-WT-3"],
            capture_output=True)
        assert r.returncode == 0, "laplace/ISSUE-WT-3 branch was deleted by end"
        # Run log records teardown status.
        log = _run_log(repo, run_id)
        assert log["worktree_teardown"] == "removed"
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-004: stale branch (exists, behind main) -> start halts BRANCH_STALE,
# does NOT create a worktree, exit code 6, state unchanged, lock released.
# ---------------------------------------------------------------------------

def test_ac_wt_004_stale_branch_halts_start_without_worktree():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-4")
        # First start creates the branch from main.
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-4", target=repo)) == 0
        run_id1 = state._load_tasks(repo)["ISSUE-WT-4"]["run_id"]
        wt = _run_log(repo, run_id1)["worktree_path"]
        # End the run (removes the worktree, preserves the branch).
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id1, outcome="blocked", target=repo,
            force_worktree_remove=False)) == 0

        # Reset the issue back to approved so we can re-start.
        tasks = state._load_tasks(repo)
        tasks["ISSUE-WT-4"]["status"] = "approved"
        state._save_tasks(tasks, target=repo)

        # Advance main PAST the branch (branch becomes stale).
        with open(os.path.join(repo, "advance.txt"), "w") as f:
            f.write("advance\n")
        _git(["add", "advance.txt"], repo)
        _git(["commit", "-q", "-m", "advance main"], repo)

        # Re-start: stale -> BRANCH_STALE, exit 6, no worktree.
        # pytest captures stderr at fd level (dup2), so we cannot intercept
        # the message via sys.stderr swap; we rely on the rc + state +
        # worktree side-effect checks (the message text is covered by the
        # runner selftest and the SKILL.md wording, not the contract).
        rc = runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-4", target=repo))
        assert rc == runner.EXIT_BRANCH_STALE, \
            f"stale start should exit {runner.EXIT_BRANCH_STALE}, got {rc}"
        # Worktree NOT created.
        assert not os.path.isdir(wt), \
            "stale start created a worktree (AC-WT-004 violation)"
        # State unchanged (still approved — NOT pm-review).
        assert state._load_tasks(repo)["ISSUE-WT-4"]["status"] == "approved"
        # Lock released.
        assert not os.path.exists(state._lock_path("ISSUE-WT-4", repo)), \
            "stale start left the lock held"
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-005: branch exists AND is current with main -> reuses it in a fresh
# worktree (idempotent).
# ---------------------------------------------------------------------------

def test_ac_wt_005_reuse_current_branch_in_fresh_worktree():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-5")
        # First start creates the branch from main.
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-5", target=repo)) == 0
        run_id1 = state._load_tasks(repo)["ISSUE-WT-5"]["run_id"]
        wt1 = _run_log(repo, run_id1)["worktree_path"]
        # End the run (removes the worktree, preserves the branch).
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id1, outcome="blocked", target=repo,
            force_worktree_remove=False)) == 0
        assert not os.path.isdir(wt1)
        # laplace/ISSUE-WT-5 still exists, at main's HEAD (current).
        branch_sha = _git(["rev-parse", "laplace/ISSUE-WT-5"], repo).stdout.strip()
        main_sha = _git(["rev-parse", "main"], repo).stdout.strip()
        assert branch_sha == main_sha

        # Reset to approved, re-start.
        tasks = state._load_tasks(repo)
        tasks["ISSUE-WT-5"]["status"] = "approved"
        state._save_tasks(tasks, target=repo)

        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-5", target=repo)) == 0
        run_id2 = state._load_tasks(repo)["ISSUE-WT-5"]["run_id"]
        log2 = _run_log(repo, run_id2)
        assert log2["branch"]["status"] == "reused", \
            f"second start should reuse, got {log2['branch']}"
        wt2 = log2["worktree_path"]
        assert wt2 and os.path.isdir(wt2), \
            "reuse did not rebuild the worktree"
        # Same path (idempotent location).
        assert wt2 == wt1
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-006: non-repo target -> BRANCH_SKIPPED:not-a-git-repo, no worktree op,
# run proceeds with state transitions only.
# ---------------------------------------------------------------------------

def test_ac_wt_006_non_repo_skips_branch_and_worktree():
    tmp = tempfile.mkdtemp(prefix="laplace-wt-nonrepo-")
    try:
        assert state.cmd_init(target=tmp) == 0
        _seed_approved(tmp, "ISSUE-WT-6")

        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-6", target=tmp)) == 0

        run_id = state._load_tasks(tmp)["ISSUE-WT-6"]["run_id"]
        log = _run_log(tmp, run_id)
        assert log["branch"]["status"] == "skipped"
        assert "not-a-git-repo" in log["branch"]["reason"]
        # worktree_path is None (BRANCH_SKIPPED).
        assert log["worktree_path"] is None
        # No worktree dir was created.
        assert not os.path.exists(
            os.path.join(tmp, ".harness", "worktrees"))
        # State transition still happened (fail-safe).
        assert state._load_tasks(tmp)["ISSUE-WT-6"]["status"] == "pm-review"
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-WT-007: every git op routed through policy.check_command first.
# (Indirect: we verify _setup_branch returns skipped with `policy-denied:`
# when policy denies a git command. We simulate denial by monkeypatching
# policy.check_command for the duration of the call.)
# ---------------------------------------------------------------------------

def test_ac_wt_007_git_ops_routed_through_policy_check():
    import policy
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-7")
        # Snapshot the real checker; replace with one that denies all git.
        real = policy.check_command
        denied_calls = []

        def denying(cmd):
            denied_calls.append(cmd)
            if isinstance(cmd, str) and cmd.strip().startswith("git"):
                return False, "denied: test"
            return real(cmd)

        policy.check_command = denying
        try:
            binfo = runner._setup_branch("ISSUE-WT-7", repo)
        finally:
            policy.check_command = real
        # Every git op was routed through check_command.
        assert denied_calls, "no git command was policy-checked"
        assert all(isinstance(c, str) for c in denied_calls)
        assert any(c.startswith("git") for c in denied_calls)
        # On denial, _setup_branch returns skipped (fail-safe), not crash.
        # The reason may be `policy-denied` (a later git op was denied) or
        # `no-main-base` (the base-resolution probe was denied first); both
        # prove the policy gate fired and the branch was NOT created.
        assert binfo.status == "skipped", \
            f"denial should surface as skipped, got {binfo.status}/{binfo.reason}"
        # No worktree was created and no branch exists.
        assert binfo.worktree_path is None
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "laplace/ISSUE-WT-7"],
            capture_output=True)
        assert r.returncode != 0, \
            "laplace/ISSUE-WT-7 branch created despite policy denial"
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-008: dirty worktree at end -> WORKTREE_DIRTY halt (exit 7) unless
# --force-worktree-remove; do NOT silently discard dev work.
# ---------------------------------------------------------------------------

def test_ac_wt_008_dirty_worktree_halts_end_unless_forced():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-8")
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-8", target=repo)) == 0
        run_id = state._load_tasks(repo)["ISSUE-WT-8"]["run_id"]
        wt = _run_log(repo, run_id)["worktree_path"]

        # Leave an uncommitted change in the worktree.
        with open(os.path.join(wt, "uncommitted.txt"), "w") as f:
            f.write("dirty\n")

        # Dirty end without --force -> WORKTREE_DIRTY halt (exit 7). The
        # message text is exercised by the runner selftest; here we assert
        # the load-bearing contract: exit code + worktree preserved + run
        # NOT finalized + lock still held.
        rc = runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=repo,
            force_worktree_remove=False))

        assert rc == runner.EXIT_WORKTREE_DIRTY, \
            f"dirty end should exit {runner.EXIT_WORKTREE_DIRTY}, got {rc}"
        # Worktree still present (dev work preserved).
        assert os.path.isdir(wt), \
            "dirty worktree was removed without --force"
        # The uncommitted file is still there.
        assert os.path.exists(os.path.join(wt, "uncommitted.txt"))
        # Run NOT finalized (outcome still None, lock still held).
        log = _run_log(repo, run_id)
        assert log["ended_at"] is None, \
            "dirty-halt run was finalized (ended_at set)"
        assert os.path.exists(state._lock_path("ISSUE-WT-8", repo)), \
            "dirty-halt released the lock"

        # Force-removing the dirty worktree is allowed.
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=repo,
            force_worktree_remove=True)) == 0
        assert not os.path.isdir(wt), \
            "forced teardown did not remove the worktree"
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-009: run log records worktree_path (top-level + branch) and
# worktree_teardown.
# ---------------------------------------------------------------------------

def test_ac_wt_009_run_log_records_worktree_path_and_teardown():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-9")
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-9", target=repo)) == 0
        run_id = state._load_tasks(repo)["ISSUE-WT-9"]["run_id"]
        log = _run_log(repo, run_id)
        # worktree_path hoisted to top level (AC-WT-009).
        assert "worktree_path" in log
        assert log["worktree_path"] is not None
        # Mirrored inside the branch dict.
        assert log["branch"]["worktree_path"] == log["worktree_path"]
        # worktree_teardown absent before end.
        assert "worktree_teardown" not in log

        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=repo,
            force_worktree_remove=False)) == 0
        log2 = _run_log(repo, run_id)
        assert log2["worktree_teardown"] == "removed"
        # worktree_path preserved post-end.
        assert log2["worktree_path"] == log["worktree_path"]
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-WT-010: characterization — single-issue run semantics (state transitions,
# evidence, gates) unchanged; only the physical working location moves.
# ---------------------------------------------------------------------------

def test_ac_wt_010_single_issue_run_semantics_unchanged():
    repo = _make_repo()
    try:
        _seed_approved(repo, "ISSUE-WT-10")

        # Full single-issue lifecycle still works with worktree isolation.
        assert runner.cmd_start(argparse.Namespace(
            issue_id="ISSUE-WT-10", target=repo)) == 0
        run_id = state._load_tasks(repo)["ISSUE-WT-10"]["run_id"]

        # Legal transition chain pm-review -> ... -> review.
        for src, dst in (("pm-review", "ready-for-dev"),
                         ("ready-for-dev", "in-progress"),
                         ("in-progress", "review")):
            assert runner.cmd_advance(argparse.Namespace(
                issue_id="ISSUE-WT-10", from_state=src, to_state=dst,
                summary="", target=repo)) == 0

        # AC-LP-008 gate: review -> review-passed requires test evidence.
        rc_no_ev = runner.cmd_advance(argparse.Namespace(
            issue_id="ISSUE-WT-10", from_state="review",
            to_state="review-passed", summary="", target=repo))
        assert rc_no_ev == 4, \
            f"review-passed without evidence should exit 4, got {rc_no_ev}"

        # Capture test evidence, then the gate opens.
        assert runner.cmd_evidence(argparse.Namespace(
            run_id=run_id, kind="test", path_or_text="pytest: ok",
            target=repo)) == 0
        assert runner.cmd_advance(argparse.Namespace(
            issue_id="ISSUE-WT-10", from_state="review",
            to_state="review-passed", summary="ok", target=repo)) == 0

        assert state._load_tasks(repo)["ISSUE-WT-10"]["status"] \
            == "review-passed"

        # End: worktree removed, run finalized, lock released.
        lock_path = state._lock_path("ISSUE-WT-10", repo)
        assert os.path.exists(lock_path), "lock missing during run"
        assert runner.cmd_end(argparse.Namespace(
            run_id=run_id, outcome="completed", target=repo,
            force_worktree_remove=False)) == 0
        assert not os.path.exists(lock_path), \
            "cmd_end did not release the lock"

        # Run log shape: transitions recorded, evidence recorded.
        log = _run_log(repo, run_id)
        assert log["outcome"] == "completed"
        assert any(e.get("kind") == "test" for e in log["evidence"])
        transitions = [(t["from"], t["to"]) for t in log["transitions"]]
        for expected in (("pm-review", "ready-for-dev"),
                         ("ready-for-dev", "in-progress"),
                         ("in-progress", "review"),
                         ("review", "review-passed")):
            assert expected in transitions, \
                f"transition {expected} missing from {transitions}"
    finally:
        _teardown(repo)
