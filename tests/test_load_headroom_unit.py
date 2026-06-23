"""Unit tests for the load-aware rate limiter (ISSUE-0014).

Covers AC-RL-001..007:
  - AC-RL-001: os.getloadavg sampled per wave; ratio = load1 / cpu_count.
  - AC-RL-002: ratio < load_threshold -> full max_parallel (unchanged).
  - AC-RL-003: threshold <= ratio < severe -> reduced cap recorded as load_cap.
  - AC-RL-004: ratio >= severe -> wave-deferred:high-load:<ratio>, nothing
    dispatched, exit 0, resumable.
  - AC-RL-005: Windows (no getloadavg) -> skip check, dispatch static cap.
  - AC-RL-006: /laplace:status shows current ratio + effective cap.
  - AC-RL-007: characterization -- low load byte-identical to v0.4.0.

Each test mocks os.getloadavg / os.cpu_count for deterministic ratios.
"""
import argparse
import os
import shutil
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import parallel_queue  # noqa: E402
import state  # noqa: E402


@pytest.fixture
def harness():
    tmp = tempfile.mkdtemp(prefix="laplace-load-")
    assert state.cmd_init(target=tmp) == 0
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def patch_load(monkeypatch):
    """Force a specific load ratio by mocking cpu_count + getloadavg."""
    def _apply(ratio, cpu=4):
        monkeypatch.setattr(os, "cpu_count", lambda: cpu)
        monkeypatch.setattr(os, "getloadavg", lambda: (ratio * cpu, 0.0, 0.0))
    return _apply


def _seed_approved(tmp, issue_id):
    tasks = state._load_tasks(tmp)
    tasks[issue_id] = {"status": "draft", "updated_at": 0.0}
    state._save_tasks(tasks, target=tmp)
    q = state._load_queue(tmp)
    if issue_id not in q["draft"]:
        q["draft"].append(issue_id)
    state._save_queue(q, target=tmp)
    assert state.cmd_approve(argparse.Namespace(
        issue_id=issue_id, user="tester", target=tmp)) == 0


def _seed_many(tmp, n):
    state._save_tasks({}, target=tmp)
    state._save_queue(state.DEFAULT_QUEUE, target=tmp)
    for i in range(n):
        _seed_approved(tmp, f"ISSUE-L{i}")


# ---------------------------------------------------------------------------
# AC-RL-001/002: ratio below threshold -> full cap, no load_cap recorded
# ---------------------------------------------------------------------------

def test_low_load_full_cap_no_load_cap_recorded(harness, patch_load):
    patch_load(0.1, cpu=4)  # ratio 0.1 < 0.7
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid, rc = parallel_queue._run_parallel_wave(harness, cfg)
    assert rc == 0
    log = parallel_queue._load_parent_log(rid, harness)
    wave = log["waves"][-1]
    # Full cap dispatched.
    assert len(wave["dispatched"]) == cfg["max_parallel"]
    # AC-RL-007: load_cap must NOT be recorded under low load.
    assert "load_cap" not in wave


def test_load_headroom_low_load_returns_full_cap(harness, patch_load):
    patch_load(0.1, cpu=4)
    cfg = state.load_config(harness)
    hr = parallel_queue._load_headroom(harness, cfg)
    assert hr is not None
    assert hr["ratio"] == pytest.approx(0.1)
    assert hr["cap"] == cfg["max_parallel"]
    assert hr["deferred"] is False


# ---------------------------------------------------------------------------
# AC-RL-003: threshold <= ratio < severe -> reduced cap recorded
# ---------------------------------------------------------------------------

def test_mid_load_reduces_cap_and_records_load_cap(harness, patch_load):
    # max_parallel=2, threshold=0.7, severe=1.5, ratio=1.0 ->
    # ceil((1.0-0.7)*2) = 1 -> cap = max(1, 2-1) = 1.
    patch_load(1.0, cpu=4)
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid, rc = parallel_queue._run_parallel_wave(harness, cfg)
    assert rc == 0
    log = parallel_queue._load_parent_log(rid, harness)
    wave = log["waves"][-1]
    assert wave["load_cap"] == 1
    assert len(wave["dispatched"]) == 1


def test_load_headroom_mid_load_reduced_cap(harness, patch_load):
    patch_load(1.0, cpu=4)
    cfg = state.load_config(harness)
    hr = parallel_queue._load_headroom(harness, cfg)
    assert hr["cap"] == 1
    assert hr["deferred"] is False


def test_mid_load_cap_floor_is_one(harness, patch_load):
    # ratio very close to severe: ceil((1.49-0.7)*2) = ceil(1.58) = 2 ->
    # cap = max(1, 2-2) = 0? No: 2-2=0, max(1, 0) = 1 (floor).
    patch_load(1.49, cpu=4)
    cfg = state.load_config(harness)
    hr = parallel_queue._load_headroom(harness, cfg)
    assert hr["cap"] == 1  # never drops below 1 in the reduced band


