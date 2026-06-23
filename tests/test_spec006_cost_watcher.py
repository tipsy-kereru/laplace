"""SPEC-006: cost watcher gate.

Covers the decision logic, config parsing/validation, and runner
integration (security-review -> cost-review redirect, cost-review
-> review-passed / human-approval-required).
"""
import argparse
import os
import sys
import tempfile
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import cost_review  # noqa: E402
import runner  # noqa: E402
import state  # noqa: E402


# --- decision logic ---

def test_decide_pass():
    v, sig, val = cost_review.decide(
        {"tokens": 1000, "runtime_minutes": 5, "files_changed": 3},
        state.DEFAULT_COST_THRESHOLDS)
    assert v == "pass"
    assert sig is None


def test_decide_block_runtime():
    v, sig, val = cost_review.decide(
        {"tokens": None, "runtime_minutes": 70, "files_changed": 3},
        state.DEFAULT_COST_THRESHOLDS)
    assert v == "block" and sig == "runtime_minutes" and val == 70


def test_decide_warn():
    v, sig, val = cost_review.decide(
        {"tokens": None, "runtime_minutes": 40, "files_changed": 11},
        state.DEFAULT_COST_THRESHOLDS)
    assert v == "warn" and sig is None


def test_decide_unknown_tokens_no_block():
    """AC-004: tokens=unknown cannot block on its own."""
    v, sig, val = cost_review.decide(
        {"tokens": None, "runtime_minutes": 5, "files_changed": 3},
        state.DEFAULT_COST_THRESHOLDS)
    assert v == "pass"


def test_decide_all_unknown_is_pass():
    """All three signals unknown -> watcher inert, returns pass."""
    v, sig, val = cost_review.decide(
        {"tokens": None, "runtime_minutes": None, "files_changed": None},
        state.DEFAULT_COST_THRESHOLDS)
    assert v == "pass"


# --- config parsing / validator ---

def test_config_default_disabled():
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    cfg = state.load_config(tmp)
    assert cfg["cost_watcher"]["enabled"] is False


def test_config_validator_rejects_runtime_block_over_cap():
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    path = os.path.join(tmp, ".harness", "config.yml")
    text = open(path).read().replace("block: 55", "block: 70")
    open(path, "w").write(text)
    try:
        state.load_config(tmp)
        assert False, "validator should have exited 2"
    except SystemExit as e:
        assert e.code == 2


def test_config_validator_rejects_files_block_over_cap():
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    path = os.path.join(tmp, ".harness", "config.yml")
    text = open(path).read().replace("block: 18", "block: 25")
    open(path, "w").write(text)
    try:
        state.load_config(tmp)
        assert False, "validator should have exited 2"
    except SystemExit as e:
        assert e.code == 2


def test_config_tokens_uncapped():
    """tokens has no hard cap; very large block is accepted."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    path = os.path.join(tmp, ".harness", "config.yml")
    text = open(path).read().replace("block: 2000000", "block: 99999999999")
    open(path, "w").write(text)
    cfg = state.load_config(tmp)
    assert cfg["cost_watcher"]["thresholds"]["tokens"]["block"] == 99999999999


# --- runner integration ---

def _enable_cost_watcher(target: str) -> None:
    path = os.path.join(target, ".harness", "config.yml")
    text = open(path).read().replace("enabled: false\ncost_watcher:",
                                     "enabled: false\ncost_watcher:") \
        if False else open(path).read()
    text = text.replace("cost_watcher:\n  enabled: false",
                        "cost_watcher:\n  enabled: true", 1)
    open(path, "w").write(text)


def _seed_run(target: str, issue_id: str, from_state: str,
              evidence=None, started_minutes_ago=0) -> str:
    run_id = f"run-{issue_id}"
    started = time.time() - started_minutes_ago * 60
    run = {"run_id": run_id, "issue_id": issue_id, "started_at": started,
           "ended_at": None, "outcome": None, "evidence": evidence or [],
           "transitions": []}
    state._atomic_write_json(
        os.path.join(state._runs_dir(target), f"{run_id}.json"), run)
    state._save_tasks(
        {issue_id: {"status": from_state, "updated_at": time.time(),
                    "run_id": run_id}}, target=target)
    return run_id


def _advance(target: str, issue_id: str, frm: str, to_: str) -> int:
    ns = argparse.Namespace(issue_id=issue_id, from_state=frm, to_state=to_,
                            summary="", target=target)
    so, se = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        return runner.cmd_advance(ns)
    finally:
        sys.stdout.close()
        sys.stdout = so
        sys.stderr.close()
        sys.stderr = se


def test_disabled_no_cost_review_state():
    """AC-001: with cost_watcher disabled, security-review -> review-passed
    works as before; cost-review is never entered."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    # force enabled false explicitly
    _seed_run(tmp, "ISSUE-0001", "security-review",
              evidence=[{"ts": time.time(), "kind": "test", "summary": "t"}])
    rc = _advance(tmp, "ISSUE-0001", "security-review", "review-passed")
    assert rc == 0
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "review-passed"


def test_enabled_redirects_to_cost_review():
    """AC: enabled routes security-review -> cost-review (not review-passed)."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    _enable_cost_watcher(tmp)
    _seed_run(tmp, "ISSUE-0001", "security-review",
              evidence=[{"ts": time.time(), "kind": "test", "summary": "t"}])
    rc = _advance(tmp, "ISSUE-0001", "security-review", "review-passed")
    assert rc == 0
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "cost-review"


def test_cost_review_blocks_on_runtime():
    """cost-review -> review-passed with runtime over block halts at
    human-approval-required."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    _enable_cost_watcher(tmp)
    # started 70 minutes ago -> runtime_minutes = 70 > block(55)
    _seed_run(tmp, "ISSUE-0001", "cost-review",
              evidence=[{"ts": time.time(), "kind": "test", "summary": "t"}],
              started_minutes_ago=70)
    rc = _advance(tmp, "ISSUE-0001", "cost-review", "review-passed")
    assert rc == 4
    rec = state._load_tasks(target=tmp)["ISSUE-0001"]
    assert rec["status"] == "human-approval-required"
    assert rec.get("block_reason", "").startswith("cost-block:runtime_minutes:")


def test_cost_review_passes_when_signals_low():
    """cost-review -> review-passed with low signals advances (AC-LP-008
    still required test evidence)."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    _enable_cost_watcher(tmp)
    _seed_run(tmp, "ISSUE-0001", "cost-review",
              evidence=[{"ts": time.time(), "kind": "test", "summary": "t"}],
              started_minutes_ago=2)
    rc = _advance(tmp, "ISSUE-0001", "cost-review", "review-passed")
    assert rc == 0
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "review-passed"


def test_cost_review_log_written():
    """Every decision writes a cost-reviews.jsonl entry."""
    tmp = tempfile.mkdtemp()
    state.cmd_init(target=tmp)
    _enable_cost_watcher(tmp)
    _seed_run(tmp, "ISSUE-0001", "cost-review",
              evidence=[{"ts": time.time(), "kind": "test", "summary": "t"}],
              started_minutes_ago=2)
    _advance(tmp, "ISSUE-0001", "cost-review", "review-passed")
    log_path = os.path.join(tmp, ".harness", "logs", "cost-reviews.jsonl")
    assert os.path.exists(log_path)
    lines = open(log_path).read().strip().splitlines()
    assert len(lines) >= 1
    import json
    entry = json.loads(lines[-1])
    assert entry["issue_id"] == "ISSUE-0001"
    assert entry["verdict"] in ("pass", "warn", "block")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} SPEC-006 tests passed")
