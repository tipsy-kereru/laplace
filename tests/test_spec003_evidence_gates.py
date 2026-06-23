"""SPEC-003: type-aware evidence gates on state transitions.

Covers AC-001..AC-007. stdlib only; runs the real `runner.cmd_advance`
against a temp harness so the gate is exercised end-to-end.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import runner  # noqa: E402
import state  # noqa: E402


def _write_issue(target: str, issue_id: str, rtype: str) -> None:
    """Write a minimal issue .md with a Routing Metadata section."""
    body = f"# {issue_id}\n\n## Routing Metadata\n\n- Type: {rtype}\n"
    path = os.path.join(state._issues_dir(target), f"{issue_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def _seed_issue(target: str, issue_id: str, rtype: str, status: str,
                run_id: str = "run-test-1") -> None:
    _write_issue(target, issue_id, rtype)
    state._save_tasks(
        {issue_id: {"status": status, "updated_at": time.time(),
                    "run_id": run_id}},
        target=target,
    )
    # Empty run log so evidence lookups resolve.
    state._atomic_write_json(
        os.path.join(state._runs_dir(target), f"{run_id}.json"),
        {"evidence": [], "transitions": []},
    )


def _add_evidence(target: str, run_id: str, kind: str) -> None:
    path = os.path.join(state._runs_dir(target), f"{run_id}.json")
    run = state._read_json(path, default=None)
    run.setdefault("evidence", []).append({"ts": time.time(), "kind": kind,
                                           "summary": "synthetic"})
    state._atomic_write_json(path, run)


def _advance(target: str, issue_id: str, frm: str, to_: str) -> int:
    ns = argparse.Namespace(issue_id=issue_id, from_state=frm, to_state=to_,
                            summary="", target=target)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        return runner.cmd_advance(ns)
    finally:
        sys.stdout.close()
        sys.stdout = saved_out
        sys.stderr = saved_err


def _fresh_harness() -> str:
    tmp = tempfile.mkdtemp(prefix="laplace-spec003-")
    assert state.cmd_init(target=tmp) == 0
    return tmp


def test_bug_blocks_without_reproduction_evidence():
    """AC-001: bug at pm-review cannot advance without reproduction evidence."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "bug", "pm-review")
    rc = _advance(tmp, "ISSUE-0001", "pm-review", "ready-for-dev")
    assert rc == 4, f"expected gate block (rc=4), got {rc}"
    tasks = state._load_tasks(target=tmp)
    assert tasks["ISSUE-0001"]["status"] == "pm-review"


def test_bug_passes_with_reproduction_evidence():
    """AC-002: bug with reproduction evidence advances."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "bug", "pm-review")
    _add_evidence(tmp, "run-test-1", "reproduction")
    rc = _advance(tmp, "ISSUE-0001", "pm-review", "ready-for-dev")
    assert rc == 0, f"expected advance ok, got {rc}"
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "ready-for-dev"


def test_feature_has_no_extra_gate():
    """AC-003: feature type follows default path; no evidence lookup fires."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "feature", "pm-review")
    rc = _advance(tmp, "ISSUE-0001", "pm-review", "ready-for-dev")
    assert rc == 0, f"feature should advance freely, got {rc}"


def test_ui_blocks_without_visual_evidence():
    """AC-004: ui at review cannot advance to security-review without visual."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "ui", "review")
    rc = _advance(tmp, "ISSUE-0001", "review", "security-review")
    assert rc == 4, f"expected gate block, got {rc}"
    assert state._load_tasks(target=tmp)["ISSUE-0001"]["status"] == "review"


def test_ui_passes_with_visual_evidence():
    """AC-004 positive: ui with visual evidence advances."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "ui", "review")
    _add_evidence(tmp, "run-test-1", "visual")
    rc = _advance(tmp, "ISSUE-0001", "review", "security-review")
    assert rc == 0, f"expected advance ok, got {rc}"


def test_absent_type_defaults_to_feature():
    """AC-005: issue with no Type field behaves as feature (no gate)."""
    tmp = _fresh_harness()
    # Write issue with empty routing section (no Type line).
    path = os.path.join(state._issues_dir(tmp), "ISSUE-0001.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# ISSUE-0001\n\n## Routing Metadata\n\n")
    state._save_tasks({"ISSUE-0001": {"status": "pm-review",
                                      "updated_at": time.time(),
                                      "run_id": "run-test-1"}}, target=tmp)
    state._atomic_write_json(
        os.path.join(state._runs_dir(tmp), "run-test-1.json"),
        {"evidence": [], "transitions": []},
    )
    rc = _advance(tmp, "ISSUE-0001", "pm-review", "ready-for-dev")
    assert rc == 0, f"absent type should behave as feature, got {rc}"


def test_new_type_via_routing_rules_only():
    """AC-006: a type declared only in routing-rules.yml gates without code."""
    tmp = _fresh_harness()
    rules_path = os.path.join(state._harness_root(tmp), ".harness",
                              "routing-rules.yml")
    with open(rules_path, "a", encoding="utf-8") as f:
        f.write("\ndocs:\n    review: [proofread]\n")
    # Inject into evidence_requirements block properly.
    rules = open(rules_path).read()
    # Rewrite to include a docs entry inside evidence_requirements.
    rules = rules.replace(
        "  security: {}\n",
        "  security: {}\n  docs:\n    review: [proofread]\n",
    ) if "evidence_requirements:" in rules else rules + (
        "\nevidence_requirements:\n  docs:\n    review: [proofread]\n")
    # If the template already has the block, the append above duplicated.
    # Cleanest: write a fresh minimal file.
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write(
            "routes: []\n\n"
            "evidence_requirements:\n"
            "  feature: {}\n"
            "  docs:\n"
            "    review: [proofread]\n"
        )
    _seed_issue(tmp, "ISSUE-0001", "docs", "review")
    rc = _advance(tmp, "ISSUE-0001", "review", "security-review")
    assert rc == 4, f"docs type should gate on proofread, got {rc}"
    _add_evidence(tmp, "run-test-1", "proofread")
    rc = _advance(tmp, "ISSUE-0001", "review", "security-review")
    assert rc == 0, f"docs with proofread should advance, got {rc}"


def test_review_passed_gate_unchanged():
    """AC-007 / AC-LP-008 regression: review-passed still requires test evidence
    for every type, including bug."""
    tmp = _fresh_harness()
    _seed_issue(tmp, "ISSUE-0001", "bug", "security-review")
    # bug at security-review with reproduction but NO test evidence.
    _add_evidence(tmp, "run-test-1", "reproduction")
    rc = _advance(tmp, "ISSUE-0001", "security-review", "review-passed")
    assert rc == 4, f"AC-LP-008 must still fire (rc=4), got {rc}"
    _add_evidence(tmp, "run-test-1", "test")
    rc = _advance(tmp, "ISSUE-0001", "security-review", "review-passed")
    assert rc == 0, f"with test evidence should pass, got {rc}"


if __name__ == "__main__":
    # Self-runnable without pytest: stdlib assert runner.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} SPEC-003 tests passed")
