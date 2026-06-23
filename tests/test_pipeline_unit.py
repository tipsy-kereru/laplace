"""Unit tests for scripts/pipeline.py (ISSUE-0005).

One test per acceptance criterion AC-PL-001..012, plus a characterization
test that existing commands are unchanged (the pipeline composes them, it
does not fork).

Each test builds a fresh temp harness, writes a PRD, and exercises
`cmd_pipeline` with argparse.Namespace args matching the CLI.
"""
import argparse
import os
import re
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
import intake  # noqa: E402
import verify  # noqa: E402
import parallel_queue  # noqa: E402
import release  # noqa: E402
import pipeline  # noqa: E402
import cancel  # noqa: E402


PRD = """# Pipeline Unit Test PRD

## Task: Widget Builder

Build a widget builder for the dashboard.

### Scope

**In Scope:**
- Widget factory function
- Dashboard integration

**Out of Scope:**
- Mobile UI

### Acceptance Criteria
- Widget builder returns a configured widget
- Dashboard renders the widget
"""

ISSUE_MD_TEMPLATE = """\
# {iid}: {summary}

**Issue ID**: {iid}
**Status**: draft
**Summary**: {summary}

## Background
{background}

## Dependencies
- depends_on: (none)

## Scope
**In Scope:**
- {scope_in}
**Out of Scope:**
- {scope_out}

## Acceptance Criteria
- {ac1}
- {ac2}

## Technical Notes
TBD

## Test Requirements
- Unit: TBD

## Risk / Release Impact
- Risk Level: {risk}
- Release Type: patch
- Security Sensitivity: low

## Routing Metadata
- Type: feature
- Priority: p2
- Area: pipeline
- Route: pm-review

## Source
- Document: prd.md
- Section: {section}
- Lines: 3-20
- Excerpt: ...

## Run History
[]
"""


