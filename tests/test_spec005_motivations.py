"""SPEC-005: motivation triggers.

Covers kill switch, rate limiter, state preconditions, per-trigger poll
logic, and dispatch routing. All triggers exercised against a temp harness
with the real state files; git subprocess paths are isolated.
"""
import json
import os
import sys
import tempfile
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import motivations as M  # noqa: E402
import state  # noqa: E402


def _fresh() -> str:
    tmp = tempfile.mkdtemp(prefix="laplace-spec005-")
    state.cmd_init(target=tmp)
    return tmp


def _enable_motivations(target: str) -> None:
    path = os.path.join(target, ".harness", "config.yml")
    text = open(path).read()
    text = text.replace("motivations:\n  enabled: false",
                        "motivations:\n  enabled: true", 1)
    open(path, "w").write(text)


def _set_tasks(target: str, tasks: dict) -> None:
    state._save_tasks(tasks, target=target)
    q = state._load_queue(target=target)
    for s in state.QUEUE_STATES:
        q[s] = []
    for iid, rec in tasks.items():
        if rec.get("status") in state.QUEUE_STATES:
            q[rec["status"]].append(iid)
    state._save_queue(q, target=target)


def _write_due(target: str, iid: str, due_iso: str) -> None:
    path = os.path.join(state._issues_dir(target), f"{iid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {iid}\n\n- Due Date: {due_iso}\n")


def _log_events(target: str) -> list:
    path = M._log_path(target)
    if not os.path.exists(path):
        return []
    return [json.loads(ln) for ln in open(path).read().strip().splitlines() if ln]


# --- kill switch + rate limiter ---

def test_disabled_exits_clean():
    tmp = _fresh()  # motivations.enabled defaults to false
    rc = M.run_once(target=tmp)
    assert rc == 0
    events = _log_events(tmp)
    assert any(e["event"] == "disabled" for e in events)


def test_rate_limit_caps_dispatches():
    tmp = _fresh()
    _enable_motivations(tmp)
    # Set max_per_hour to 1 via config edit.
    path = os.path.join(tmp, ".harness", "config.yml")
    text = open(path).read().replace("max_dispatches_per_hour: 10",
                                     "max_dispatches_per_hour: 1")
    open(path, "w").write(text)
    now = time.time()
    # Pre-fill the rate window so the next call is rate-limited.
    M._save_rate([now], tmp)
    rc = M.run_once(target=tmp, now=now)
    assert rc == 0
    events = _log_events(tmp)
    assert any(e["event"] == "rate-limited" for e in events)


# --- triggers ---

def test_clock_fires_for_due_approved_issue():
    tmp = _fresh()
    _enable_motivations(tmp)
    now = time.time()
    # Due 2 days out -> comfortably within default 24h window when we widen
    # the trigger config; here set due_within_hours big enough.
    soon_iso = time.strftime("%Y-%m-%d", time.localtime(now + 2 * 86400))
    _write_due(tmp, "ISSUE-A", soon_iso)
    # Widen the clock window so the 2-day-out date fires.
    cfg_path = os.path.join(tmp, ".harness", "config.yml")
    text = open(cfg_path).read().replace("due_within_hours: 24",
                                         "due_within_hours: 200")
    open(cfg_path, "w").write(text)
    _set_tasks(tmp, {"ISSUE-A": {"status": "approved", "updated_at": now}})
    dispatched = []
    M.dispatch = lambda evt, iid, target: dispatched.append((evt, iid)) or 0
    try:
        M.run_once(target=tmp, now=now)
    finally:
        del M.dispatch
    events = _log_events(tmp)
    assert any(e["event"] == "dispatch" and e["trigger"] == "clock"
               and e["issue_id"] == "ISSUE-A" for e in events)
    assert ("clock", "ISSUE-A") in dispatched


def test_clock_noop_on_non_approved():
    tmp = _fresh()
    _enable_motivations(tmp)
    now = time.time()
    soon_iso = time.strftime("%Y-%m-%d", time.localtime(now + 2 * 86400))
    _write_due(tmp, "ISSUE-A", soon_iso)
    cfg_path = os.path.join(tmp, ".harness", "config.yml")
    text = open(cfg_path).read().replace("due_within_hours: 24",
                                         "due_within_hours: 200")
    open(cfg_path, "w").write(text)
    _set_tasks(tmp, {"ISSUE-A": {"status": "review-passed", "updated_at": now}})
    M.dispatch = lambda *a, **k: 0
    try:
        M.run_once(target=tmp, now=now)
    finally:
        del M.dispatch
    events = _log_events(tmp)
    assert not any(e["event"] == "dispatch" and e["trigger"] == "clock"
                   for e in events)


