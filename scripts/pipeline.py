#!/usr/bin/env python3
"""Laplace pipeline orchestrator (ISSUE-0005).

A checkpoint pipeline that composes the 5 existing Laplace commands into a
single end-to-end flow:

    intake -> verify -> approve-gate -> parallel -> release-gate -> done

Each phase calls an existing command's Python entry point
(``intake.cmd_intake``, ``verify.cmd_verify``, ``state.cmd_approve``,
``parallel_queue.cmd_parallel_start``, ``release.cmd_release``), records
the phase transition in a pipeline-run log, and either proceeds (auto
phases) or halts (gates). Resume reads the log and jumps to the recorded
phase.

Design notes:
  - Thin state machine: composes, never re-implements. Every gate in the
    composed commands still fires.
  - Every gate halts: approve-gate, verify-failed, merge-wait,
    release-gate. Resume continues from the recorded phase.
  - R-5 (state drift): the dispatcher re-reads disk at each phase entry;
    the recorded phase is a hint, disk is truth.
  - stdlib only. No subprocess to the composed commands -- import and
    call their ``cmd_*`` entry points directly.

Pipeline-run log at ``.harness/state/runs/<pipeline-run-id>.json``:
    {run_id, kind:"pipeline", prd, started_at, ended_at, outcome, phase,
     phase_history:[{ts, phase, result}], max_parallel,
     auto_approve_low_risk, release_version, force_verify}
"""

