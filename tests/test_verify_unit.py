"""Unit tests for verify.py — one per selftest case A-L + parametrized helpers."""
import argparse
import os
import shutil
import tempfile

import pytest

import state
import verify
from verify import (
    _normalize_heading,
    _significant_tokens,
    _parse_lines_field,
    check_duplicate_ac,
    cmd_verify,
)


# --- parametrized helper tests ---------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("3-19", (3, 19)),
    (" 3 - 19 ", (3, 19)),
    ("10-10", (10, 10)),
    ("0-9", None),
    ("9-3", None),
    ("abc", None),
    ("", None),
    (None, None),
    ("3", None),
])
def test_parse_lines_field(raw, expected):
    assert _parse_lines_field(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Task: User Login", "user login"),
    ("Feature:  Foo ", "foo"),
    ("task: bar:", "bar"),
    ("Requirement Bar Baz", "bar baz"),
    ("", ""),
    ("ISSUE: Title", "title"),
])
def test_normalize_heading(raw, expected):
    assert _normalize_heading(raw) == expected


def test_significant_tokens_strips_stoplist():
    toks = _significant_tokens(
        "The quick brown fox must satisfy acceptance criteria"
    )
    assert "quick" in toks
    assert "brown" in toks
    assert "acceptance" not in toks
    assert "criteria" not in toks
    assert "must" not in toks
    assert "the" not in toks


def test_significant_tokens_short_tokens_excluded():
    toks = _significant_tokens("cat dog hi xx")
    assert toks == set()  # all < 4 chars


# --- duplicate threshold boundary ------------------------------------------

def _issue_with_ac(issue_id, ac_list):
    return {"issue_id": issue_id, "ac_list": ac_list}


def test_duplicate_ac_threshold_at_0_8_no_warn():
    # Two AC sets that share exactly enough to hit 0.80 -> not > 0.80, no warn.
    a = {"alpha", "bravo", "charlie", "delta", "echo"}
    b = {"alpha", "bravo", "charlie", "delta", "foxtrot"}
    # Jaccard = 4/6 = 0.667
    issues = [
        _issue_with_ac("ISSUE-0001", [" ".join(a)]),
        _issue_with_ac("ISSUE-0002", [" ".join(b)]),
    ]
    assert check_duplicate_ac(issues) == []


def test_duplicate_ac_threshold_above_0_81_warns():
    a = {"alpha", "bravo", "charlie", "delta", "echo"}
    b = {"alpha", "bravo", "charlie", "delta", "echo", "foxtrot"}
    # Jaccard = 5/6 = 0.833 > 0.8
    issues = [
        _issue_with_ac("ISSUE-0001", [" ".join(a)]),
        _issue_with_ac("ISSUE-0002", [" ".join(b)]),
    ]
    results = check_duplicate_ac(issues)
    assert len(results) == 1
    assert results[0][0] == "warn"
    assert results[0][1] == "AC-VRF-006"


# --- selftest case fixtures (A-L) -------------------------------------------

_CLEAN_PRD = """# Widget PRD

## Task: User Login

Users need to log in via OAuth. This is core to onboarding.

### Scope

**In Scope:**
- Login page
- Token refresh

**Out of Scope:**
- SSO

### Acceptance Criteria
- Given valid creds, return 200
- Given invalid creds, return 401

## Task: Audit Log

Audit log must persist for 90 days for compliance.

### Acceptance Criteria
- Audit entries persist for at least 90 days
"""


def _issue_md(issue_id="ISSUE-0001", *, summary="Task: User Login",
              background="Users need to log in via OAuth. This is core to onboarding.",
              scope_in="Login page ; Token refresh",
              scope_out="SSO",
              ac_list=None,
              depends_on=None,
              document="prd.md",
              section="Task: User Login",
              lines="3-19",
              status="draft"):
    if ac_list is None:
        ac_list = ["Given valid creds, return 200", "Given invalid creds, return 401"]
    deps = ", ".join(depends_on) if depends_on else "(none)"
    ac_block = "\n".join(f"- {a}" for a in ac_list)
    return (
        f"# {issue_id}: {summary}\n\n"
        f"**Issue ID**: {issue_id}\n"
        f"**Status**: {status}\n"
        f"**Summary**: {summary}\n\n"
        f"## Background\n{background}\n\n"
        f"## Dependencies\n- depends_on: {deps}\n\n"
        f"## Scope\n**In Scope:**\n- {scope_in}\n"
        f"**Out of Scope:**\n- {scope_out}\n\n"
        f"## Acceptance Criteria\n{ac_block}\n\n"
        f"## Technical Notes\nTBD\n\n"
        f"## Test Requirements\n- Unit: TBD\n\n"
        f"## Risk / Release Impact\n- Risk Level: medium\n\n"
        f"## Routing Metadata\n- Area: auth\n\n"
        f"## Source\n"
        f"- Document: {document}\n"
        f"- Section: {section}\n"
        f"- Lines: {lines}\n"
        f"- Excerpt: ...\n\n"
        f"## Run History\n[]\n"
    )