# ---------------------------------------------------------------------------
# AC-RL-004: ratio >= severe -> wave-deferred, nothing dispatched, resumable
# ---------------------------------------------------------------------------

def test_severe_load_defers_wave(harness, patch_load):
    patch_load(2.0, cpu=4)  # ratio 2.0 >= 1.5
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid, rc = parallel_queue._run_parallel_wave(harness, cfg)
    assert rc == 0  # resumable, not an error
    log = parallel_queue._load_parent_log(rid, harness)
    assert log["outcome"].startswith("wave-deferred:high-load:")
    # Ratio appears in the outcome suffix.
    assert "2.0" in log["outcome"]
    wave = log["waves"][-1]
    assert wave["dispatched"] == []
    # Nothing left approved state.
    for i in range(5):
        assert state._load_tasks(harness)[f"ISSUE-L{i}"]["status"] == "approved"


def test_severe_load_parent_log_stays_resumable(harness, patch_load):
    patch_load(2.0, cpu=4)
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid, _ = parallel_queue._run_parallel_wave(harness, cfg)
    # Parallel run is still "active" (resumable) after a defer.
    active = state._find_active_parallel_run(harness)
    assert active is not None
    assert active["run_id"] == rid
    # Scheduler resume path also finds it.
    assert parallel_queue._find_open_parallel_run(harness) is not None


def test_severe_then_mid_resumes_and_dispatches(harness, patch_load):
    patch_load(2.0, cpu=4)
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid_defer, _ = parallel_queue._run_parallel_wave(harness, cfg)
    # Load drops into the reduced band.
    patch_load(1.0, cpu=4)
    rid_resume, rc = parallel_queue._run_parallel_wave(harness, cfg)
    assert rid_resume == rid_defer
    assert rc == 0
    log = parallel_queue._load_parent_log(rid_resume, harness)
    wave = log["waves"][-1]
    assert len(wave["dispatched"]) == 1


# ---------------------------------------------------------------------------
# AC-RL-005: Windows (no getloadavg) -> skip, dispatch static cap
# ---------------------------------------------------------------------------

def test_no_getloadavg_skips_check_and_dispatches_static(harness, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    # Simulate Windows: no getloadavg attribute on os.
    monkeypatch.delattr(os, "getloadavg", raising=False)
    # Reset module-level warning flag so the warn path is exercised.
    parallel_queue._LOAD_WARNED = False
    cfg = state.load_config(harness)
    _seed_many(harness, 5)
    rid, rc = parallel_queue._run_parallel_wave(harness, cfg)
    assert rc == 0
    log = parallel_queue._load_parent_log(rid, harness)
    wave = log["waves"][-1]
    # Full static cap dispatched (load check skipped).
    assert len(wave["dispatched"]) == cfg["max_parallel"]
    assert "load_cap" not in wave
    assert parallel_queue._load_headroom(harness, cfg) is None


# ---------------------------------------------------------------------------
# AC-RL-006: /laplace:status shows ratio + effective cap
# ---------------------------------------------------------------------------

def test_status_parallel_block_shows_load_line(harness, patch_load):
    patch_load(1.0, cpu=4)
    cfg = state.load_config(harness)
    _seed_many(harness, 3)
    parallel_queue._run_parallel_wave(harness, cfg)
    out = state._format_status(harness)
    assert "Parallel run:" in out
    assert "Load: ratio=" in out
    assert "cap 1 of max_parallel 2" in out


def test_status_load_line_omitted_when_no_getloadavg(harness, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.delattr(os, "getloadavg", raising=False)
    parallel_queue._LOAD_WARNED = False
    cfg = state.load_config(harness)
    _seed_many(harness, 3)
    parallel_queue._run_parallel_wave(harness, cfg)
    out = state._format_status(harness)
    assert "Parallel run:" in out
    assert "Load:" not in out


# ---------------------------------------------------------------------------
# AC-RL-007: characterization -- low load wave JSON shape unchanged
# ---------------------------------------------------------------------------

def test_low_load_wave_keys_match_v040(harness, patch_load):
    """Under low load the wave entry must contain exactly the v0.4.0 keys:
    ts, dispatched, in_flight, halted, ready_count (no load_cap key)."""
    patch_load(0.1, cpu=4)
    cfg = state.load_config(harness)
    _seed_many(harness, 3)
    rid, _ = parallel_queue._run_parallel_wave(harness, cfg)
    log = parallel_queue._load_parent_log(rid, harness)
    wave = log["waves"][-1]
    assert set(wave.keys()) == {
        "ts", "dispatched", "in_flight", "halted", "ready_count"}
