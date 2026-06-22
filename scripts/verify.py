#!/usr/bin/env python3
"""Laplace verify: read-only pre-approve quality gate.

Responsibilities (ISSUE-0001, PRD docs/prd-verify-gate.md):
  - Load a source PRD + all draft issues whose Source.Document matches it
  - Run a suite of read-only checks (field, source, coverage, AC trace,
    depends_on, duplicate AC) and print a per-issue + coverage report
  - Re-use intake parsing primitives (`_split_sections`) and state helpers
    (`_load_tasks`, `_issues_dir`) — does not duplicate the parser

stdlib-only. Read-only: imports NO state-mutation primitive from state.py.
"""

import argparse
import glob
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer-module reuse (do not duplicate the parser).
import intake  # noqa: E402
from intake import _split_sections, _HEADING_KEYWORDS, _strip_bullet  # noqa: E402
import state  # noqa: E402
from state import _load_tasks, _issues_dir, _harness_root, _read_json  # noqa: E402

# Significant-token stoplist (PRD/issue boilerplate words that should not
# count toward AC traceability overlap).
_STOPLIST = {
    "the", "a", "and", "of", "to", "in", "is", "for", "with", "that", "this",
    "scope", "criteria", "acceptance", "must", "should", "will",
}

# Duplicate-AC Jaccard threshold (AC-VRF-006). Hardcoded per PRD R-3 / v1.
_DUP_THRESHOLD = 0.8

# Check result levels + codes.
Level = str  # "warn" | "fail"
CheckResult = Tuple[Level, str, str]  # (level, code, message)


# ---------------------------------------------------------------------------
# Normalization + tokenization helpers
# ---------------------------------------------------------------------------

def _normalize_heading(s: str) -> str:
    """Normalize a heading for cross-document matching.

    Lowercase, strip, collapse internal whitespace, strip trailing punctuation
    (`:` and similar), and strip a leading keyword prefix (feature/task/
    requirement/story/epic/issue). PRD R-2: match on normalized text so a
    rename/typo between PRD heading and issue Source.Section does not create
    a false orphan.
    """
    if s is None:
        return ""
    out = re.sub(r"\s+", " ", str(s).strip().lower())
    out = out.rstrip(":.;,")
    for kw in _HEADING_KEYWORDS:
        # `feature:`, `feature `, or `feature:` after we already stripped ws.
        if out == kw:
            return ""
        if out.startswith(kw + ":"):
            out = out[len(kw) + 1:].strip()
            break
        if out.startswith(kw + " "):
            out = out[len(kw) + 1:].strip()
            break
    out = out.rstrip(":.;,")
    return out.strip()


def _significant_tokens(text: str) -> set:
    """Return the set of significant alphanumeric tokens in `text`.

    Tokens are lowercase runs of >=4 alphanumerics, minus the boilerplate
    stoplist. Used for AC traceability (AC-VRF-005) and duplicate-AC Jaccard
    (AC-VRF-006).
    """
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]{4,}", text.lower())
    return {t for t in tokens if t not in _STOPLIST}


def _parse_lines_field(s: str) -> Optional[Tuple[int, int]]:
    """Parse a `Source.Lines` value into a (start, end) 1-based inclusive tuple.

    Accepts `"X-Y"` (whitespace-tolerant). Returns None on invalid or
    inverted ranges.
    """
    if s is None:
        return None
    raw = str(s).strip()
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", raw)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a <= 0 or b <= 0 or a > b:
        return None
    return (a, b)


# ---------------------------------------------------------------------------
# Issue markdown parsing (read-only; does not import intake render path)
# ---------------------------------------------------------------------------