@pytest.fixture
def repo(tmp_path):
    """Init a harness repo; return (repo_root, prd_path, issues_dir)."""
    root = str(tmp_path)
    assert state.cmd_init(target=root) == 0
    prd_path = os.path.join(root, "prd.md")
    with open(prd_path, "w") as f:
        f.write(_CLEAN_PRD)
    issues_dir = os.path.join(root, ".harness", "issues")
    os.makedirs(issues_dir, exist_ok=True)
    return root, prd_path, issues_dir


def _write(issues_dir, issue_id, body):
    with open(os.path.join(issues_dir, f"{issue_id}.md"), "w") as f:
        f.write(body)


def _run(repo, prd_path):
    return cmd_verify(argparse.Namespace(prd_path=prd_path, target=repo))


def _harness_mtimes(repo):
    snap = {}
    for dirpath, _dirs, files in os.walk(os.path.join(repo, ".harness")):
        for name in files:
            p = os.path.join(dirpath, name)
            snap[p] = os.path.getmtime(p)
    return snap


# Case A — clean
def test_case_a_clean(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001"))
    _write(issues_dir, "ISSUE-0002",
           _issue_md("ISSUE-0002", summary="Task: Audit Log",
                     background="Audit log must persist for 90 days for compliance.",
                     scope_in="Audit entries persist", scope_out="Nothing",
                     ac_list=["Audit entries persist for at least 90 days"],
                     section="Task: Audit Log", lines="21-30"))
    assert _run(root, prd_path) == 0


# Case B — TBD field
def test_case_b_tbd_field(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001", summary="TBD"))
    assert _run(root, prd_path) == 0  # warn-only


# Case C — out-of-scope TBD
def test_case_c_scope_out_tbd(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001", scope_out="TBD"))
    assert _run(root, prd_path) == 0


# Case D — bad section (fail)
def test_case_d_bad_section(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", section="Task: Does Not Exist"))
    assert _run(root, prd_path) == 1


# Case E — bad lines (warn)
def test_case_e_bad_lines(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001", lines="999-9999"))
    assert _run(root, prd_path) == 0


# Case F — orphan section (warn)
def test_case_f_orphan_section(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001"))
    # Only one issue; PRD has 2 sections -> Audit Log orphan.
    assert _run(root, prd_path) == 0


# Case G — orphan issue (warn)
def test_case_g_orphan_issue(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", document="other-prd.md"))
    assert _run(root, prd_path) == 0


# Case H — bad dep ref (fail)
def test_case_h_bad_dep_ref(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", depends_on=["ISSUE-9999"]))
    assert _run(root, prd_path) == 1


# Case I — duplicate AC (warn)
def test_case_i_duplicate_ac(repo):
    root, prd_path, issues_dir = repo
    dup = ["Given valid creds, return 200 status code",
           "Given invalid creds, return 401 status code"]
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", ac_list=dup, section="Task: User Login",
                     lines="3-19"))
    _write(issues_dir, "ISSUE-0002",
           _issue_md("ISSUE-0002", summary="Task: Audit Log",
                     background="Audit log must persist for 90 days.",
                     scope_in="Audit entries",
                     ac_list=dup, section="Task: Audit Log", lines="21-30"))
    assert _run(root, prd_path) == 0  # warn-only


# Case J — zero AC overlap (warn)
def test_case_j_zero_ac_overlap(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", section="Task: User Login",
                     ac_list=["Quantum entanglement detector returns true"],
                     lines="3-19"))
    assert _run(root, prd_path) == 0


# Case K — read-only mtime check
def test_case_k_read_only(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001", _issue_md("ISSUE-0001", section="Task: User Login"))
    _write(issues_dir, "ISSUE-0002",
           _issue_md("ISSUE-0002", summary="Task: Audit Log",
                     background="Audit log must persist for 90 days for compliance.",
                     scope_in="Audit entries persist", scope_out="Nothing",
                     ac_list=["Audit entries persist for at least 90 days"],
                     section="Task: Audit Log", lines="21-30"))
    before = _harness_mtimes(root)
    _run(root, prd_path)
    after = _harness_mtimes(root)
    for p, t in before.items():
        assert after.get(p) == t, f"read-only violated: {p} mtime changed"


# Case L — non-draft skipped
def test_case_l_non_draft_skipped(repo):
    root, prd_path, issues_dir = repo
    _write(issues_dir, "ISSUE-0001",
           _issue_md("ISSUE-0001", status="approved",
                     section="Task: Does Not Exist"))
    assert _run(root, prd_path) == 0


# --- read-only imports (no state mutation primitive used) ------------------

def test_verify_does_not_import_state_mutation():
    """verify.py must NOT import any state-mutation primitive."""
    import inspect
    src = inspect.getsource(verify)
    forbidden = [
        "_save_tasks", "_save_queue", "_set_issue_state",
        "_atomic_write_text", "acquire_lock", "release_lock",
        "cmd_approve", "cmd_transition", "cmd_run_start",
    ]
    for name in forbidden:
        assert name not in src, f"verify.py references forbidden state-mutation primitive: {name}"