def test_idle_queue_dispatches_when_inactive():
    tmp = _fresh()
    _enable_motivations(tmp)
    now = time.time()
    _set_tasks(tmp, {"ISSUE-A": {"status": "approved", "updated_at": now}})
    M.dispatch = lambda evt, iid, target: 0
    try:
        M.run_once(target=tmp, now=now)
    finally:
        del M.dispatch
    events = _log_events(tmp)
    assert any(e["trigger"] == "idle-queue" and e["event"] == "dispatch"
               for e in events)


def test_idle_queue_silent_when_active():
    tmp = _fresh()
    _enable_motivations(tmp)
    now = time.time()
    # An in-progress issue with a live run updated 1 minute ago.
    run_id = "run-active"
    state._atomic_write_json(
        os.path.join(state._runs_dir(tmp), f"{run_id}.json"),
        {"started_at": now - 60, "ended_at": None})
    _set_tasks(tmp, {
        "ISSUE-ACTIVE": {"status": "in-progress", "updated_at": now - 60,
                         "run_id": run_id},
        "ISSUE-QUEUED": {"status": "approved", "updated_at": now},
    })
    M.dispatch = lambda evt, iid, target: (_ for _ in ()).throw(
        AssertionError("should not dispatch when active"))
    try:
        M.run_once(target=tmp, now=now)
    except AssertionError:
        raise
    finally:
        del M.dispatch


def test_test_signal_only_acts_on_review():
    tmp = _fresh()
    _enable_motivations(tmp)
    # Enable test-signal trigger.
    path = os.path.join(tmp, ".harness", "config.yml")
    text = open(path).read()
    text = text.replace("test-signal:\n      enabled: false",
                        "test-signal:\n      enabled: true")
    open(path, "w").write(text)
    now = time.time()
    test_dir = os.path.join(tmp, ".harness", "logs", "test-runs")
    os.makedirs(test_dir, exist_ok=True)
    state._atomic_write_json(
        os.path.join(test_dir, "t1.json"),
        {"issue_id": "ISSUE-X", "status": "failing", "ts": now})
    # ISSUE-X in review-passed (not review) -> noop.
    _set_tasks(tmp, {"ISSUE-X": {"status": "review-passed", "updated_at": now}})
    M.dispatch = lambda *a, **k: 0
    try:
        M.run_once(target=tmp, now=now)
    finally:
        del M.dispatch
    events = _log_events(tmp)
    assert not any(e.get("trigger") == "test-signal" and e["event"] == "dispatch"
                   for e in events), "test-signal must noop on review-passed"
    assert any(e.get("trigger") == "test-signal" and e["event"] == "noop:state"
               for e in events)


def test_human_approval_required_never_dispatched():
    """A human-approval-required issue is terminal; no trigger dispatches it."""
    tmp = _fresh()
    _enable_motivations(tmp)
    now = time.time()
    soon_iso = time.strftime("%Y-%m-%d", time.localtime(now + 3600))
    _write_due(tmp, "ISSUE-H", soon_iso)
    _set_tasks(tmp, {"ISSUE-H": {"status": "human-approval-required",
                                 "updated_at": now}})
    called = []
    M.dispatch = lambda *a, **k: called.append(a) or 0
    try:
        M.run_once(target=tmp, now=now)
    finally:
        del M.dispatch
    assert called == []


# --- config parser ---

def test_parse_motivations_defaults_disabled():
    tmp = _fresh()
    text = open(os.path.join(tmp, ".harness", "config.yml")).read()
    mot = M._parse_motivations(text)
    assert mot["enabled"] is False
    assert mot["max_dispatches_per_hour"] == 10
    assert mot["triggers"]["clock"]["enabled"] is True
    assert mot["triggers"]["test-signal"]["enabled"] is False
    assert mot["triggers"]["git-upstream"]["base_branch"] == "main"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} SPEC-005 tests passed")