def _section_block(content: str, header: str) -> str:
    """Return the body text under a `## <header>` heading.

    Body ends at the next `## ` heading or end of file.
    """
    lines = content.splitlines()
    out: List[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            if line[3:].strip().lower() == header.lower():
                in_section = True
                continue
        elif in_section:
            out.append(line)
    return "\n".join(out)


def _parse_issue_md(path: str) -> Dict[str, Any]:
    """Parse a draft-issue `.md` into a dict of the fields verify needs.

    Keys: summary, background, scope_in, scope_out, ac_list, source
    (document, section, lines, excerpt), depends_on (list), status.
    Missing fields default to "" / [] / "TBD" — verify checks catch them.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    def _field(label: str) -> str:
        m = re.search(r"^\*\*" + re.escape(label) + r"\*\*:\s*(.*)$", content, re.MULTILINE)
        return m.group(1).strip() if m else ""

    summary = _field("Summary")
    status = _field("Status")
    background = _section_block(content, "Background").strip()

    scope_block = _section_block(content, "Scope")
    scope_in = ""
    scope_out = ""
    in_in = False
    in_out = False
    for line in scope_block.splitlines():
        s = line.strip()
        if re.match(r"^\*{0,2}\s*In Scope\s*:?\s*\*{0,2}\s*$", s, re.IGNORECASE):
            in_in = True
            in_out = False
            continue
        if re.match(r"^\*{0,2}\s*Out of Scope\s*:?\s*\*{0,2}\s*$", s, re.IGNORECASE):
            in_out = True
            in_in = False
            continue
        bullet = _strip_bullet(line)
        if not bullet:
            continue
        if in_in:
            scope_in = bullet if not scope_in else scope_in + " ; " + bullet
        elif in_out:
            scope_out = bullet if not scope_out else scope_out + " ; " + bullet

    ac_block = _section_block(content, "Acceptance Criteria")
    ac_list: List[str] = []
    for line in ac_block.splitlines():
        b = _strip_bullet(line)
        if b:
            ac_list.append(b)

    deps_block = _section_block(content, "Dependencies")
    depends_on: List[str] = []
    m = re.search(r"(?im)^\s*-\s*depends_on:\s*(.*)$", deps_block)
    if m:
        raw = m.group(1).strip()
        if raw and raw.lower() != "(none)":
            for tok in re.split(r"[,\s]+", raw):
                if re.match(r"^ISSUE-\d{4}$", tok):
                    depends_on.append(tok)

    source_block = _section_block(content, "Source")
    src: Dict[str, str] = {"document": "", "section": "", "lines": "", "excerpt": ""}

    def _src_field(label: str) -> str:
        sm = re.search(
            r"(?im)^\s*-\s*" + re.escape(label) + r"\s*:\s*(.*)$", source_block
        )
        return sm.group(1).strip() if sm else ""

    src["document"] = _src_field("Document")
    src["section"] = _src_field("Section")
    src["lines"] = _src_field("Lines")
    src["excerpt"] = _src_field("Excerpt")

    return {
        "issue_id": _field("Issue ID"),
        "status": status,
        "summary": summary,
        "background": background,
        "scope_in": scope_in,
        "scope_out": scope_out,
        "ac_list": ac_list,
        "depends_on": depends_on,
        "source": src,
        "path": path,
    }


# ---------------------------------------------------------------------------
# Checks (all read-only; each returns list[(level, code, msg)])
# ---------------------------------------------------------------------------

def _is_tbd_or_empty(s: str) -> bool:
    if s is None:
        return True
    s = s.strip()
    if not s:
        return True
    return s.lower() == "tbd"


def check_field_completeness(issue: Dict[str, Any]) -> List[CheckResult]:
    """AC-VRF-002 — required fields non-TBD non-empty (warn)."""
    out: List[CheckResult] = []
    fields = {
        "Summary": issue.get("summary", ""),
        "Background": issue.get("background", ""),
        "Scope In": issue.get("scope_in", ""),
        "Scope Out": issue.get("scope_out", ""),
    }
    for label, val in fields.items():
        if _is_tbd_or_empty(val):
            out.append(("warn", "AC-VRF-002", f"{label} is TBD or empty"))
    if not issue.get("ac_list"):
        out.append(("warn", "AC-VRF-002", "Acceptance Criteria empty"))
    else:
        for i, ac in enumerate(issue["ac_list"]):
            if _is_tbd_or_empty(ac):
                out.append(("warn", "AC-VRF-002",
                            f"Acceptance Criteria bullet {i + 1} is TBD or empty"))
                break
    return out


def check_source_traceability(issue: Dict[str, Any], prd_text: str,
                              prd_sections: List[Tuple[str, str, int, int]]
                              ) -> List[CheckResult]:
    """AC-VRF-003 — Source.Section must exist in PRD (fail); lines in bounds
    (warn)."""
    out: List[CheckResult] = []
    src = issue.get("source", {})
    section = src.get("section", "")
    if not section:
        out.append(("fail", "AC-VRF-003", "Source.Section missing"))
        return out

    norm_target = _normalize_heading(section)
    matched = None
    for (title, _body, _s, _e) in prd_sections:
        if _normalize_heading(title) == norm_target:
            matched = (title, _body, _s, _e)
            break
    if matched is None:
        out.append(("fail", "AC-VRF-003",
                    f"Source.Section {section!r} not found in PRD"))
        # Cannot check lines if section missing.
        return out

    lines = _parse_lines_field(src.get("lines", ""))
    if src.get("lines") and lines is None:
        out.append(("warn", "AC-VRF-003",
                    f"Source.Lines {src.get('lines')!r} is malformed"))
        return out
    if lines is None:
        return out
    n = len(prd_text.splitlines())
    a, b = lines
    if b > n:
        out.append(("warn", "AC-VRF-003",
                    f"Source.Lines {a}-{b} end exceeds PRD length ({n})"))
    elif a > n:
        out.append(("warn", "AC-VRF-003",
                    f"Source.Lines {a}-{b} start exceeds PRD length ({n})"))
    return out


def check_coverage(issues: List[Dict[str, Any]],
                   prd_sections: List[Tuple[str, str, int, int]],
                   prd_path: str) -> List[CheckResult]:
    """AC-VRF-004 — orphan PRD section (warn); orphan issue (warn).

    Note: orphan-section and orphan-issue are reported in the per-PRD coverage
    matrix (not per-issue). This function returns aggregate AC-VRF-004 entries
    so callers can surface them in the cross-issue table too.
    """
    out: List[CheckResult] = []
    # Normalize which issue sections are claimed, scoped to this PRD.
    try:
        prd_rel = os.path.relpath(os.path.abspath(prd_path), _harness_root())
    except ValueError:
        prd_rel = os.path.abspath(prd_path)
    prd_norm_targets = {
        os.path.normpath(p) for p in (prd_path, prd_rel, os.path.abspath(prd_path))
    }
    claimed_norm = set()
    for iss in issues:
        doc = iss.get("source", {}).get("document", "")
        if doc and os.path.normpath(doc) in prd_norm_targets:
            claimed_norm.add(_normalize_heading(iss["source"].get("section", "")))

    for (title, _body, _s, _e) in prd_sections:
        if _normalize_heading(title) not in claimed_norm:
            out.append(("warn", "AC-VRF-004",
                        f"PRD section {title!r} has no matching draft issue (orphan)"))

    for iss in issues:
        doc = iss.get("source", {}).get("document", "")
        if not doc:
            continue
        if os.path.normpath(doc) not in prd_norm_targets:
            out.append(("warn", "AC-VRF-004",
                        f"{iss['issue_id']} Source.Document {doc!r} != verified PRD "
                        f"(orphan issue)"))
    return out


def _matched_prd_section(issue: Dict[str, Any],
                         prd_sections: List[Tuple[str, str, int, int]]
                         ) -> Optional[Tuple[str, str, int, int]]:
    src = issue.get("source", {})
    norm_target = _normalize_heading(src.get("section", ""))
    if not norm_target:
        return None
    for sec in prd_sections:
        if _normalize_heading(sec[0]) == norm_target:
            return sec
    return None


def check_ac_traceability(issue: Dict[str, Any],
                          prd_sections: List[Tuple[str, str, int, int]]
                          ) -> List[CheckResult]:
    """AC-VRF-005 — AC bullets share >=1 significant token with the matched
    PRD section body (warn)."""
    out: List[CheckResult] = []
    ac_list = issue.get("ac_list", [])
    if not ac_list:
        return out
    matched = _matched_prd_section(issue, prd_sections)
    if matched is None:
        return out
    body_tokens = _significant_tokens(matched[1])
    if not body_tokens:
        return out
    ac_tokens = set()
    for ac in ac_list:
        ac_tokens |= _significant_tokens(ac)
    if ac_tokens & body_tokens:
        return out
    out.append(("warn", "AC-VRF-005",
                f"AC bullets share no significant token with PRD section "
                f"{matched[0]!r} body (traceability gap)"))
    return out


def check_depends_on(issue: Dict[str, Any], all_issue_ids: set) -> List[CheckResult]:
    """AC-VRF-005 — depends_on refs to non-existent issues (fail)."""
    out: List[CheckResult] = []
    for dep in issue.get("depends_on", []):
        if dep not in all_issue_ids:
            out.append(("fail", "AC-VRF-005",
                        f"depends_on {dep!r} does not match any issue id"))
    return out


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def check_duplicate_ac(issues: List[Dict[str, Any]]) -> List[CheckResult]:
    """AC-VRF-006 — pairwise Jaccard > threshold across AC token sets (warn)."""
    out: List[CheckResult] = []
    prepared = []
    for iss in issues:
        ac_tokens: set = set()
        for ac in iss.get("ac_list", []):
            ac_tokens |= _significant_tokens(ac)
        prepared.append((iss.get("issue_id", "?"), ac_tokens))
    seen_pairs = set()
    for i in range(len(prepared)):
        id_i, toks_i = prepared[i]
        if not toks_i:
            continue
        for j in range(i + 1, len(prepared)):
            id_j, toks_j = prepared[j]
            if not toks_j:
                continue
            ratio = _jaccard(toks_i, toks_j)
            if ratio > _DUP_THRESHOLD:
                pair_key = tuple(sorted((id_i, id_j)))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                out.append(("warn", "AC-VRF-006",
                            f"AC bullets of {id_i} and {id_j} overlap "
                            f"{ratio:.2f} (> {_DUP_THRESHOLD:.2f}) — duplicate AC"))
    return out


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _verdict(per_issue: Dict[str, List[CheckResult]],
             cross: List[CheckResult]) -> Tuple[str, int, int]:
    """Return (verdict_label, warn_count, fail_count)."""
    fails = 0
    warns = 0
    for results in per_issue.values():
        for (lvl, _code, _msg) in results:
            if lvl == "fail":
                fails += 1
            elif lvl == "warn":
                warns += 1
    for (lvl, _code, _msg) in cross:
        if lvl == "fail":
            fails += 1
        elif lvl == "warn":
            warns += 1
    if fails > 0:
        return ("FAIL", warns, fails)
    if warns > 0:
        return ("WARN", warns, fails)
    return ("PASS", warns, fails)


def render_report(prd_path: str,
                  prd_sections: List[Tuple[str, str, int, int]],
                  issues: List[Dict[str, Any]],
                  per_issue: Dict[str, List[CheckResult]],
                  cross: List[CheckResult]) -> str:
    """Render the verify report (per-issue + PRD coverage + cross-issue +
    verdict)."""
    lines: List[str] = []
    lines.append(f"Laplace verify — {prd_path}")
    lines.append("")

    # Per-issue table.
    lines.append("Per-issue:")
    if issues:
        for iss in issues:
            cid = iss.get("issue_id", "?")
            results = per_issue.get(cid, [])
            fails = [r for r in results if r[0] == "fail"]
            warns = [r for r in results if r[0] == "warn"]
            if fails:
                label = "FAIL"
            elif warns:
                label = "WARN"
            else:
                label = "PASS"
            lines.append(f"  {cid}: {label}")
            for (lvl, code, msg) in results:
                lines.append(f"    - [{lvl}] {code}: {msg}")
    else:
        lines.append("  (no draft issues)")
    lines.append("")

    # PRD coverage matrix.
    lines.append("PRD coverage:")
    if prd_sections:
        try:
            prd_rel = os.path.relpath(os.path.abspath(prd_path), _harness_root())
        except ValueError:
            prd_rel = os.path.abspath(prd_path)
        prd_norm_targets = {
            os.path.normpath(p) for p in (prd_path, prd_rel, os.path.abspath(prd_path))
        }
        # section -> [issue_ids]
        section_to_issues: Dict[str, List[str]] = {}
        orphan_issues: List[str] = []
        for iss in issues:
            doc = iss.get("source", {}).get("document", "")
            sec = iss.get("source", {}).get("section", "")
            if doc and os.path.normpath(doc) not in prd_norm_targets:
                orphan_issues.append(iss.get("issue_id", "?"))
                continue
            key = _normalize_heading(sec)
            section_to_issues.setdefault(key, []).append(iss.get("issue_id", "?"))
        for (title, _body, s, e) in prd_sections:
            key = _normalize_heading(title)
            owners = section_to_issues.get(key, [])
            if owners:
                lines.append(f"  - {title} (lines {s + 1}-{e + 1}) <- {', '.join(owners)}")
            else:
                lines.append(f"  - {title} (lines {s + 1}-{e + 1}) <- ORPHAN")
        for cid in orphan_issues:
            lines.append(f"  - {cid} -> ORPHAN (Source.Document != this PRD)")
    else:
        lines.append("  (no ## <Keyword>: task sections found in PRD)")
    lines.append("")

    # Cross-issue (deps + duplicates).
    lines.append("Cross-issue:")
    if cross:
        for (lvl, code, msg) in cross:
            lines.append(f"  - [{lvl}] {code}: {msg}")
    else:
        lines.append("  (none)")
    lines.append("")

    label, warns, fails = _verdict(per_issue, cross)
    lines.append(f"Verdict: {label} ({fails} fail, {warns} warn)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core command
# ---------------------------------------------------------------------------

def cmd_verify(args: Any) -> int:
    """Read-only verify pass. Exit 0 clean / 1 any fail / 2 usage."""
    prd_path: str = args.prd_path
    target: Optional[str] = getattr(args, "target", None)

    root = _harness_root(target)
    if not os.path.isdir(os.path.join(root, ".harness")):
        print(f"Laplace is not initialized at {root}. Run /laplace:init first.",
              file=sys.stderr)
        return 2
    if not os.path.isfile(prd_path):
        print(f"PRD not found: {prd_path}", file=sys.stderr)
        return 2

    with open(prd_path, "r", encoding="utf-8") as f:
        prd_text = f.read()
    prd_sections = _split_sections(prd_text)

    # Load all issue ids (for depends_on checks) — read-only.
    tasks = _load_tasks(target)
    all_issue_ids = set(tasks.keys())

    # Parse every draft ISSUE-*.md in the issues dir.
    issues: List[Dict[str, Any]] = []
    issues_glob = sorted(glob.glob(os.path.join(_issues_dir(target), "ISSUE-*.md")))
    for path in issues_glob:
        try:
            parsed = _parse_issue_md(path)
        except Exception as exc:  # parse must never crash verify
            print(f"WARN: failed to parse {path}: {exc}", file=sys.stderr)
            continue
        # Non-draft issues are out of scope (PRD Non-goals).
        if parsed.get("status", "draft") != "draft":
            continue
        issues.append(parsed)

    # Per-issue checks.
    per_issue: Dict[str, List[CheckResult]] = {}
    for iss in issues:
        results: List[CheckResult] = []
        results += check_field_completeness(iss)
        results += check_source_traceability(iss, prd_text, prd_sections)
        results += check_ac_traceability(iss, prd_sections)
        results += check_depends_on(iss, all_issue_ids)
        per_issue[iss.get("issue_id", "?")] = results

    # Cross-issue checks.
    cross: List[CheckResult] = []
    cross += check_coverage(issues, prd_sections, prd_path)
    cross += check_duplicate_ac(issues)

    report = render_report(prd_path, prd_sections, issues, per_issue, cross)
    print(report)

    _label, _warns, fails = _verdict(per_issue, cross)
    return 1 if fails > 0 else 0


# ---------------------------------------------------------------------------
# selftest — temp repo, write fake PRD + fake ISSUE-*.md DIRECTLY (no intake)
# ---------------------------------------------------------------------------

def _write_fake_prd(repo: str, name: str, text: str) -> str:
    p = os.path.join(repo, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def _write_fake_issue(repo: str, issue_id: str, body: str) -> str:
    p = os.path.join(repo, ".harness", "issues", f"{issue_id}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


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


def _issue_md(issue_id: str, *, summary: str = "Task: User Login",
              background: str = "Users need to log in via OAuth. This is core to onboarding.",
              scope_in: str = "Login page ; Token refresh",
              scope_out: str = "SSO",
              ac_list: Optional[List[str]] = None,
              depends_on: Optional[List[str]] = None,
              document: str = "prd.md",
              section: str = "Task: User Login",
              lines: str = "3-19",
              status: str = "draft") -> str:
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


def selftest() -> int:
    import shutil
    import tempfile

    failures: List[str] = []
    tmp_repo = tempfile.mkdtemp(prefix="laplace-verify-selftest-")
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        if state.cmd_init(target=tmp_repo) != 0:
            failures.append("state.cmd_init returned non-zero")
            raise RuntimeError("init failed")

        # Snapshot .harness mtimes for read-only check (AC-VRF-007).
        def _snapshot() -> Dict[str, float]:
            snap: Dict[str, float] = {}
            for dirpath, _dirs, files in os.walk(os.path.join(tmp_repo, ".harness")):
                for name in files:
                    p = os.path.join(dirpath, name)
                    snap[p] = os.path.getmtime(p)
            return snap

        def _seed_issues_dir() -> str:
            return os.path.join(tmp_repo, ".harness", "issues")

        # --- Case A: clean — everything populated, 1:1 with PRD ----------
        os.makedirs(_seed_issues_dir(), exist_ok=True)
        prd_a = _write_fake_prd(tmp_repo, "prd.md", _CLEAN_PRD)
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001",
                                    summary="Task: User Login",
                                    section="Task: User Login",
                                    document="prd.md",
                                    lines="3-19"))
        _write_fake_issue(tmp_repo, "ISSUE-0002",
                          _issue_md("ISSUE-0002",
                                    summary="Task: Audit Log",
                                    background="Audit log must persist for 90 days for compliance.",
                                    scope_in="Audit entries persist",
                                    scope_out="Nothing",
                                    ac_list=["Audit entries persist for at least 90 days"],
                                    section="Task: Audit Log",
                                    document="prd.md",
                                    lines="21-30"))
        before = _snapshot()
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        after = _snapshot()
        if rc != 0:
            failures.append(f"Case A clean should exit 0, got {rc}")
        for p, t in before.items():
            if after.get(p) != t:
                failures.append(f"Case A read-only violated: {p} mtime changed")

        # --- Case B: TBD field -----------------------------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", summary="TBD"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case B (warn-only) should exit 0, got {rc}")

        # --- Case C: out-of-scope TBD (Background still real) -----------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", scope_out="TBD"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case C (warn-only) should exit 0, got {rc}")

        # --- Case D: bad section (fail AC-VRF-003) ----------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", section="Task: Does Not Exist"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 1:
            failures.append(f"Case D (bad section) should exit 1, got {rc}")

        # --- Case E: bad lines (warn AC-VRF-003) ------------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", lines="999-9999"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case E (bad lines, warn-only) should exit 0, got {rc}")

        # --- Case F: orphan section (warn AC-VRF-004) -------------------
        # PRD has 2 sections; only ISSUE-0001 covers one -> orphan Audit Log.
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case F (orphan section, warn-only) should exit 0, got {rc}")

        # --- Case G: orphan issue (warn AC-VRF-004) ---------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", document="other-prd.md"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case G (orphan issue, warn-only) should exit 0, got {rc}")

        # --- Case H: bad dep ref (fail AC-VRF-005) ----------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", depends_on=["ISSUE-9999"]))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 1:
            failures.append(f"Case H (bad dep ref) should exit 1, got {rc}")

        # --- Case I: duplicate AC (warn AC-VRF-006) ---------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001",
                                    ac_list=["Given valid creds, return 200 status code",
                                             "Given invalid creds, return 401 status code"],
                                    section="Task: User Login", document="prd.md",
                                    lines="3-19"))
        _write_fake_issue(tmp_repo, "ISSUE-0002",
                          _issue_md("ISSUE-0002",
                                    summary="Task: Audit Log",
                                    section="Task: Audit Log",
                                    background="Audit log must persist for 90 days.",
                                    scope_in="Audit entries",
                                    ac_list=["Given valid creds, return 200 status code",
                                             "Given invalid creds, return 401 status code"],
                                    document="prd.md", lines="21-30"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case I (duplicate AC, warn-only) should exit 0, got {rc}")

        # --- Case J: zero AC overlap (warn AC-VRF-005 traceability) -----
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001",
                                    section="Task: User Login",
                                    ac_list=["Quantum entanglement detector returns true"],
                                    document="prd.md", lines="3-19"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case J (AC trace warn-only) should exit 0, got {rc}")

        # --- Case K: read-only mtime check across full run --------------
        # Re-run Case A fixture (clean) and assert .harness untouched.
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", section="Task: User Login"))
        _write_fake_issue(tmp_repo, "ISSUE-0002",
                          _issue_md("ISSUE-0002", summary="Task: Audit Log",
                                    background="Audit log must persist for 90 days for compliance.",
                                    scope_in="Audit entries persist",
                                    scope_out="Nothing",
                                    ac_list=["Audit entries persist for at least 90 days"],
                                    section="Task: Audit Log", document="prd.md",
                                    lines="21-30"))
        before = _snapshot()
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        cmd_verify(ns)
        after = _snapshot()
        for p, t in before.items():
            if after.get(p) != t:
                failures.append(f"Case K read-only violated: {p} mtime changed")

        # --- Case L: non-draft skipped ----------------------------------
        shutil.rmtree(_seed_issues_dir())
        os.makedirs(_seed_issues_dir())
        _write_fake_issue(tmp_repo, "ISSUE-0001",
                          _issue_md("ISSUE-0001", status="approved",
                                    section="Task: Does Not Exist"))
        ns = argparse.Namespace(prd_path=prd_a, target=tmp_repo)
        rc = cmd_verify(ns)
        if rc != 0:
            failures.append(f"Case L (non-draft skipped, clean) should exit 0, got {rc}")

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
        shutil.rmtree(tmp_repo, ignore_errors=True)

    # Also exercise helper functions directly (parametric sanity).
    for raw, exp in [("3-19", (3, 19)), (" 3 - 19 ", (3, 19)),
                     ("0-9", None), ("9-3", None), ("abc", None)]:
        got = _parse_lines_field(raw)
        if got != exp:
            failures.append(f"_parse_lines_field({raw!r}) = {got}, want {exp}")
    if _normalize_heading("Task: User Login") != "user login":
        failures.append("_normalize_heading(Task: User Login) wrong")
    if _normalize_heading("Feature:  Foo ") != "foo":
        failures.append("_normalize_heading leading keyword strip wrong")
    toks = _significant_tokens("The quick brown fox jumps over acceptance criteria")
    if "acceptance" in toks or "the" in toks or "quick" not in toks:
        failures.append("_significant_tokens stoplist wrong")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("verify selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "selftest":
        return selftest()
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Read-only pre-approve verify gate over draft issues + PRD",
    )
    parser.add_argument("prd_path", help="Path to the source PRD markdown file")
    parser.add_argument("--target", default=None,
                        help="Repository root containing .harness/ (default: CWD)")
    args = parser.parse_args(argv)
    return cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
