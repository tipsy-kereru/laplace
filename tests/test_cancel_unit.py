"""Unit tests for /laplace:cancel (ISSUE-0008).

Covers:
  - Single-issue cancel: active in-progress run -> runner.cmd_end called,
    issue state set to `cancelled`, lock released, run history appended.
  - Queue-scope cancel: resumable merge-wait parent log -> outcome
    rewritten to `cancelled:<id>`, no longer resumable.
  - No-arg detection priority: active single + resumable queue both
    present -> cancels the single issue (priority).
  - Nothing to cancel -> exit 0 with message.
  - AC-QR-022-cancel characterization: single-issue path unchanged.

Each test builds a fresh temp harness via state.cmd_init. Composes with
state + runner primitives — does not reimplement lock or run-log logic.
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

import cancel as cancel_mod  # noqa: E402
import runner  # noqa: E402
import state  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_harness():
    tmp = tempfile.mkdtemp(prefix="laplace-cancel-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _write_issue(tmp, issue_id, *, body="draft body"):
    """Write a minimal issue markdown file under .harness/issues/."""
    path = os.path.join(state._issues_dir(tmp), f"{issue_id}.md")
    text = (
        f"# {issue_id}\n\n"
        f"Summary: {body}\n\n"
        f"## Run History\n[]\n"
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _start_in_progress_run(tmp, issue_id, agent="dev"):
    """Approve an issue and start a run -> issue is in-progress with run_id."""
    state.cmd_approve(argparse.Namespace(
        issue_id=issue_id, user="tester", target=tmp))
    state.cmd_run_start(argparse.Namespace(
        issue_id=issue_id, agent=agent, attempt=1, target=tmp))
    tasks = state._load_tasks(tmp)
    return tasks[issue_id]["run_id"]


def _write_queue_log(tmp, run_id, *, outcome, started_at, ended_at=None,
                     merge_policy="wait-for-human-merge", issues=None,
                     kind="queue"):
    """Write a parent queue run log directly to .harness/state/runs/."""
    log = {
        "run_id": run_id,
        "kind": kind,
        "issue_id": None,
        "started_at": started_at,
        "ended_at": ended_at if ended_at is not None else started_at + 1.0,
        "outcome": outcome,
        "max_queue_run": 5,
        "merge_policy": merge_policy,
        "start_issue": None,
        "queue_steps": [],
        "issues": list(issues or []),
    }
    state._atomic_write_json(
        os.path.join(state._runs_dir(tmp), f"{run_id}.json"), log)
    return log


def _lock_path(tmp, issue_id):
    return state._lock_path(issue_id, target=tmp)


# ---------------------------------------------------------------------------
# Single-issue cancel
# ---------------------------------------------------------------------------

def test_single_issue_cancel_with_arg_ends_run_and_releases_lock():
    """AC-QR-022-cancel: explicit issue arg cancels the active run."""
    tmp = _make_harness()
    try:
        _write_issue(tmp, "ISSUE-A")
        run_id = _start_in_progress_run(tmp, "ISSUE-A")
        # Sanity: run is in-progress, lock present.
        assert state._load_tasks(tmp)["ISSUE-A"]["status"] == "in-progress"
        assert os.path.exists(_lock_path(tmp, "ISSUE-A"))

        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id="ISSUE-A", target=tmp))
        assert rc == 0

        tasks = state._load_tasks(tmp)
        assert tasks["ISSUE-A"]["status"] == "cancelled"
        # Lock released.
        assert not os.path.exists(_lock_path(tmp, "ISSUE-A"))
        # Run log outcome = cancelled.
        run = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{run_id}.json"))
        assert run["outcome"] == "cancelled"
        assert run["ended_at"] is not None
    finally:
        _teardown(tmp)


def test_single_issue_cancel_appends_run_history():
    """Single-issue cancel appends a `cancel: ...` line to Run History."""
    tmp = _make_harness()
    try:
        _write_issue(tmp, "ISSUE-H")
        run_id = _start_in_progress_run(tmp, "ISSUE-H")
        cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id="ISSUE-H", target=tmp))
        path = os.path.join(state._issues_dir(tmp), "ISSUE-H.md")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        assert f"cancel: {run_id} -> cancelled" in text
    finally:
        _teardown(tmp)


def test_single_issue_cancel_no_arg_uses_active_run():
    """No arg + active in-progress run -> single-issue path on it."""
    tmp = _make_harness()
    try:
        _write_issue(tmp, "ISSUE-NOARG")
        run_id = _start_in_progress_run(tmp, "ISSUE-NOARG")
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
        tasks = state._load_tasks(tmp)
        assert tasks["ISSUE-NOARG"]["status"] == "cancelled"
        assert not os.path.exists(_lock_path(tmp, "ISSUE-NOARG"))
        run = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{run_id}.json"))
        assert run["outcome"] == "cancelled"
    finally:
        _teardown(tmp)


def test_single_issue_cancel_arg_with_no_active_run_exits_nonzero():
    """Issue arg + no active run -> exit 1, no state change."""
    tmp = _make_harness()
    try:
        # Register issue in tasks.json (status=approved, no run).
        _write_issue(tmp, "ISSUE-IDLE")
        tasks = state._load_tasks(tmp)
        tasks["ISSUE-IDLE"] = {"status": "approved", "updated_at": time.time()}
        state._save_tasks(tasks, target=tmp)
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id="ISSUE-IDLE", target=tmp))
        assert rc != 0
        # State untouched.
        assert state._load_tasks(tmp)["ISSUE-IDLE"]["status"] == "approved"
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Queue-scope cancel
# ---------------------------------------------------------------------------

def test_queue_cancel_rewrites_outcome_to_cancelled():
    """AC-QR-020-cancel: no arg + no active single + resumable merge-wait
    parent log -> outcome rewritten to cancelled:<id>."""
    tmp = _make_harness()
    try:
        ts = time.time()
        _write_queue_log(
            tmp, "q-merge-1", outcome="merge-wait:ISSUE-MW", started_at=ts,
            issues=["child-mw"])
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
        log = state._read_json(
            os.path.join(state._runs_dir(tmp), "q-merge-1.json"))
        assert log["outcome"] == "cancelled:ISSUE-MW"
        # ended_at bumped (was started_at + 1.0 originally).
        assert log["ended_at"] is not None
        assert log["ended_at"] >= ts
        # Preserved fields.
        assert log["issues"] == ["child-mw"]
        assert log["queue_steps"] == []
    finally:
        _teardown(tmp)


def test_queue_cancel_drops_from_resumable_set():
    """AC-QR-021-cancel: after queue cancel, _find_resumable_queue_run
    returns None and status has no Queue run: block."""
    tmp = _make_harness()
    try:
        ts = time.time()
        _write_queue_log(
            tmp, "q-res", outcome="merge-wait:ISSUE-Z", started_at=ts)
        # Before: resumable.
        assert state._find_resumable_queue_run(tmp) is not None
        assert "Queue run:" in state._format_status(tmp)

        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0

        # After: not resumable, no Queue run: block.
        assert state._find_resumable_queue_run(tmp) is None
        assert "Queue run:" not in state._format_status(tmp)
    finally:
        _teardown(tmp)


def test_queue_cancel_preserves_suffix_for_other_merge_reasons():
    """A merge-conflict log rewrites to cancelled:<id> preserving suffix."""
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-conf", outcome="merge-conflict:ISSUE-CF",
            started_at=time.time(), merge_policy="auto-merge-branch")
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
        log = state._read_json(
            os.path.join(state._runs_dir(tmp), "q-conf.json"))
        assert log["outcome"] == "cancelled:ISSUE-CF"
        assert state._find_resumable_queue_run(tmp) is None
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Detection priority (AC-QR-020-cancel-detect)
# ---------------------------------------------------------------------------

def test_no_arg_priority_single_wins_over_queue():
    """Both active single-issue run AND resumable queue log present with
    no arg -> cancels the single-issue run; queue stays resumable."""
    tmp = _make_harness()
    try:
        _write_issue(tmp, "ISSUE-SINGLE")
        single_run_id = _start_in_progress_run(tmp, "ISSUE-SINGLE")
        _write_queue_log(
            tmp, "q-priority", outcome="merge-wait:ISSUE-QUEUED",
            started_at=time.time())

        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0

        # Single issue cancelled.
        tasks = state._load_tasks(tmp)
        assert tasks["ISSUE-SINGLE"]["status"] == "cancelled"
        single_log = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{single_run_id}.json"))
        assert single_log["outcome"] == "cancelled"
        # Queue log untouched (still merge-wait, still resumable).
        q_log = state._read_json(
            os.path.join(state._runs_dir(tmp), "q-priority.json"))
        assert q_log["outcome"] == "merge-wait:ISSUE-QUEUED"
        assert state._find_resumable_queue_run(tmp) is not None
    finally:
        _teardown(tmp)


def test_explicit_arg_always_single_even_with_queue_present():
    """Issue arg forces single path even if a resumable queue exists."""
    tmp = _make_harness()
    try:
        _write_issue(tmp, "ISSUE-EXPLICIT")
        run_id = _start_in_progress_run(tmp, "ISSUE-EXPLICIT")
        _write_queue_log(
            tmp, "q-bg", outcome="merge-wait:ISSUE-OTHER",
            started_at=time.time())

        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id="ISSUE-EXPLICIT", target=tmp))
        assert rc == 0
        assert state._load_tasks(tmp)["ISSUE-EXPLICIT"]["status"] == "cancelled"
        # Queue log untouched.
        q_log = state._read_json(
            os.path.join(state._runs_dir(tmp), "q-bg.json"))
        assert q_log["outcome"] == "merge-wait:ISSUE-OTHER"
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Nothing to cancel
# ---------------------------------------------------------------------------

def test_nothing_to_cancel_exit_zero():
    """No active single run and no resumable queue -> exit 0, message."""
    tmp = _make_harness()
    try:
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
    finally:
        _teardown(tmp)


def test_nothing_to_cancel_when_only_non_resumable_queue_logs():
    """Non-merge queue logs do not count as resumable; no-op."""
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-exhausted", outcome="queue-exhausted",
            started_at=time.time())
        rc = cancel_mod.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp))
        assert rc == 0
        # Log untouched.
        log = state._read_json(
            os.path.join(state._runs_dir(tmp), "q-exhausted.json"))
        assert log["outcome"] == "queue-exhausted"
    finally:
        _teardown(tmp)