import argparse
import hashlib
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer modules imported after the sys.path bootstrap (mirrors the other
# scripts). pipeline.py only composes their cmd_* entry points.
import state  # noqa: E402
import intake  # noqa: E402
import verify  # noqa: E402
import parallel_queue  # noqa: E402
import release  # noqa: E402

# Phase ordering (SPEC §Scope).
PHASES = ["intake", "verify", "approve-gate", "parallel",
          "release-gate", "done"]

# Issue statuses that count as "in-flight" for status reporting + halt
# detection (mirrors parallel_queue.IN_FLIGHT_STATUSES).
_IN_FLIGHT_STATUSES = (
    "pm-review", "ready-for-dev", "in-progress", "review",
    "needs-fix", "security-review",
)

# Halted-issues statuses (parallel-blocked gate).
_HALTED_STATUSES = ("blocked", "human-approval-required")


def _new_pipeline_run_id() -> str:
    raw = f"pipeline-{time.time()}-{os.getpid()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _pipeline_run_log_path(run_id: str, target: Optional[str]) -> str:
    return os.path.join(state._runs_dir(target), f"{run_id}.json")


def _create_pipeline_log(run_id: str, prd: str, target: Optional[str],
                         max_parallel: int, auto_approve_low_risk: bool,
                         release_version: Optional[str],
                         force_verify: bool) -> Dict[str, Any]:
    log: Dict[str, Any] = {
        "run_id": run_id,
        "kind": "pipeline",
        "prd": state._redact_evidence(prd),
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,
        "phase": "intake",
        "phase_history": [],
        "max_parallel": max_parallel,
        "auto_approve_low_risk": auto_approve_low_risk,
        "release_version": release_version,
        "force_verify": force_verify,
    }
    state._atomic_write_json(_pipeline_run_log_path(run_id, target), log)
    return log


def _load_pipeline_log(run_id: str, target: Optional[str]) \
        -> Optional[Dict[str, Any]]:
    log = state._read_json(_pipeline_run_log_path(run_id, target),
                           default=None)
    return log if isinstance(log, dict) else None


def _save_pipeline_log(log: Dict[str, Any], target: Optional[str]) -> None:
    run_id = log.get("run_id") or ""
    if not run_id:
        return
    state._atomic_write_json(_pipeline_run_log_path(run_id, target), log)


def _finalize_pipeline_log(log: Dict[str, Any], outcome: str,
                           target: Optional[str]) -> None:
    log["outcome"] = state._redact_evidence(outcome)
    log["ended_at"] = time.time()
    _save_pipeline_log(log, target)


def _record_phase(log: Dict[str, Any], phase: str, result: str,
                  target: Optional[str]) -> None:
    log.setdefault("phase_history", []).append({
        "ts": time.time(),
        "phase": phase,
        "result": state._redact_evidence(result),
    })
    log["phase"] = phase
    _save_pipeline_log(log, target)


def _realpath(p: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(p))
    except Exception:
        return os.path.abspath(p)


def _drafts(target: Optional[str]) -> List[str]:
    return list(state._load_queue(target).get("draft", []))


def _parse_risk_level(issue_id: str, target: Optional[str]) -> str:
    """Read the Risk Level field from the issue .md file. Defaults to medium."""
    path = os.path.join(state._issues_dir(target), f"{issue_id}.md")
    if not os.path.isfile(path):
        return "medium"
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "medium"
    m = re.search(r"(?im)^\s*-\s*Risk Level\s*:\s*(\w+)\s*$", text)
    if not m:
        return "medium"
    return m.group(1).strip().lower() or "medium"


def _halted_issues(target: Optional[str]) -> List[str]:
    """Issues currently in a parallel-blocked style halt state."""
    tasks = state._load_tasks(target)
    out: List[str] = []
    for iid, meta in tasks.items():
        if meta.get("status") in _HALTED_STATUSES:
            out.append(iid)
    return out


def _in_flight_count(target: Optional[str]) -> int:
    tasks = state._load_tasks(target)
    return sum(1 for meta in tasks.values()
               if meta.get("status") in _IN_FLIGHT_STATUSES)


# ---------------------------------------------------------------------------
# Phase handlers (each returns None to proceed, or a halt sub-state str)
# ---------------------------------------------------------------------------

def _phase_intake(log: Dict[str, Any], args: argparse.Namespace,
                  target: Optional[str]) -> Optional[str]:
    """Run intake. Returns None to proceed to verify, or a halt sub-state."""
    prd = log.get("prd") or ""
    rc = intake.cmd_intake(prd, target=target)
    if rc != 0:
        return "intake-failed"
    _record_phase(log, "intake", f"rc={rc}", target)
    log["phase"] = "verify"
    _save_pipeline_log(log, target)
    return None


def _phase_verify(log: Dict[str, Any], args: argparse.Namespace,
                  target: Optional[str]) -> Optional[str]:
    """Run verify. rc 0/2 -> proceed; rc 1 (FAIL) -> halt unless --force-verify."""
    prd = log.get("prd") or ""
    ns = argparse.Namespace(prd_path=prd, target=target)
    rc = verify.cmd_verify(ns)
    if rc == 1:
        if getattr(args, "force_verify", False) or log.get("force_verify"):
            _record_phase(log, "verify", "rc=1 (force-verify)", target)
            log["phase"] = "approve-gate"
            _save_pipeline_log(log, target)
            return None
        return "verify-failed"
    if rc == 2:
        # Usage error from verify itself; surface as a halt.
        return "verify-usage"
    _record_phase(log, "verify", f"rc={rc}", target)
    log["phase"] = "approve-gate"
    _save_pipeline_log(log, target)
    return None


def _phase_approve_gate(log: Dict[str, Any], args: argparse.Namespace,
                        target: Optional[str]) -> Optional[str]:
    """Approve-gate. Default halt; on resume batch-approve all drafts.

    With --auto-approve-low-risk: approve risk.level==low drafts inline;
    halt if any medium+/high draft remains.
    """
    drafts = _drafts(target)
    auto = bool(log.get("auto_approve_low_risk"))

    # SPEC-007: when freerange `flow` is active, the approve-gate halt is
    # skipped and all drafts are auto-approved via cmd_approve with
    # user="freerange" (matching the --auto-approve-low-risk precedent).
    try:
        import freerange  # type: ignore
        flow_active = freerange.suppressed_by_freerange(
            "issue_approval", target)
    except Exception:
        flow_active = False
    if flow_active and drafts:
        for iid in drafts:
            ns = argparse.Namespace(issue_id=iid, target=target,
                                    user="freerange")
            state.cmd_approve(ns)
        _record_phase(log, "approve-gate",
                      f"freerange-flow approved {len(drafts)} drafts",
                      target)
        log["phase"] = "parallel"
        _save_pipeline_log(log, target)
        return None

    if auto:
        low_drafts: List[str] = []
        held_drafts: List[str] = []
        for iid in drafts:
            lvl = _parse_risk_level(iid, target)
            if lvl == "low":
                low_drafts.append(iid)
            else:
                held_drafts.append(iid)
        for iid in low_drafts:
            ns = argparse.Namespace(issue_id=iid, target=target,
                                    user="pipeline")
            state.cmd_approve(ns)
        if held_drafts:
            # Halt surfacing medium+/high drafts for manual approve.
            return f"approve-gate:{','.join(held_drafts)}"
        _record_phase(log, "approve-gate",
                      f"auto-approved {len(low_drafts)} low-risk",
                      target)
        log["phase"] = "parallel"
        _save_pipeline_log(log, target)
        return None

    # Default: halt once, surfacing verify report + drafts + per-issue risk.
    if drafts:
        risk_summary = ",".join(
            f"{iid}={_parse_risk_level(iid, target)}" for iid in drafts)
        return f"approve-gate:{risk_summary}"
    _record_phase(log, "approve-gate", "no drafts", target)
    log["phase"] = "parallel"
    _save_pipeline_log(log, target)
    return None


def _resume_approve_gate(log: Dict[str, Any], target: Optional[str]) -> None:
    """Batch-approve all remaining drafts (resume after human gate)."""
    drafts = _drafts(target)
    for iid in drafts:
        ns = argparse.Namespace(issue_id=iid, target=target, user="pipeline")
        state.cmd_approve(ns)
    _record_phase(log, "approve-gate",
                  f"batch-approved {len(drafts)} drafts", target)
    log["phase"] = "parallel"
    _save_pipeline_log(log, target)


def _phase_parallel(log: Dict[str, Any], args: argparse.Namespace,
                    target: Optional[str]) -> Optional[str]:
    """Parallel phase: dispatch one wave, then map the active parallel run."""
    halted = _halted_issues(target)
    if halted:
        return f"parallel-blocked:{halted[0]}"

    ns = argparse.Namespace(target=target)
    rc = parallel_queue.cmd_parallel_start(ns)
    if rc != 0:
        active = state._find_active_parallel_run(target)
        outcome = active.get("outcome") if active else None
        if isinstance(outcome, str) and outcome.startswith("start-failed:"):
            iid = outcome.split(":", 1)[1].split(":", 1)[0]
            return f"parallel-blocked:{iid}"
        return "parallel-blocked:?"

    active = state._find_active_parallel_run(target)
    outcome = active.get("outcome") if active else None

    if outcome is None or outcome in ("wave-dispatched",
                                      "wave-dispatched:waiting"):
        return "parallel:wave-dispatched:waiting"
    if isinstance(outcome, str) and outcome.startswith("cancel-failed:"):
        suffix = outcome.split(":", 1)[1]
        return f"parallel:cancel-failed:{suffix}"
    if isinstance(outcome, str) and outcome.startswith("merge-"):
        suffix = outcome.split(":", 1)[1] if ":" in outcome else "?"
        return f"parallel:merge-wait:{suffix}"
    if outcome == "queue-exhausted":
        if _halted_issues(target):
            return f"parallel-blocked:{_halted_issues(target)[0]}"
        _record_phase(log, "parallel", "queue-exhausted", target)
        log["phase"] = "release-gate"
        _save_pipeline_log(log, target)
        return None
    if isinstance(outcome, str) and outcome.startswith("start-failed:"):
        iid = outcome.split(":", 1)[1].split(":", 1)[0]
        return f"parallel-blocked:{iid}"
    return "parallel:wave-dispatched:waiting"


def _phase_release_gate(log: Dict[str, Any], args: argparse.Namespace,
                        target: Optional[str]) -> Optional[str]:
    """Release gate. Default halt; with --release <ver> call release.cmd_release."""
    ver = log.get("release_version")
    reached_exhausted = any(
        ph.get("phase") == "parallel"
        and ph.get("result") == "queue-exhausted"
        for ph in log.get("phase_history", []))
    has_halted = bool(_halted_issues(target))

    if ver and reached_exhausted and not has_halted:
        ns = argparse.Namespace(version=ver, target=target, force=False)
        try:
            rc = release.cmd_release(ns)
        except Exception as exc:  # noqa: BLE001 -- surface, don't crash
            print(f"release.cmd_release raised: {exc}", file=sys.stderr)
            return "release-failed"
        if rc != 0:
            return "release-failed"
        _record_phase(log, "release-gate", f"released {ver}", target)
        log["phase"] = "done"
        _save_pipeline_log(log, target)
        return None
    return "release-gate"


# ---------------------------------------------------------------------------
# Halt message rendering
# ---------------------------------------------------------------------------

def _print_halt(sub_state: str, log: Dict[str, Any],
                target: Optional[str]) -> None:
    phase = log.get("phase", "?")
    print(f"Pipeline halt: {sub_state}")
    print(f"  Phase: {phase}")
    if phase == "approve-gate" and sub_state.startswith("approve-gate:"):
        body = sub_state.split(":", 1)[1]
        print(f"  Drafts (issue=risk): {body}")
        print("  Next: review the verify report above, then re-run "
              "/laplace:pipeline --resume to batch-approve all drafts.")
    elif phase == "approve-gate":
        drafts = _drafts(target)
        risk_summary = ",".join(
            f"{iid}={_parse_risk_level(iid, target)}" for iid in drafts)
        print(f"  Drafts (issue=risk): {risk_summary}")
        print("  Next: review the verify report above, then re-run "
              "/laplace:pipeline --resume to batch-approve all drafts.")
    elif sub_state == "verify-failed":
        print("  Next: fix the verify failures above (or re-run with "
              "--force-verify as the documented escape hatch).")
    elif sub_state == "verify-usage":
        print("  Next: verify returned a usage error; inspect the PRD path "
              "and .harness/ state.")
    elif sub_state == "intake-failed":
        print("  Next: fix the intake failure above (PRD parse error or "
              "missing .harness/) then re-run /laplace:pipeline --resume.")
    elif sub_state.startswith("parallel:merge-wait:"):
        iid = sub_state.split(":", 2)[2]
        print(f"  Merge-waited issue: {iid}")
        print(f"  Next: merge {iid} (or /laplace:cancel {iid}), then "
              f"re-run /laplace:pipeline --resume to dispatch the next wave.")
    elif sub_state == "parallel:wave-dispatched:waiting":
        print("  Next: drive each in-flight issue to a terminal state, "
              "then re-run /laplace:pipeline --resume.")
    elif sub_state.startswith("parallel:cancel-failed:"):
        iid = sub_state.split(":", 2)[2]
        print(f"  Stranded: {iid}")
        print(f"  Next: run /laplace:cancel {iid} to resolve, then re-run "
              f"/laplace:pipeline --resume.")
    elif sub_state.startswith("parallel-blocked:"):
        iid = sub_state.split(":", 1)[1]
        print(f"  Blocked issue: {iid}")
        print(f"  Next: resolve {iid} (blocked/human-approval-required/"
              f"start-failed), then re-run /laplace:pipeline --resume.")
    elif sub_state == "release-gate":
        ver = log.get("release_version")
        if ver:
            print(f"  Next: /laplace:release {ver}  (or re-run "
                  f"/laplace:pipeline --release {ver} --resume)")
        else:
            print("  Next: /laplace:release <X.Y.Z>  (or re-run "
                  "/laplace:pipeline --release <X.Y.Z> --resume)")
    elif sub_state == "release-failed":
        print("  Next: release.cmd_release halted; resolve the failing "
              "check (see message above), then re-run "
              "/laplace:pipeline --resume.")
    else:
        print("  Next: resolve the gate above, then re-run "
              "/laplace:pipeline --resume.")


# ---------------------------------------------------------------------------
# Phase dispatcher
# ---------------------------------------------------------------------------

def _dispatch_one_phase(log: Dict[str, Any], args: argparse.Namespace,
                        target: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Run one phase. Returns (should_continue, halt_sub_state)."""
    phase = log.get("phase") or "intake"
    if phase == "intake":
        halt = _phase_intake(log, args, target)
        return (halt is None, halt)
    if phase == "verify":
        halt = _phase_verify(log, args, target)
        return (halt is None, halt)
    if phase == "approve-gate":
        halt = _phase_approve_gate(log, args, target)
        return (halt is None, halt)
    if phase == "parallel":
        halt = _phase_parallel(log, args, target)
        return (halt is None, halt)
    if phase == "release-gate":
        halt = _phase_release_gate(log, args, target)
        return (halt is None, halt)
    if phase == "done":
        return (False, None)
    return (False, None)


def cmd_pipeline(args: argparse.Namespace) -> int:
    target = getattr(args, "target", None)
    resume = bool(getattr(args, "resume", False))
    prd_arg = getattr(args, "prd", None)

    root = state._harness_root(target)
    if not os.path.isdir(os.path.join(root, ".harness")):
        print(f"Laplace is not initialized at {root}. Run /laplace:init first.",
              file=sys.stderr)
        return 2

    active = state._find_active_pipeline_run(target)

    if resume:
        if active is None:
            print("no active pipeline to resume", file=sys.stderr)
            return 1
        log = active
        # Refresh flags from the CLI so a resume can switch modes.
        if getattr(args, "auto_approve_low_risk", False):
            log["auto_approve_low_risk"] = True
            _save_pipeline_log(log, target)
        if getattr(args, "force_verify", False):
            log["force_verify"] = True
            _save_pipeline_log(log, target)
        if getattr(args, "release", None) and not log.get("release_version"):
            log["release_version"] = getattr(args, "release")
            _save_pipeline_log(log, target)
        # Default-mode resume at approve-gate: batch-approve all drafts.
        # (Auto-approve-low-risk mode lets the dispatcher re-run the gate so
        # the risk filter + medium+ halt fires again.)
        if log.get("phase") == "approve-gate" and not \
                log.get("auto_approve_low_risk"):
            _resume_approve_gate(log, target)
    else:
        if active is not None:
            if prd_arg is not None:
                # R-3: ambiguity check.
                active_prd = active.get("prd") or ""
                if _realpath(active_prd) != _realpath(prd_arg):
                    other = os.path.basename(active_prd)
                    print(
                        f"active pipeline for {other}; cancel it first "
                        f"or use --resume", file=sys.stderr)
                    return 1
            log = active
            if getattr(args, "auto_approve_low_risk", False):
                log["auto_approve_low_risk"] = True
                _save_pipeline_log(log, target)
            # Implicit resume (same PRD, no --resume flag).
            if log.get("phase") == "approve-gate" and not \
                    log.get("auto_approve_low_risk"):
                _resume_approve_gate(log, target)
        else:
            if not prd_arg:
                print("prd argument required for a fresh pipeline",
                      file=sys.stderr)
                return 2
            if not os.path.isfile(prd_arg):
                print(f"PRD not found: {prd_arg}", file=sys.stderr)
                return 2
            run_id = _new_pipeline_run_id()
            max_parallel = getattr(args, "max_parallel", None) or \
                state.MAX_PARALLEL
            log = _create_pipeline_log(
                run_id, _realpath(prd_arg), target,
                max_parallel=max_parallel,
                auto_approve_low_risk=bool(getattr(args, "auto_approve_low_risk",
                                                   False)),
                release_version=getattr(args, "release", None),
                force_verify=bool(getattr(args, "force_verify", False)),
            )

    # Each iteration re-reads the log from disk (R-5) and runs one phase.
    max_iters = len(PHASES) + 4
    for _ in range(max_iters):
        run_id = log.get("run_id") or ""
        if run_id:
            fresh = _load_pipeline_log(run_id, target)
            if fresh is not None:
                log = fresh

        phase = log.get("phase") or "intake"
        if phase == "done":
            if log.get("outcome") is None:
                _finalize_pipeline_log(log, "released", target)
            print("Pipeline complete.")
            print(f"  Run: {run_id}")
            return 0

        should_continue, halt = _dispatch_one_phase(log, args, target)

        if not should_continue:
            if halt is not None:
                _save_pipeline_log(log, target)
                _print_halt(halt, log, target)
                return 0
            if log.get("outcome") is None and log.get("phase") == "done":
                _finalize_pipeline_log(log, "released", target)
            return 0

    print("pipeline: exceeded phase iteration bound", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# selftest (temp harness, stdlib only; release is stubbed for --release case)
# ---------------------------------------------------------------------------

def _stub_release_for_selftest() -> Tuple[Dict[str, Any], Any]:
    """Stub release.cmd_release so selftest never does real git push."""
    monkey: Dict[str, Any] = {"calls": [], "return_rc": 0}
    original = release.cmd_release

    def fake(args):
        monkey["calls"].append({
            "version": getattr(args, "version", None),
            "target": getattr(args, "target", None),
        })
        return monkey["return_rc"]

    release.cmd_release = fake
    return monkey, original


def _write_prd(repo: str, name: str, text: str) -> str:
    p = os.path.join(repo, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


_PRD = """# Pipeline Selftest PRD

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

_ISSUE_MD_TEMPLATE = """\
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


def _seed_draft_issue(repo: str, iid: str, *, risk: str = "low",
                      document: str = "prd.md",
                      section: str = "Task: Widget Builder") -> None:
    """Write a draft issue .md directly + register in tasks/queue (no intake)."""
    body = _ISSUE_MD_TEMPLATE.format(
        iid=iid, summary=section,
        background="Build a widget builder for the dashboard.",
        scope_in="Widget factory function",
        scope_out="Mobile UI",
        ac1="Widget builder returns a configured widget",
        ac2="Dashboard renders the widget",
        risk=risk, document=document, section=section,
    )
    path = os.path.join(repo, ".harness", "issues", f"{iid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    tasks = state._load_tasks(repo)
    tasks[iid] = {"status": "draft", "updated_at": time.time()}
    state._save_tasks(tasks, target=repo)
    q = state._load_queue(repo)
    if iid not in q["draft"]:
        q["draft"].append(iid)
    state._save_queue(q, target=repo)


def selftest() -> int:
    """Temp-harness selftest: 8 pipeline cases per the PM plan."""
    import shutil
    import tempfile

    failures: List[str] = []
    tmp = tempfile.mkdtemp(prefix="laplace-pipeline-selftest-")
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        def fresh_repo() -> str:
            d = tempfile.mkdtemp(prefix="laplace-pipeline-case-")
            assert state.cmd_init(target=d) == 0
            return d

        def ns_run(repo, prd, **kw):
            return argparse.Namespace(
                prd=prd, resume=kw.get("resume", False), target=repo,
                release=kw.get("release"),
                auto_approve_low_risk=kw.get("auto_approve_low_risk", False),
                max_parallel=kw.get("max_parallel", 2),
                force_verify=kw.get("force_verify", False),
            )

        # Case 1: happy-with-halts (intake + verify + approve-gate halt)
        r1 = fresh_repo()
        prd1 = _write_prd(r1, "prd.md", _PRD)
        rc1 = cmd_pipeline(ns_run(r1, prd1))
        if rc1 != 0:
            failures.append(f"case1 halt should exit 0, got {rc1}")
        active1 = state._find_active_pipeline_run(r1)
        if not active1:
            failures.append("case1: active pipeline log missing")
        elif active1.get("phase") != "approve-gate":
            failures.append(
                f"case1: expected phase approve-gate, got {active1.get('phase')}")
        elif active1.get("outcome") is not None:
            failures.append("case1: outcome should be None (resumable halt)")
        if not _drafts(r1):
            failures.append("case1: no drafts after intake")
        shutil.rmtree(r1, ignore_errors=True)

        # Case 2: resume-after-approve -> parallel phase
        r2 = fresh_repo()
        prd2 = _write_prd(r2, "prd.md", _PRD)
        cmd_pipeline(ns_run(r2, prd2))  # halt at approve-gate
        rc2b = cmd_pipeline(ns_run(r2, prd2, resume=True))
        if rc2b != 0:
            failures.append(f"case2 resume should exit 0, got {rc2b}")
        active2 = state._find_active_pipeline_run(r2)
        if not active2:
            failures.append("case2: active pipeline missing after resume")
        elif active2.get("phase") not in ("parallel", "release-gate", "done"):
            failures.append(
                f"case2: expected phase parallel/release-gate/done, "
                f"got {active2.get('phase')}")
        if _drafts(r2):
            failures.append("case2: drafts should be empty after resume-approve")
        shutil.rmtree(r2, ignore_errors=True)

        # Case 3: resume-after-merge (merge-wait halts)
        r3 = fresh_repo()
        prd3 = _write_prd(r3, "prd.md", _PRD)
        cmd_pipeline(ns_run(r3, prd3))
        cmd_pipeline(ns_run(r3, prd3, resume=True))
        qrun = {
            "run_id": "mergefake0001", "kind": "queue",
            "started_at": time.time(), "ended_at": None,
            "outcome": "merge-wait:ISSUE-0001", "issues": [],
            "merge_policy": "wait-for-human-merge",
        }
        state._atomic_write_json(
            os.path.join(state._runs_dir(r3), "mergefake0001.json"), qrun)
        active3 = state._find_active_pipeline_run(r3)
        if not active3:
            failures.append("case3: active pipeline missing")
        else:
            active3["phase"] = "parallel"
            _save_pipeline_log(active3, r3)
            rc3c = cmd_pipeline(ns_run(r3, prd3, resume=True))
            if rc3c != 0:
                failures.append(
                    f"case3 resume-after-merge should exit 0, got {rc3c}")
            active3b = state._find_active_pipeline_run(r3)
            if active3b and active3b.get("phase") != "parallel":
                failures.append(
                    f"case3: phase should stay parallel on merge-wait, "
                    f"got {active3b.get('phase')}")
        shutil.rmtree(r3, ignore_errors=True)

        # Case 4a: release-gate WITHOUT --release (halt)
        r4 = fresh_repo()
        prd4 = _write_prd(r4, "prd.md", _PRD)
        cmd_pipeline(ns_run(r4, prd4))  # approve-gate halt
        drafts4 = _drafts(r4)
        for iid in drafts4:
            state.cmd_approve(argparse.Namespace(
                issue_id=iid, target=r4, user="pipeline"))
            state._set_issue_state(iid, "review-passed", target=r4)
        q4 = state._load_queue(r4)
        q4["approved"] = []
        state._save_queue(q4, target=r4)
        active4 = state._find_active_pipeline_run(r4)
        if not active4:
            failures.append("case4: active pipeline missing")
        else:
            _record_phase(active4, "parallel", "queue-exhausted", r4)
            active4["phase"] = "release-gate"
            _save_pipeline_log(active4, r4)
            rc4 = cmd_pipeline(ns_run(r4, prd4))
            if rc4 != 0:
                failures.append(
                    f"case4 release-gate halt should exit 0, got {rc4}")
            active4b = state._find_active_pipeline_run(r4)
            if active4b and active4b.get("phase") != "release-gate":
                failures.append(
                    f"case4: phase should stay release-gate (halt), "
                    f"got {active4b.get('phase')}")
        shutil.rmtree(r4, ignore_errors=True)

        # Case 4b: release-gate WITH --release (calls release.cmd_release)
        r4b = fresh_repo()
        prd4b = _write_prd(r4b, "prd.md", _PRD)
        cmd_pipeline(ns_run(r4b, prd4b, release="0.5.0"))  # approve-gate
        drafts4b = _drafts(r4b)
        for iid in drafts4b:
            state.cmd_approve(argparse.Namespace(
                issue_id=iid, target=r4b, user="pipeline"))
            state._set_issue_state(iid, "review-passed", target=r4b)
        q4b = state._load_queue(r4b)
        q4b["approved"] = []
        state._save_queue(q4b, target=r4b)
        active4b = state._find_active_pipeline_run(r4b)
        _record_phase(active4b, "parallel", "queue-exhausted", r4b)
        active4b["phase"] = "release-gate"
        _save_pipeline_log(active4b, r4b)
        monkey, real_release = _stub_release_for_selftest()
        try:
            rc4b = cmd_pipeline(ns_run(r4b, prd4b, release="0.5.0"))
        finally:
            release.cmd_release = real_release
        if rc4b != 0:
            failures.append(f"case4b release should exit 0, got {rc4b}")
        if not monkey["calls"]:
            failures.append("case4b: release.cmd_release was not called")
        elif monkey["calls"][0]["version"] != "0.5.0":
            failures.append(
                f"case4b: release called with wrong version: {monkey['calls']}")
        active4bb = state._find_active_pipeline_run(r4b)
        if active4bb:
            failures.append(
                "case4b: pipeline log should be finalized (no active run)")
        shutil.rmtree(r4b, ignore_errors=True)

        # Case 5: verify-fail block
        r5 = fresh_repo()
        prd5 = _write_prd(r5, "prd.md", _PRD)
        _seed_draft_issue(r5, "ISSUE-0001", section="Task: Does Not Exist")
        rc5 = cmd_pipeline(ns_run(r5, prd5))
        if rc5 != 0:
            failures.append(f"case5 verify-fail halt should exit 0, got {rc5}")
        active5 = state._find_active_pipeline_run(r5)
        if not active5:
            failures.append("case5: active pipeline missing")
        elif active5.get("phase") != "verify":
            failures.append(
                f"case5: phase should stay verify (halt), "
                f"got {active5.get('phase')}")
        if not _drafts(r5):
            failures.append("case5: drafts should still exist (verify failed)")
        shutil.rmtree(r5, ignore_errors=True)

        # Case 6: --auto-approve-low-risk (low drafts approved inline)
        r6 = fresh_repo()
        prd6 = _write_prd(r6, "prd.md", _PRD)
        # First run halts at approve-gate; rewrite the draft's Risk Level to
        # low (intake defaults to medium), then switch the active log into
        # auto-approve mode and resume.
        cmd_pipeline(ns_run(r6, prd6))  # halt at approve-gate
        drafts6 = _drafts(r6)
        for iid in drafts6:
            ipath = os.path.join(r6, ".harness", "issues", f"{iid}.md")
            with open(ipath, "r", encoding="utf-8") as f:
                t = f.read()
            t = re.sub(r"Risk Level:\s*\w+", "Risk Level: low", t)
            with open(ipath, "w", encoding="utf-8") as f:
                f.write(t)
        active6 = state._find_active_pipeline_run(r6)
        if not active6:
            failures.append("case6: active pipeline missing before resume")
        else:
            active6["auto_approve_low_risk"] = True
            _save_pipeline_log(active6, r6)
            rc6 = cmd_pipeline(ns_run(r6, prd6, resume=True,
                                      auto_approve_low_risk=True))
            if rc6 != 0:
                failures.append(f"case6 auto-approve should exit 0, got {rc6}")
            active6b = state._find_active_pipeline_run(r6)
            if not active6b:
                failures.append("case6: active pipeline missing after resume")
            elif active6b.get("phase") not in (
                    "parallel", "release-gate", "done"):
                failures.append(
                    f"case6: expected phase parallel/release-gate/done, "
                    f"got {active6b.get('phase')}")
            if _drafts(r6):
                failures.append(
                    "case6: drafts should be empty (auto-approved low-risk)")
        shutil.rmtree(r6, ignore_errors=True)

        # Case 6b: --auto-approve-low-risk halts on medium+
        r6b = fresh_repo()
        prd6b = _write_prd(r6b, "prd.md", _PRD)
        cmd_pipeline(ns_run(r6b, prd6b))  # halt at approve-gate
        drafts6b = _drafts(r6b)
        for iid in drafts6b:
            ipath = os.path.join(r6b, ".harness", "issues", f"{iid}.md")
            with open(ipath, "r", encoding="utf-8") as f:
                t = f.read()
            t = re.sub(r"Risk Level:\s*\w+", "Risk Level: medium", t)
            with open(ipath, "w", encoding="utf-8") as f:
                f.write(t)
        active6b = state._find_active_pipeline_run(r6b)
        if active6b:
            _finalize_pipeline_log(active6b, "cancelled", r6b)
        tasks6b = state._load_tasks(r6b)
        for iid in list(tasks6b.keys()):
            tasks6b[iid]["status"] = "draft"
        state._save_tasks(tasks6b, target=r6b)
        q6b = state._load_queue(r6b)
        q6b["approved"] = []
        q6b["draft"] = list(tasks6b.keys())
        state._save_queue(q6b, target=r6b)
        rc6b3 = cmd_pipeline(
            ns_run(r6b, prd6b, auto_approve_low_risk=True))
        if rc6b3 != 0:
            failures.append(f"case6b halt should exit 0, got {rc6b3}")
        active6b3 = state._find_active_pipeline_run(r6b)
        if not active6b3:
            failures.append("case6b: active pipeline missing")
        elif active6b3.get("phase") != "approve-gate":
            failures.append(
                f"case6b: expected phase approve-gate (medium+ halt), "
                f"got {active6b3.get('phase')}")
        if not _drafts(r6b):
            failures.append(
                "case6b: medium-risk draft should NOT be auto-approved")
        shutil.rmtree(r6b, ignore_errors=True)

        # Case 7: intake-failed (missing PRD on fresh run)
        r7 = fresh_repo()
        rc7 = cmd_pipeline(
            ns_run(r7, os.path.join(r7, "nonexistent.md")))
        if rc7 != 2:
            failures.append(f"case7 missing-PRD should exit 2, got {rc7}")
        active7 = state._find_active_pipeline_run(r7)
        if active7 is not None:
            failures.append("case7: no pipeline log expected for missing PRD")
        shutil.rmtree(r7, ignore_errors=True)

        # Case 8: status characterization
        r8 = fresh_repo()
        status_text = state._format_status(r8)
        if "Pipeline:" in status_text:
            failures.append(
                "case8: status should NOT contain Pipeline block when no "
                "active pipeline")
        prd8 = _write_prd(r8, "prd.md", _PRD)
        cmd_pipeline(ns_run(r8, prd8))
        status_text2 = state._format_status(r8)
        if "Pipeline:" not in status_text2:
            failures.append("case8: status should contain Pipeline block when "
                            "active pipeline exists")
        if "phase: approve-gate" not in status_text2:
            failures.append("case8: status Pipeline block missing phase line")
        shutil.rmtree(r8, ignore_errors=True)
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
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("pipeline selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "selftest":
        return selftest()

    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Laplace pipeline: checkpoint orchestrator over "
                    "intake/verify/approve/parallel/release (ISSUE-0005).",
    )
    parser.add_argument("prd", nargs="?", default=None,
                        help="Path to the source PRD markdown file "
                             "(required for a fresh pipeline; optional with --resume)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume the active pipeline from its recorded phase")
    parser.add_argument("--release", default=None,
                        help="Release version X.Y.Z to publish at the release-gate")
    parser.add_argument("--auto-approve-low-risk", action="store_true",
                        help="Auto-approve risk.level==low drafts at the "
                             "approve-gate; halt for medium+")
    parser.add_argument("--max-parallel", type=int, default=None,
                        help="Concurrency cap for the parallel phase "
                             "(default: .harness/config.yml max_parallel)")
    parser.add_argument("--force-verify", action="store_true",
                        help="Escape hatch: proceed past a verify FAIL verdict")
    _add_target_arg(parser)
    args = parser.parse_args(argv)
    return cmd_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
