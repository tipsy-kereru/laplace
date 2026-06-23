"""Unit tests for orphan worktree reconcile (ISSUE-0013).

Covers AC-OW-001..004:
  - AC-OW-001: reconcile lists worktrees on disk with no live parent run.
  - AC-OW-002: --sweep --yes removes orphans; never removes a live one.
  - AC-OW-003: status shows orphan count when non-zero; byte-identical at 0.
  - AC-OW-004: a worktree whose run log is missing -> manual recovery, not
    auto-swept.

Each test builds a REAL git repo under tempfile so `git worktree list
--porcelain` returns the actual on-disk worktrees.
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
import parallel_queue  # noqa: E402


def _git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def _make_git_repo():
    """Build a fresh git repo with one commit on `main` and a harness."""
    repo = tempfile.mkdtemp(prefix="laplace-reconcile-repo-")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@t.test"], repo)
    _git(["config", "user.name", "Test"], repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    assert state.cmd_init(target=repo) == 0
    return repo


def _teardown(path):
    shutil.rmtree(path, ignore_errors=True)


def _write_child_log(target, run_id, *, issue_id, worktree_path,
                     finalized=False):
    log = {
        "run_id": run_id,
        "kind": "single",
        "issue_id": issue_id,
        "started_at": time.time(),
        "ended_at": time.time() if finalized else None,
        "outcome": "blocked" if finalized else None,
        "worktree_path": worktree_path,
    }
    state._atomic_write_json(
        os.path.join(state._runs_dir(target), f"{run_id}.json"), log)
    return log


def _add_worktree(repo, wt_path, branch_name):
    # Place under <repo>/.harness/worktrees/ so reconcile classifies it as a
    # Laplace-managed worktree (the main repo worktree is excluded).
    full = os.path.join(repo, ".harness", "worktrees", os.path.basename(wt_path))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    _git(["worktree", "add", "-b", branch_name, full, "main"], repo)
    return full


# ---------------------------------------------------------------------------
# AC-OW-001: lists worktrees on disk with no live parent run
# ---------------------------------------------------------------------------

def test_reconcile_lists_orphan_not_live():
    repo = _make_git_repo()
    try:
        live_wt = _add_worktree(repo, "wt-live", "laplace/live")
        orph_wt = _add_worktree(repo, "wt-orph", "laplace/orph")
        _write_child_log(repo, "c-live", issue_id="ISSUE-LIVE",
                         worktree_path=live_wt, finalized=False)
        _write_child_log(repo, "c-orph", issue_id="ISSUE-ORPH",
                         worktree_path=orph_wt, finalized=True)
        orphans, manual, live = parallel_queue._classify_worktrees(repo)
        live_norm = {os.path.normpath(p) for p in live}
        assert os.path.normpath(live_wt) in live_norm
        orph_paths = {os.path.normpath(e["path"]) for e in orphans}
        assert os.path.normpath(orph_wt) in orph_paths
        entry = next(e for e in orphans
                     if os.path.normpath(e["path"])
                     == os.path.normpath(orph_wt))
        assert entry["issue_id"] == "ISSUE-ORPH"
        assert manual == []
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-OW-002: --sweep --yes removes orphans; never removes live
# ---------------------------------------------------------------------------

def test_sweep_removes_orphans_keeps_live():
    repo = _make_git_repo()
    try:
        live_wt = _add_worktree(repo, "wt-live", "laplace/live")
        orph_wt = _add_worktree(repo, "wt-orph", "laplace/orph")
        _write_child_log(repo, "c-live", issue_id="ISSUE-LIVE",
                         worktree_path=live_wt, finalized=False)
        _write_child_log(repo, "c-orph", issue_id="ISSUE-ORPH",
                         worktree_path=orph_wt, finalized=True)
        ns = argparse.Namespace(sweep=True, yes=True, target=repo)
        rc = parallel_queue.cmd_reconcile_worktrees(ns)
        assert rc == 0
        # Orphan gone, live preserved.
        assert not os.path.isdir(orph_wt)
        assert os.path.isdir(live_wt)
    finally:
        _teardown(repo)


def test_sweep_without_yes_prompts_and_aborts_on_no():
    repo = _make_git_repo()
    try:
        orph_wt = _add_worktree(repo, "wt-orph", "laplace/orph")
        _write_child_log(repo, "c-orph", issue_id="ISSUE-ORPH",
                         worktree_path=orph_wt, finalized=True)
        ns = argparse.Namespace(sweep=True, yes=False, target=repo)
        # Empty stdin -> prompt reads EOF -> abort.
        saved = sys.stdin
        sys.stdin = open(os.devnull)
        try:
            rc = parallel_queue.cmd_reconcile_worktrees(ns)
        finally:
            sys.stdin.close()
            sys.stdin = saved
        assert rc == 1
        # Not swept.
        assert os.path.isdir(orph_wt)
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-OW-003: status orphan count line
# ---------------------------------------------------------------------------

BASELINE_EMPTY = (
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


def test_status_zero_orphans_byte_identical():
    """Empty git repo with no orphan worktrees -> baseline unchanged."""
    repo = _make_git_repo()
    try:
        assert state._format_status(repo) == BASELINE_EMPTY
    finally:
        _teardown(repo)


def test_status_nonzero_shows_orphan_line():
    repo = _make_git_repo()
    try:
        orph_wt = _add_worktree(repo, "wt-orph", "laplace/orph")
        _write_child_log(repo, "c-orph", issue_id="ISSUE-ORPH",
                         worktree_path=orph_wt, finalized=True)
        out = state._format_status(repo)
        assert "Orphan worktrees: 1" in out
        assert "/laplace:reconcile-worktrees" in out
        # The byte-identical baseline is NOT present anymore.
        assert out != BASELINE_EMPTY
    finally:
        _teardown(repo)


def test_status_live_worktree_does_not_count_as_orphan():
    repo = _make_git_repo()
    try:
        live_wt = _add_worktree(repo, "wt-live", "laplace/live")
        _write_child_log(repo, "c-live", issue_id="ISSUE-LIVE",
                         worktree_path=live_wt, finalized=False)
        # Only Laplace worktrees under .harness/worktrees/ count; live_wt is
        # live so orphan count is 0 (main repo worktree is excluded).
        orphan_count = state._count_orphan_worktrees(repo)
        assert orphan_count == 0, orphan_count
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# AC-OW-004: worktree whose run log is missing -> manual recovery, not swept
# ---------------------------------------------------------------------------

def test_manual_recovery_not_swept():
    repo = _make_git_repo()
    try:
        # A worktree on disk with NO run-log reference at all.
        manual_wt = _add_worktree(repo, "wt-manual", "laplace/manual")
        # No child log written.
        orphans, manual, live = parallel_queue._classify_worktrees(repo)
        manual_paths = {os.path.normpath(e["path"]) for e in manual}
        assert os.path.normpath(manual_wt) in manual_paths
        # Sweep must NOT remove manual entries.
        ns = argparse.Namespace(sweep=True, yes=True, target=repo)
        rc = parallel_queue.cmd_reconcile_worktrees(ns)
        assert rc == 0
        assert os.path.isdir(manual_wt), \
            "manual-recovery worktree must NOT be auto-swept (AC-OW-004)"
    finally:
        _teardown(repo)


def test_reconcile_report_includes_manual_and_orphan_sections():
    repo = _make_git_repo()
    try:
        orph_wt = _add_worktree(repo, "wt-orph", "laplace/orph")
        manual_wt = _add_worktree(repo, "wt-manual", "laplace/manual")
        _write_child_log(repo, "c-orph", issue_id="ISSUE-ORPH",
                         worktree_path=orph_wt, finalized=True)
        # manual_wt: no log -> manual recovery.
        orphans, manual, _live = parallel_queue._classify_worktrees(repo)
        lines = parallel_queue._reconcile_report(orphans, manual)
        text = "\n".join(lines)
        assert "Orphan worktrees (1)" in text
        assert "ISSUE-ORPH" in text
        assert "Manual recovery (1)" in text
    finally:
        _teardown(repo)


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_reconcile_no_orphans_reports_clean():
    repo = _make_git_repo()
    try:
        live_wt = _add_worktree(repo, "wt-live", "laplace/live")
        _write_child_log(repo, "c-live", issue_id="ISSUE-LIVE",
                         worktree_path=live_wt, finalized=False)
        # Only live worktree present -> no orphans, no manual entries.
        orphans, manual, _live = parallel_queue._classify_worktrees(repo)
        assert orphans == []
        assert manual == []
        lines = parallel_queue._reconcile_report(orphans, manual)
        text = "\n".join(lines)
        assert "No orphan worktrees." in text
        assert "Orphan worktrees" not in text
    finally:
        _teardown(repo)


def test_reconcile_sweep_with_no_orphans_noop_exit_zero():
    repo = _make_git_repo()
    try:
        ns = argparse.Namespace(sweep=True, yes=True, target=repo)
        rc = parallel_queue.cmd_reconcile_worktrees(ns)
        # No orphans -> sweep is a no-op, exit 0 even though manual entries
        # (the repo root) are present.
        assert rc == 0
    finally:
        _teardown(repo)


def test_policy_denial_on_git_list_fails_safe():
    """When policy denies `git worktree list --porcelain`, classification
    returns empty (fail-safe: nothing reconciled, no false-positive orphans)."""
    repo = _make_git_repo()
    try:
        orig = parallel_queue.policy.check_command

        def deny(cmd):
            if "worktree list" in cmd:
                return False, "denied: test"
            return orig(cmd)

        parallel_queue.policy.check_command = deny
        try:
            on_disk = parallel_queue._git_worktree_list(repo)
            assert on_disk == []
        finally:
            parallel_queue.policy.check_command = orig
    finally:
        _teardown(repo)
