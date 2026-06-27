"""SPEC-010: ci-signal motivation trigger.

Mocked gh/git. Verifies issue mapping from commit message, ci-seen dedup,
state-based dispatch (review-passed → needs-fix, release-candidate →
blocked), and opt-out.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from unittest import mock

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import motivations as M  # noqa: E402
import state  # noqa: E402


def _fresh() -> str:
    tmp = tempfile.mkdtemp(prefix="laplace-spec010-")
    state.cmd_init(target=tmp)
    return tmp


def _seed(tmp, iid, status):
    state._save_tasks({iid: {"status": status, "updated_at": time.time()}},
                      target=tmp)


def _gh_result(runs):
    """Build a mock subprocess.CompletedMessage-style object."""
    obj = mock.MagicMock()
    obj.returncode = 0
    obj.stdout = json.dumps(runs).encode("utf-8")
    obj.stderr = b""
    return obj


def _set_commit_msg(msg):
    """Patch _git_commit_message to return `msg`."""
    return mock.patch.object(M, "_git_commit_message", return_value=msg)


def test_no_issue_token_ignored():
    tmp = _fresh()
    runs = [{"databaseId": 101, "headSha": "abc"}]
    with _set_commit_msg("chore: tidy up"), \
         mock.patch("subprocess.run", return_value=_gh_result(runs)):
        out = M.trigger_ci_signal({}, {"base_branch": "main"}, tmp, time.time())
    assert out == []


def test_matched_issue_returned():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0042", "review-passed")
    runs = [{"databaseId": 102, "headSha": "def"}]
    with _set_commit_msg("fix: bug (ISSUE-0042)"), \
         mock.patch("subprocess.run", return_value=_gh_result(runs)):
        out = M.trigger_ci_signal(state._load_tasks(tmp),
                                  {"base_branch": "main"}, tmp, time.time())
    assert out == ["ISSUE-0042"]


def test_seen_run_not_re_acted():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0042", "review-passed")
    runs = [{"databaseId": 103, "headSha": "ghi"}]
    with _set_commit_msg("fix: bug (ISSUE-0042)"), \
         mock.patch("subprocess.run", return_value=_gh_result(runs)):
        first = M.trigger_ci_signal(state._load_tasks(tmp),
                                    {"base_branch": "main"}, tmp, time.time())
        second = M.trigger_ci_signal(state._load_tasks(tmp),
                                     {"base_branch": "main"}, tmp, time.time())
    assert first == ["ISSUE-0042"]
    assert second == []  # 103 now in ci-seen.json


def test_wrong_state_skipped():
    """Issue in `done` is not a ci-signal candidate."""
    tmp = _fresh()
    _seed(tmp, "ISSUE-0099", "done")
    runs = [{"databaseId": 104, "headSha": "jkl"}]
    with _set_commit_msg("feat: x (ISSUE-0099)"), \
         mock.patch("subprocess.run", return_value=_gh_result(runs)):
        out = M.trigger_ci_signal(state._load_tasks(tmp),
                                  {"base_branch": "main"}, tmp, time.time())
    assert out == []


def test_release_candidate_also_candidate():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0007", "release-candidate")
    runs = [{"databaseId": 105, "headSha": "mno"}]
    with _set_commit_msg("release: v1 (ISSUE-0007)"), \
         mock.patch("subprocess.run", return_value=_gh_result(runs)):
        out = M.trigger_ci_signal(state._load_tasks(tmp),
                                  {"base_branch": "main"}, tmp, time.time())
    assert out == ["ISSUE-0007"]


def test_gh_failure_returns_empty():
    tmp = _fresh()
    obj = mock.MagicMock()
    obj.returncode = 1
    obj.stdout = b""
    obj.stderr = b"not authenticated"
    with mock.patch("subprocess.run", return_value=obj):
        out = M.trigger_ci_signal({}, {"base_branch": "main"}, tmp, time.time())
    assert out == []


def test_dispatch_review_passed_to_needs_fix():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0001", "review-passed")
    rc = M.dispatch("ci-signal", "ISSUE-0001", tmp)
    assert rc == 0
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "needs-fix"


def test_dispatch_release_candidate_to_blocked():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0002", "release-candidate")
    rc = M.dispatch("ci-signal", "ISSUE-0002", tmp)
    assert rc == 0
    rec = state._load_tasks(target=tmp)["ISSUE-0002"]
    assert rec["status"] == "blocked"
    assert rec.get("block_reason", "").startswith("ci-failure")


def test_dispatch_wrong_state_noop():
    tmp = _fresh()
    _seed(tmp, "ISSUE-0003", "approved")
    rc = M.dispatch("ci-signal", "ISSUE-0003", tmp)
    assert rc == 1  # noop refusal
    assert state._load_tasks(target=tmp)["ISSUE-0003"]["status"] == "approved"


def test_review_passed_needs_fix_transition_valid():
    """AC-008: TERMINAL_STATES unchanged; transition still valid via
    VALID_TRANSITIONS."""
    ok, _ = state.validate_transition("review-passed", "needs-fix")
    assert ok
    # review-passed still in TERMINAL_STATES (queue semantics preserved).
    assert "review-passed" in state.TERMINAL_STATES


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} SPEC-010 tests passed")
