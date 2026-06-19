"""Unit tests for /laplace:status queue-run integration (ISSUE-0007).

Covers:
  - AC-QR-019 (characterization): empty harness / no queue logs -> status
    output byte-identical to pre-change (no "Queue run:" block).
  - AC-QR-018: a resumable merge-* queue log -> "Queue run:" block renders
    with all five fields (run id, current issue, step, merge policy,
    consecutive).
  - Filtering: non-resumable queue logs (queue-exhausted, terminal:blocked,
    noop-*) are NOT reported.
  - Selection: most-recent by started_at when multiple merge-* logs exist.

Each test builds a fresh temp harness via state.cmd_init and writes parent
queue run logs directly under .harness/state/runs/ using state._atomic_write_json
(compose, do not re-implement).
"""
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


# Pre-change baseline snapshot of `_format_status` output for an empty
# harness (no runs at all). Captured 2026-06-19; AC-QR-019 requires any
# harness with no resumable queue run to remain byte-identical to this.
# NOTE: `_format_status` returns the body WITHOUT a trailing newline; the
# `cmd_status` CLI wrapper adds the final newline via `print`.
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


def _make_harness():
    tmp = tempfile.mkdtemp(prefix="laplace-status-q-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


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


def _write_child_log(tmp, child_run_id, issue_id, started_at):
    log = {
        "run_id": child_run_id,
        "kind": "single",
        "issue_id": issue_id,
        "started_at": started_at,
        "ended_at": started_at + 0.5,
        "outcome": "review-passed",
        "agent": "dev",
        "attempt": 1,
        "evidence": [],
    }
    state._atomic_write_json(
        os.path.join(state._runs_dir(tmp), f"{child_run_id}.json"), log)
    return log


# ---------------------------------------------------------------------------
# AC-QR-019: characterization (no queue logs -> byte-identical)
# ---------------------------------------------------------------------------

def test_status_empty_harness_byte_identical_to_baseline():
    tmp = _make_harness()
    try:
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


def test_status_non_queue_run_logs_do_not_add_block():
    """A single-issue (kind != queue) run log must not trigger the block."""
    tmp = _make_harness()
    try:
        _write_child_log(tmp, "child-1", "ISSUE-X", time.time())
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# AC-QR-018: resumable merge-* queue log -> block with all 5 fields
# ---------------------------------------------------------------------------

def test_status_resumable_merge_wait_renders_full_block():
    tmp = _make_harness()
    try:
        ts = time.time()
        _write_queue_log(
            tmp, "q-merge-wait", outcome="merge-wait:ISSUE-A",
            started_at=ts, merge_policy="wait-for-human-merge")
        out = state._format_status(tmp)
        # Block separator + header.
        assert "\nQueue run:\n" in out
        # All five fields present with expected values.
        assert "  run id: q-merge-wait" in out
        assert "  current issue: ISSUE-A" in out
        assert "  step: 0" in out
        assert "  merge policy: wait-for-human-merge" in out
        assert "  consecutive: 0" in out
    finally:
        _teardown(tmp)


def test_status_resumable_merge_conflict_renders_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-conflict", outcome="merge-conflict:ISSUE-B",
            started_at=time.time(), merge_policy="auto-merge-branch")
        out = state._format_status(tmp)
        assert "  run id: q-conflict" in out
        assert "  current issue: ISSUE-B" in out
        assert "  merge policy: auto-merge-branch" in out
    finally:
        _teardown(tmp)


def test_status_resumable_merge_policy_denied_renders_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-denied", outcome="merge-policy-denied:ISSUE-C",
            started_at=time.time())
        out = state._format_status(tmp)
        assert "  run id: q-denied" in out
        assert "  current issue: ISSUE-C" in out
    finally:
        _teardown(tmp)


def test_status_resumable_merge_not_a_git_repo_renders_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-nogit", outcome="merge-not-a-git-repo:ISSUE-D",
            started_at=time.time())
        out = state._format_status(tmp)
        assert "  run id: q-nogit" in out
        assert "  current issue: ISSUE-D" in out
    finally:
        _teardown(tmp)


def test_status_block_step_consecutive_equals_issues_length():
    """step and consecutive both equal len(log['issues'])."""
    tmp = _make_harness()
    try:
        ts = time.time()
        # Two child runs recorded -> step=2, consecutive=2.
        _write_queue_log(
            tmp, "q-step2", outcome="merge-wait:ISSUE-A", started_at=ts,
            issues=["child-a", "child-b"])
        out = state._format_status(tmp)
        assert "  step: 2" in out
        assert "  consecutive: 2" in out
    finally:
        _teardown(tmp)