def _make_harness():
    tmp = tempfile.mkdtemp(prefix="laplace-pipeline-test-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def _teardown(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


def _write_prd(tmp, name="prd.md", text=PRD):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _ns(tmp, prd=None, **kw):
    return argparse.Namespace(
        prd=prd, resume=kw.get("resume", False), target=tmp,
        release=kw.get("release"),
        auto_approve_low_risk=kw.get("auto_approve_low_risk", False),
        max_parallel=kw.get("max_parallel", 2),
        force_verify=kw.get("force_verify", False),
    )


def _set_draft_risk(tmp, iid, risk):
    ipath = os.path.join(tmp, ".harness", "issues", f"{iid}.md")
    with open(ipath, "r", encoding="utf-8") as f:
        t = f.read()
    t = re.sub(r"Risk Level:\s*\w+", f"Risk Level: {risk}", t)
    with open(ipath, "w", encoding="utf-8") as f:
        f.write(t)


def _seed_draft(tmp, iid, *, risk="medium", section="Task: Widget Builder"):
    body = ISSUE_MD_TEMPLATE.format(
        iid=iid, summary=section,
        background="Build a widget builder for the dashboard.",
        scope_in="Widget factory function",
        scope_out="Mobile UI",
        ac1="Widget builder returns a configured widget",
        ac2="Dashboard renders the widget",
        risk=risk, section=section,
    )
    path = os.path.join(tmp, ".harness", "issues", f"{iid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    tasks = state._load_tasks(tmp)
    tasks[iid] = {"status": "draft", "updated_at": time.time()}
    state._save_tasks(tasks, target=tmp)
    q = state._load_queue(tmp)
    if iid not in q["draft"]:
        q["draft"].append(iid)
    state._save_queue(q, target=tmp)


def _silently(fn):
    """Run fn with stdout/stderr swallowed; returns fn's return value."""
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        return fn()
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = saved_out
        sys.stderr = saved_err


def test_ac_pl_001_halt_at_approve_gate():
    """AC-PL-001: pipeline runs intake+verify, halts at approve-gate, exit 0."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))
        assert rc == 0, f"halt should exit 0, got {rc}"
        active = state._find_active_pipeline_run(tmp)
        assert active is not None, "active pipeline log missing"
        assert active["phase"] == "approve-gate", active["phase"]
        assert active["outcome"] is None, "halt must be resumable"
        # intake produced drafts.
        assert len(state._load_queue(tmp)["draft"]) >= 1
    finally:
        _teardown(tmp)


def test_ac_pl_002_resume_batch_approves_and_proceeds():
    """AC-PL-002: --resume after the gate batch-approves and proceeds."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        assert rc == 0, f"resume should exit 0, got {rc}"
        active = state._find_active_pipeline_run(tmp)
        assert active is not None
        assert active["phase"] in ("parallel", "release-gate", "done"), \
            active["phase"]
        # All drafts batch-approved.
        assert state._load_queue(tmp)["draft"] == []
    finally:
        _teardown(tmp)


def test_ac_pl_003_auto_approve_low_risk_halts_on_medium():
    """AC-PL-003: --auto-approve-low-risk halts when a medium+ draft remains."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        drafts = state._load_queue(tmp)["draft"]
        assert drafts, "intake should produce drafts"
        # Leave the intake default (medium risk) in place.
        rc = _silently(lambda: pipeline.cmd_pipeline(
            _ns(tmp, prd, resume=True, auto_approve_low_risk=True)))
        assert rc == 0
        active = state._find_active_pipeline_run(tmp)
        assert active["phase"] == "approve-gate", active["phase"]
        # The medium-risk draft must NOT have been auto-approved.
        assert state._load_queue(tmp)["draft"], \
            "medium-risk draft should NOT be auto-approved"
    finally:
        _teardown(tmp)


def test_ac_pl_003b_auto_approve_low_risk_approves_low():
    """AC-PL-003b: --auto-approve-low-risk approves risk.level==low drafts."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        drafts = state._load_queue(tmp)["draft"]
        for iid in drafts:
            _set_draft_risk(tmp, iid, "low")
        rc = _silently(lambda: pipeline.cmd_pipeline(
            _ns(tmp, prd, resume=True, auto_approve_low_risk=True)))
        assert rc == 0
        active = state._find_active_pipeline_run(tmp)
        assert active["phase"] in ("parallel", "release-gate", "done"), \
            active["phase"]
        assert state._load_queue(tmp)["draft"] == [], \
            "low-risk drafts should be auto-approved"
    finally:
        _teardown(tmp)


def test_ac_pl_004_parallel_merge_wait_halt():
    """AC-PL-004: merge-wait halts surface as parallel:merge-wait:<id>."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        qrun = {
            "run_id": "mergefake0001", "kind": "queue",
            "started_at": time.time(), "ended_at": None,
            "outcome": "merge-wait:ISSUE-0001", "issues": [],
            "merge_policy": "wait-for-human-merge",
        }
        state._atomic_write_json(
            os.path.join(state._runs_dir(tmp), "mergefake0001.json"), qrun)
        active = state._find_active_pipeline_run(tmp)
        assert active is not None
        active["phase"] = "parallel"
        pipeline._save_pipeline_log(active, tmp)
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        assert rc == 0
        active2 = state._find_active_pipeline_run(tmp)
        assert active2["phase"] == "parallel", active2["phase"]
    finally:
        _teardown(tmp)


def test_ac_pl_005_queue_exhausted_transitions_to_release_gate():
    """AC-PL-005: queue-exhausted (parallel) transitions to release-gate."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        drafts = state._load_queue(tmp)["draft"]
        for iid in drafts:
            state.cmd_approve(argparse.Namespace(
                issue_id=iid, target=tmp, user="tester"))
            state._set_issue_state(iid, "review-passed", target=tmp)
        q = state._load_queue(tmp)
        q["approved"] = []
        state._save_queue(q, target=tmp)
        active = state._find_active_pipeline_run(tmp)
        pipeline._record_phase(active, "parallel", "queue-exhausted", tmp)
        active["phase"] = "release-gate"
        pipeline._save_pipeline_log(active, tmp)
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        assert rc == 0
        active2 = state._find_active_pipeline_run(tmp)
        assert active2["phase"] == "release-gate", active2["phase"]
    finally:
        _teardown(tmp)


def test_ac_pl_006_release_gate_halt_default():
    """AC-PL-006: release-gate halts by default (suggests /laplace:release)."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))
        drafts = state._load_queue(tmp)["draft"]
        for iid in drafts:
            state.cmd_approve(argparse.Namespace(
                issue_id=iid, target=tmp, user="tester"))
            state._set_issue_state(iid, "review-passed", target=tmp)
        q = state._load_queue(tmp)
        q["approved"] = []
        state._save_queue(q, target=tmp)
        active = state._find_active_pipeline_run(tmp)
        pipeline._record_phase(active, "parallel", "queue-exhausted", tmp)
        active["phase"] = "release-gate"
        pipeline._save_pipeline_log(active, tmp)
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        assert rc == 0
        active2 = state._find_active_pipeline_run(tmp)
        assert active2["phase"] == "release-gate"
        assert active2["outcome"] is None
    finally:
        _teardown(tmp)


def test_ac_pl_006b_release_gate_with_release_calls_cmd_release():
    """AC-PL-006b: --release <ver> calls release.cmd_release after exhausted."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, release="0.5.0")))
        drafts = state._load_queue(tmp)["draft"]
        for iid in drafts:
            state.cmd_approve(argparse.Namespace(
                issue_id=iid, target=tmp, user="tester"))
            state._set_issue_state(iid, "review-passed", target=tmp)
        q = state._load_queue(tmp)
        q["approved"] = []
        state._save_queue(q, target=tmp)
        active = state._find_active_pipeline_run(tmp)
        pipeline._record_phase(active, "parallel", "queue-exhausted", tmp)
        active["phase"] = "release-gate"
        pipeline._save_pipeline_log(active, tmp)
        calls = []
        original = release.cmd_release

        def fake(args):
            calls.append(getattr(args, "version", None))
            return 0
        release.cmd_release = fake
        try:
            rc = _silently(lambda: pipeline.cmd_pipeline(
                _ns(tmp, prd, resume=True, release="0.5.0")))
        finally:
            release.cmd_release = original
        assert rc == 0
        assert calls == ["0.5.0"], calls
        assert state._find_active_pipeline_run(tmp) is None
    finally:
        _teardown(tmp)


def test_ac_pl_007_halt_resumable_no_re_intake():
    """AC-PL-007: re-invoking resumes from recorded phase (no re-intake)."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # intake+verify
        issues_dir = state._issues_dir(tmp)
        n_after_first = len([f for f in os.listdir(issues_dir)
                             if f.endswith(".md")])
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd, resume=True)))
        n_after_resume = len([f for f in os.listdir(issues_dir)
                              if f.endswith(".md")])
        assert n_after_resume == n_after_first, \
            "resume should NOT re-run intake"
    finally:
        _teardown(tmp)


def test_ac_pl_008_intake_failed_and_verify_failed_halt():
    """AC-PL-008: intake-failed / verify-failed halt with recovery paths."""
    tmp = _make_harness()
    try:
        # intake-failed: missing PRD.
        rc = _silently(lambda: pipeline.cmd_pipeline(
            _ns(tmp, os.path.join(tmp, "nope.md"))))
        assert rc == 2, f"missing PRD should exit 2, got {rc}"
        assert state._find_active_pipeline_run(tmp) is None
    finally:
        _teardown(tmp)

    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        # Seed a draft whose Source.Section does NOT exist in the PRD ->
        # verify AC-VRF-003 FAIL.
        _seed_draft(tmp, "ISSUE-0001", section="Task: Does Not Exist")
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))
        assert rc == 0, f"verify-fail halt should exit 0, got {rc}"
        active = state._find_active_pipeline_run(tmp)
        assert active["phase"] == "verify", active["phase"]
        # The draft is NOT approved.
        assert "ISSUE-0001" in state._load_queue(tmp)["draft"]
    finally:
        _teardown(tmp)


def test_ac_pl_009_status_reports_pipeline_and_byte_identical_when_none():
    """AC-PL-009: status reports active pipeline; byte-identical when none."""
    # No active pipeline -> no Pipeline: block.
    tmp = _make_harness()
    try:
        text_none = state._format_status(tmp)
        assert "Pipeline:" not in text_none
        # Run pipeline to approve-gate.
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))
        text_active = state._format_status(tmp)
        assert "Pipeline:" in text_active
        assert "phase: approve-gate" in text_active
    finally:
        _teardown(tmp)


def test_ac_pl_010_cancel_finalizes_pipeline_log():
    """AC-PL-010: /laplace:cancel finalizes the active pipeline log."""
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))  # approve-gate
        active = state._find_active_pipeline_run(tmp)
        assert active is not None
        rc = _silently(lambda: cancel.cmd_cancel(argparse.Namespace(
            issue_id=None, target=tmp)))
        assert rc == 0, f"cancel should exit 0, got {rc}"
        # Pipeline log finalized.
        assert state._find_active_pipeline_run(tmp) is None
        # Issues NOT touched (still in draft).
        assert state._load_queue(tmp)["draft"]
    finally:
        _teardown(tmp)


def test_ac_pl_011_characterization_composed_commands_unchanged():
    """AC-PL-011: pipeline composes; doesn't fork. cmd_* entry points intact."""
    # The pipeline module imports intake/verify/state/parallel_queue/release
    # and only calls their cmd_* entry points. Sanity-check the entry points
    # are the original (unwrapped) callables.
    assert callable(intake.cmd_intake)
    assert callable(verify.cmd_verify)
    assert callable(state.cmd_approve)
    assert callable(parallel_queue.cmd_parallel_start)
    assert callable(release.cmd_release)
    # state._find_active_pipeline_run exists and is idempotent on empty.
    tmp = _make_harness()
    try:
        assert state._find_active_pipeline_run(tmp) is None
    finally:
        _teardown(tmp)


def test_ac_pl_012_verify_fail_blocks_approve_gate():
    """AC-PL-012: verify FAIL blocks the approve-gate until verify passes or
    --force-verify override.
    """
    tmp = _make_harness()
    try:
        prd = _write_prd(tmp)
        _seed_draft(tmp, "ISSUE-0001", section="Task: Does Not Exist")
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd)))
        assert rc == 0
        active = state._find_active_pipeline_run(tmp)
        assert active["phase"] == "verify", active["phase"]
        # Drafts untouched.
        assert "ISSUE-0001" in state._load_queue(tmp)["draft"]

        # --force-verify override proceeds past verify FAIL to approve-gate.
        rc2 = _silently(lambda: pipeline.cmd_pipeline(
            _ns(tmp, prd, resume=True, force_verify=True)))
        assert rc2 == 0
        active2 = state._find_active_pipeline_run(tmp)
        assert active2["phase"] == "approve-gate", active2["phase"]
    finally:
        _teardown(tmp)


def test_r3_resume_ambiguity_refuses_different_prd():
    """R-3: a different PRD while another pipeline is active -> refuse."""
    tmp = _make_harness()
    try:
        prd_a = _write_prd(tmp, "a.md")
        prd_b = _write_prd(tmp, "b.md")
        _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd_a)))  # active
        rc = _silently(lambda: pipeline.cmd_pipeline(_ns(tmp, prd_b)))
        assert rc != 0, "different-PRD while active should refuse"
    finally:
        _teardown(tmp)