def test_status_current_issue_fallback_to_last_child_run():
    """When outcome has no ':' suffix, current issue falls back to the
    issue_id of the last entry in log['issues']."""
    tmp = _make_harness()
    try:
        ts = time.time()
        # outcome "merge-wait" (no ':') -- fallback path.
        _write_child_log(tmp, "child-fb", "ISSUE-FALLBACK", ts)
        _write_queue_log(
            tmp, "q-fb", outcome="merge-wait", started_at=ts,
            issues=["child-fb"])
        out = state._format_status(tmp)
        assert "  current issue: ISSUE-FALLBACK" in out
    finally:
        _teardown(tmp)


def test_status_current_issue_fallback_to_question_when_unknown():
    """No outcome suffix and no resolvable child run -> '?'."""
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-unknown", outcome="merge-wait", started_at=time.time(),
            issues=["does-not-exist"])
        out = state._format_status(tmp)
        assert "  current issue: ?" in out
    finally:
        _teardown(tmp)


def test_status_merge_policy_missing_falls_back_to_question():
    tmp = _make_harness()
    try:
        ts = time.time()
        # Hand-craft a log without merge_policy to exercise the '?' fallback.
        log = {
            "run_id": "q-nomergepol", "kind": "queue", "issue_id": None,
            "started_at": ts, "ended_at": ts + 1.0,
            "outcome": "merge-wait:ISSUE-E",
            "queue_steps": [], "issues": [],
        }
        state._atomic_write_json(
            os.path.join(state._runs_dir(tmp), "q-nomergepol.json"), log)
        out = state._format_status(tmp)
        assert "  merge policy: ?" in out
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Filtering: non-resumable queue outcomes are NOT reported
# ---------------------------------------------------------------------------

def test_status_non_resumable_queue_exhausted_no_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-exhausted", outcome="queue-exhausted",
            started_at=time.time())
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


def test_status_non_resumable_terminal_blocked_no_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-blocked", outcome="terminal:blocked",
            started_at=time.time())
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


def test_status_non_resumable_noop_empty_queue_no_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-noop", outcome="noop-empty-queue",
            started_at=time.time())
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


def test_status_non_resumable_noop_start_not_approved_no_block():
    tmp = _make_harness()
    try:
        _write_queue_log(
            tmp, "q-noop2", outcome="noop-start-not-approved:ISSUE-Z",
            started_at=time.time())
        assert state._format_status(tmp) == BASELINE_EMPTY
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# Selection: most-recent by started_at
# ---------------------------------------------------------------------------

def test_status_picks_most_recent_merge_log_by_started_at():
    tmp = _make_harness()
    try:
        base = time.time()
        # Older merge-wait log.
        _write_queue_log(
            tmp, "q-old", outcome="merge-wait:ISSUE-OLD",
            started_at=base, merge_policy="wait-for-human-merge")
        # Newer merge-wait log -- should win.
        _write_queue_log(
            tmp, "q-new", outcome="merge-wait:ISSUE-NEW",
            started_at=base + 10.0, merge_policy="auto-merge-branch")
        out = state._format_status(tmp)
        assert "  run id: q-new" in out
        assert "  current issue: ISSUE-NEW" in out
        assert "  merge policy: auto-merge-branch" in out
        # Older one's fields must NOT be in the block.
        assert "ISSUE-OLD" not in out
        assert "  run id: q-old" not in out
    finally:
        _teardown(tmp)


def test_status_picks_only_merge_log_when_others_present():
    """A merge-* log wins over a more recent non-resumable log because the
    non-resumable one is filtered out."""
    tmp = _make_harness()
    try:
        base = time.time()
        _write_queue_log(
            tmp, "q-resumable", outcome="merge-wait:ISSUE-R",
            started_at=base)
        # Newer but non-resumable.
        _write_queue_log(
            tmp, "q-exhausted-late", outcome="queue-exhausted",
            started_at=base + 100.0)
        out = state._format_status(tmp)
        assert "  run id: q-resumable" in out
        assert "  current issue: ISSUE-R" in out
    finally:
        _teardown(tmp)


# ---------------------------------------------------------------------------
# _find_resumable_queue_run direct unit coverage
# ---------------------------------------------------------------------------

def test_find_resumable_queue_run_returns_none_when_empty():
    tmp = _make_harness()
    try:
        assert state._find_resumable_queue_run(tmp) is None
    finally:
        _teardown(tmp)


def test_find_resumable_queue_run_returns_the_merge_log():
    tmp = _make_harness()
    try:
        ts = time.time()
        _write_queue_log(
            tmp, "q-only", outcome="merge-wait:ISSUE-A", started_at=ts)
        found = state._find_resumable_queue_run(tmp)
        assert found is not None
        assert found["run_id"] == "q-only"
        assert found["kind"] == "queue"
        assert found["outcome"] == "merge-wait:ISSUE-A"
    finally:
        _teardown(tmp)
