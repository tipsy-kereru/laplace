#!/usr/bin/env python3
"""Laplace intake: PRD/story markdown -> draft issue files.

Responsibilities (P2, SPEC-002 §Local Issue Schema):
  - Parse a PRD/story markdown file
  - Split into work units by top-level (`#` or `##`) feature/task/etc headings
  - Assign sequential ISSUE-NNNN ids (thread-safe via state.py lock helpers)
  - Write `.harness/issues/ISSUE-NNNN.md` with all 13 schema fields (status=draft)
  - Register each issue in tasks.json + queue.json draft array
  - Redact any user-supplied content before persist (G-LP-003)

stdlib-only. Reuses state.py atomic write + lock helpers; does NOT reimplement.
"""

import argparse
import glob
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# state.py is a peer module; reuse its atomic + lock + state helpers.
import state  # noqa: E402

# Lock ID for the ID-allocation critical section (sequential numbering).
_INTAKE_LOCK_ID = "ISSUE-INTAKE"

# Heading prefix keywords that look like work-unit boundaries.
_HEADING_KEYWORDS = ("feature", "task", "requirement", "story", "epic", "issue")

# Type inference keyword map. Order matters: first match wins.
_TYPE_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("bug", ["bug", "fix", "defect", "regression", "crash", "broken"]),
    ("refactor", ["refactor", "cleanup", "clean-up", "restructure", "rewrite"]),
    ("test", ["test", "coverage", "tdd", "spec test"]),
    ("docs", ["doc", "readme", "documentation", "guide", "changelog"]),
    ("chore", ["chore", "dependency", "upgrade", "bump", "config", "ci"]),
    ("security", ["security", "auth", "permission", "vulnerability"]),
]


# ---------------------------------------------------------------------------
# PRD parsing
# ---------------------------------------------------------------------------

def _split_sections(text: str) -> List[Tuple[str, str, int, int]]:
    """Split markdown into (heading, body, start_line, end_line) sections.

    A section boundary is a `#` or `##` heading whose stripped title starts with
    one of _HEADING_KEYWORDS (case-insensitive). If none match, fall back to
    every `##` heading. If no headings at all, return a single section spanning
    the whole document.
    """
    lines = text.splitlines()
    n = len(lines)
    # Collect candidate heading line indexes.
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    headings: List[Tuple[int, int, str]] = []  # (line_idx, level, title)
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level <= 2:
                headings.append((i, level, title))

    if not headings:
        # Single undivided section.
        return [("(untitled)", "\n".join(lines), 0, max(n - 1, 0))]

    # Prefer explicit feature/task/etc headings at level 1-2.
    explicit = [(i, lv, t) for (i, lv, t) in headings
                if any(t.lower().startswith(kw) or t.lower().startswith(kw + ":")
                       for kw in _HEADING_KEYWORDS)]
    if explicit:
        chosen = explicit
    else:
        # Fall back to every level-2 heading.
        l2 = [(i, lv, t) for (i, lv, t) in headings if lv == 2]
        chosen = l2 if l2 else headings

    out: List[Tuple[str, str, int, int]] = []
    for idx, (start, _lv, title) in enumerate(chosen):
        end = chosen[idx + 1][0] - 1 if idx + 1 < len(chosen) else n - 1
        body = "\n".join(lines[start + 1:end + 1]).strip()
        out.append((title, body, start, max(end, start)))
    return out


def _infer_type(title: str, body: str) -> str:
    hay = (title + " " + body).lower()
    for t, kws in _TYPE_KEYWORDS:
        for kw in kws:
            if kw in hay:
                return t
    return "feature"


def _infer_area(title: str) -> str:
    # Heuristic: first whitespace-delimited token after any "Feature:" prefix.
    cleaned = re.sub(r"^(?:feature|task|requirement|story|epic)\s*:\s*", "", title.strip(),
                     flags=re.IGNORECASE)
    token = cleaned.split()[0] if cleaned.split() else ""
    token = re.sub(r"[^A-Za-z0-9_-]", "", token)
    return token or "TBD"


def _extract_background(body: str) -> str:
    # First non-empty paragraph that is not a sub-heading.
    para: List[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if para:
                break
            continue
        if not stripped:
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(para) if para else "TBD"


def _extract_scope(body: str) -> Tuple[str, str]:
    """Return (in_scope, out_of_scope) as semicolon-joined bullets.

    Recognizes both markdown headings (## In Scope) and plain-text labels
    (In Scope: or In Scope on its own line).
    """
    in_re = re.compile(r"(?im)^\s*(?:#{1,6}\s*)?In Scope\s*:?\s*$")
    out_re = re.compile(r"(?im)^\s*(?:#{1,6}\s*)?Out of Scope\s*:?\s*$")
    # A section ends at the next heading or at a recognized peer label.
    stop_re = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:In Scope|Out of Scope|Acceptance Criteria|AC|Acceptance|Technical Notes|Test Requirements|Risk)\s*:?\s*$"
    )

    def _block(start_re: "re.Pattern[str]") -> List[str]:
        m = start_re.search(body)
        if not m:
            return []
        sub = body[m.end():]
        end = stop_re.search(sub)
        chunk = sub[:end.start()] if end else sub
        bullets = []
        for line in chunk.splitlines():
            s = line.strip().lstrip("-*").strip()
            if s:
                bullets.append(s)
        return bullets

    in_scope = _block(in_re) or ["TBD"]
    out_scope = _block(out_re) or ["TBD"]
    return "; ".join(in_scope), "; ".join(out_scope)


def _extract_depends_on(body: str) -> List[str]:
    """Parse a `Depends on:` line into a list of ISSUE-NNNN ids.

    Matches the first `^Depends on:\\s*(.+)$` line (multiline, case-insensitive).
    Splits the RHS on commas and/or whitespace; each token must match
    `^ISSUE-\\d{4}$`. Invalid tokens are dropped. Returns `[]` when absent
    or when no valid tokens remain.
    """
    m = re.search(r"(?im)^Depends on:\s*(.+)$", body)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    tokens = re.split(r"[,\s]+", raw)
    out: List[str] = []
    seen = set()
    for tok in tokens:
        if not tok:
            continue
        if not re.match(r"^ISSUE-\d{4}$", tok):
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _extract_acceptance(body: str) -> List[str]:
    ac_head = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:Acceptance Criteria|AC|Acceptance)\s*:?\s*$"
    )
    stop_re = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:In Scope|Out of Scope|Acceptance Criteria|AC|Acceptance|Technical Notes|Test Requirements|Risk)\s*:?\s*$"
    )
    m = ac_head.search(body)
    if not m:
        return []
    sub = body[m.end():]
    end = stop_re.search(sub)
    chunk = sub[:end.start()] if end else sub
    bullets = []
    for line in chunk.splitlines():
        s = line.strip().lstrip("-*").strip()
        if s:
            bullets.append(s)
    return bullets


# ---------------------------------------------------------------------------
# Issue ID allocation
# ---------------------------------------------------------------------------

def _next_issue_id(target: Optional[str] = None) -> str:
    """Scan existing ISSUE-*.md files and return the next ISSUE-NNNN id."""
    pattern = os.path.join(state._issues_dir(target), "ISSUE-*.md")
    existing = []
    for path in glob.glob(pattern):
        base = os.path.basename(path)
        m = re.match(r"ISSUE-(\d+)\.md$", base)
        if m:
            existing.append(int(m.group(1)))
    nxt = (max(existing) + 1) if existing else 1
    # Also consider tasks.json to avoid reuse if a file was deleted but state remains.
    tasks = state._load_tasks(target)
    used = {int(k.split("-")[-1]) for k in tasks.keys() if re.match(r"ISSUE-\d+$", k)}
    while nxt in used:
        nxt += 1
    return f"ISSUE-{nxt:04d}"


# ---------------------------------------------------------------------------
# Issue file rendering
# ---------------------------------------------------------------------------

def _render_issue(issue: Dict[str, Any]) -> str:
    """Render the 13-field issue as markdown."""
    scope = issue["scope"]
    risk = issue["risk"]
    route = issue["routing"]
    source = issue["source"]
    test_req = issue["test_requirements"]
    ac_lines = issue["acceptance_criteria"]
    out = [
        f"# {issue['issue_id']}: {issue['summary']}",
        "",
        f"**Issue ID**: {issue['issue_id']}",
        f"**Status**: {issue['status']}",
        f"**Summary**: {issue['summary']}",
        "",
        "## Background",
        issue["background"],
        "",
        "## Dependencies",
        f"- depends_on: {', '.join(issue['depends_on']) if issue['depends_on'] else '(none)'}",
        "",
        "## Scope",
        "**In Scope:**",
        f"- {scope['in_scope']}",
        "**Out of Scope:**",
        f"- {scope['out_scope']}",
        "",
        "## Acceptance Criteria",
    ]
    if ac_lines:
        for a in ac_lines:
            out.append(f"- {a}")
    else:
        out.append("- TBD - PM agent to refine")
    out.extend([
        "",
        "## Technical Notes",
        issue["technical_notes"],
        "",
        "## Test Requirements",
        f"- Unit: {test_req['unit']}",
        f"- Integration: {test_req['integration']}",
        f"- E2E: {test_req['e2e']}",
        f"- Regression: {test_req['regression']}",
        f"- Manual: {test_req['manual']}",
        "",
        "## Risk / Release Impact",
        f"- Risk Level: {risk['level']}",
        f"- Release Type: {risk['release_type']}",
        f"- Security Sensitivity: {risk['security_sensitivity']}",
        "",
        "## Routing Metadata",
        f"- Type: {route['type']}",
        f"- Priority: {route['priority']}",
        f"- Area: {route['area']}",
        f"- Route: {route['route']}",
        "",
        "## Source",
        f"- Document: {source['document']}",
        f"- Section: {source['section']}",
        f"- Lines: {source['lines']}",
        f"- Excerpt: {source.get('excerpt', 'TBD')}",
        "",
        "## Run History",
        json_dump_compact(issue["run_history"]),
        "",
    ])
    return "\n".join(out)


def json_dump_compact(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Core command
# ---------------------------------------------------------------------------

def cmd_intake(prd_path: str, target: Optional[str] = None) -> int:
    root = state._harness_root(target)
    if not os.path.isdir(os.path.join(root, ".harness")):
        print(f"Laplace is not initialized at {root}. Run /laplace:init first.", file=sys.stderr)
        return 1
    if not os.path.isfile(prd_path):
        print(f"PRD not found: {prd_path}", file=sys.stderr)
        return 1
    with open(prd_path, "r", encoding="utf-8") as f:
        text = f.read()

    sections = _split_sections(text)
    if not sections:
        # Treat whole doc as a single section.
        sections = [("(untitled)", text, 0, max(len(text.splitlines()) - 1, 0))]

    # One-writer critical section for sequential ID allocation + state updates.
    ok, reason = state.acquire_lock(_INTAKE_LOCK_ID, target=target)
    if not ok:
        print(f"intake lock failed: {reason}", file=sys.stderr)
        return 3
    created: List[str] = []
    try:
        tasks = state._load_tasks(target)
        queue = state._load_queue(target)
        for (title, body, start, end) in sections:
            issue_id = _next_issue_id(target)
            # Redact ALL user-supplied content before persist (G-LP-003).
            r_title = state._redact_evidence(title)
            r_body = state._redact_evidence(body)
            in_scope, out_scope = _extract_scope(body)
            in_scope = state._redact_evidence(in_scope)
            out_scope = state._redact_evidence(out_scope)
            ac = [state._redact_evidence(a) for a in _extract_acceptance(body)]
            summary = r_title or "TBD"
            background = state._redact_evidence(_extract_background(body))
            deps = _extract_depends_on(body)
            issue_type = _infer_type(title, body)
            area = state._redact_evidence(_infer_area(title))
            # Relative path keeps Source portable across machines.
            try:
                rel_doc = os.path.relpath(os.path.abspath(prd_path), root)
            except ValueError:
                rel_doc = os.path.abspath(prd_path)
            rel_doc = state._redact_evidence(rel_doc)
            # Redacted raw excerpt so downstream agents can quote PRD text
            # safely. Capped to keep issue files readable.
            raw_excerpt = r_body[:800] if r_body else "TBD"
            issue = {
                "issue_id": issue_id,
                "status": "draft",
                "summary": summary,
                "background": background or "TBD",
                "scope": {"in_scope": in_scope, "out_scope": out_scope},
                "acceptance_criteria": ac,
                "depends_on": deps,
                "technical_notes": "TBD",
                "test_requirements": {
                    "unit": "TBD", "integration": "TBD", "e2e": "TBD",
                    "regression": "TBD", "manual": "TBD",
                },
                "risk": {
                    "level": "medium",
                    "release_type": "patch",
                    "security_sensitivity": "low",
                },
                "routing": {
                    "type": issue_type,
                    "priority": "p2",
                    "area": area,
                    "route": "pm-review",
                },
                "source": {
                    "document": rel_doc,
                    "section": summary,
                    "lines": f"{start + 1}-{end + 1}",
                    "excerpt": raw_excerpt,
                },
                "run_history": [],
            }
            content = _render_issue(issue)
            out_path = os.path.join(state._issues_dir(target), f"{issue_id}.md")
            state._atomic_write_text(out_path, content)
            # Register in state.
            tasks[issue_id] = {
                "status": "draft",
                "updated_at": time.time(),
                "created_at": time.time(),
                "source": rel_doc,
                "depends_on": deps,
            }
            if issue_id not in queue["draft"]:
                queue["draft"].append(issue_id)
            created.append(issue_id)
        state._save_tasks(tasks, target=target)
        state._save_queue(queue, target=target)
    finally:
        state.release_lock(_INTAKE_LOCK_ID, target=target)

    # SPEC-002 §Output Format — Result template.
    print("Laplace result: intake complete")
    print()
    print(f"Issue: (none) -> {', '.join(created)}")
    print()
    print("State: (no issues) -> draft")
    print()
    print("Evidence:")
    for cid in created:
        path = os.path.join(state._issues_dir(target), f"{cid}.md")
        print(f"  - {cid}: {path}")
    print(f"  - queue.json draft: {len(state._load_queue(target)['draft'])} entries")
    print(f"  - tasks.json: {len(state._load_tasks(target))} issues")
    print()
    print("Artifacts:")
    for cid in created:
        print(f"  - .harness/issues/{cid}.md")
    print()
    print("Risks:")
    print("  - none (draft only; no source files modified; no auto-approval)")
    print()
    print("Next:")
    if created:
        print(f"  /laplace:list  (or /laplace:approve {created[0]})")
    else:
        print("  /laplace:intake <prd> with a more structured document")
    return 0


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest() -> int:
    import shutil
    import tempfile
    import json

    failures: List[str] = []
    tmp_repo = tempfile.mkdtemp(prefix="laplace-intake-selftest-")
    saved = sys.stdout
    saved_err = sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        # Initialize the harness tree in the fixture repo.
        if state.cmd_init(target=tmp_repo) != 0:
            failures.append("state.cmd_init returned non-zero")
            raise RuntimeError("init failed")

        # --- Case 1: structured PRD with 3 sections -------------------------
        prd_text = (
            "# Widget Service PRD\n\n"
            "Intro paragraph that spans the whole product.\n\n"
            "## Feature: User Login\n\n"
            "Users need to log in via OAuth. This is core to onboarding.\n\n"
            "Depends on: ISSUE-0001\n\n"
            "In Scope:\n"
            "- Login page\n"
            "- Token refresh\n\n"
            "Out of Scope:\n"
            "- SSO\n\n"
            "Acceptance Criteria:\n"
            "- Given valid creds, return 200\n"
            "- Given invalid creds, return 401\n\n"
            "## Task: Fix crash on empty email\n\n"
            "App crashes when email is blank. Bug fix needed.\n\n"
            "## Requirement: Audit log retention\n\n"
            "Retention must be 90 days. This is a docs update.\n"
        )
        # Embed an assembled fake token so no literal token is source-scanned.
        # Token pattern: "api_key: <24+ alnum>". Build at runtime.
        token_val = "x" * 28
        prd_text_with_secret = prd_text + f"\n\nSecret note: api_key: {token_val}\n"
        prd_path = os.path.join(tmp_repo, "prd.md")
        with open(prd_path, "w", encoding="utf-8") as f:
            f.write(prd_text_with_secret)

        rc = cmd_intake(prd_path, target=tmp_repo)
        if rc != 0:
            failures.append(f"intake returned {rc}")

        issues_dir = state._issues_dir(tmp_repo)
        issue_files = sorted(glob.glob(os.path.join(issues_dir, "ISSUE-*.md")))
        if len(issue_files) != 3:
            failures.append(f"expected 3 issue files, got {len(issue_files)}: {issue_files}")

        # --- Verify each issue has all 13 schema fields ---------------------
        required_fields = [
            "Issue ID", "Status", "Summary", "Background", "Scope",
            "Acceptance Criteria", "Technical Notes", "Test Requirements",
            "Risk / Release Impact", "Routing Metadata", "Source", "Run History",
        ]
        for path in issue_files:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            for field in required_fields:
                if field not in content:
                    failures.append(f"{os.path.basename(path)} missing field: {field}")
            # Status must be draft.
            if "**Status**: draft" not in content:
                failures.append(f"{os.path.basename(path)} status not draft")
            # Route must be pm-review (never auto-approved).
            if "Route: pm-review" not in content:
                failures.append(f"{os.path.basename(path)} route not pm-review")
            # Test requirements sub-bullets present.
            for sub in ["Unit:", "Integration:", "E2E:", "Regression:", "Manual:"]:
                if sub not in content:
                    failures.append(f"{os.path.basename(path)} missing test sub {sub}")
            # Risk fields present.
            for sub in ["Risk Level:", "Release Type:", "Security Sensitivity:"]:
                if sub not in content:
                    failures.append(f"{os.path.basename(path)} missing risk sub {sub}")

        # --- queue.json + tasks.json consistency ----------------------------
        queue = state._load_queue(tmp_repo)
        tasks = state._load_tasks(tmp_repo)
        if len(queue["draft"]) != 3:
            failures.append(f"queue draft should have 3, got {len(queue['draft'])}")
        for cid in ["ISSUE-0001", "ISSUE-0002", "ISSUE-0003"]:
            if cid not in queue["draft"]:
                failures.append(f"{cid} not in queue.draft")
            if cid not in tasks:
                failures.append(f"{cid} not in tasks.json")
            elif tasks[cid].get("status") != "draft":
                failures.append(f"{cid} tasks.json status != draft")

        # --- Type inference -------------------------------------------------
        # ISSUE-0002 contains "Fix crash" -> bug
        with open(os.path.join(issues_dir, "ISSUE-0002.md"), "r", encoding="utf-8") as f:
            c2 = f.read()
        if "Type: bug" not in c2:
            failures.append(f"ISSUE-0002 type inference wrong; expected bug.\n{c2[:400]}")

        # --- Scope + AC extraction from plain-text labels -------------------
        with open(os.path.join(issues_dir, "ISSUE-0001.md"), "r", encoding="utf-8") as f:
            c1 = f.read()
        if "Login page" not in c1:
            failures.append("ISSUE-0001 In Scope not extracted (Login page missing)")
        if "Token refresh" not in c1:
            failures.append("ISSUE-0001 In Scope bullet Token refresh missing")
        if "SSO" not in c1:
            failures.append("ISSUE-0001 Out of Scope SSO missing")
        if "return 200" not in c1:
            failures.append("ISSUE-0001 Acceptance Criteria not extracted")
        if "return 401" not in c1:
            failures.append("ISSUE-0001 Acceptance Criteria 401 missing")

        # --- depends_on parsing --------------------------------------------
        if "## Dependencies" not in c1:
            failures.append("ISSUE-0001 missing Dependencies section")
        if "depends_on: ISSUE-0001" not in c1:
            failures.append("ISSUE-0001 depends_on not rendered in .md")
        if tasks.get("ISSUE-0001", {}).get("depends_on") != ["ISSUE-0001"]:
            failures.append(f"ISSUE-0001 depends_on not in tasks.json: {tasks.get('ISSUE-0001')}")
        # Other issues should render (none) and have empty lists.
        for cid, path in [("ISSUE-0002", issue_files[1]), ("ISSUE-0003", issue_files[2])]:
            with open(path, "r", encoding="utf-8") as f:
                cc = f.read()
            if "depends_on: (none)" not in cc:
                failures.append(f"{cid} should render empty depends_on as (none)")
            if tasks.get(cid, {}).get("depends_on") != []:
                failures.append(f"{cid} tasks.json depends_on should be empty list")

        # --- Redaction of Source field -------------------------------------
        # The fake token must NOT appear in any persisted issue file.
        for path in issue_files:
            with open(path, "r", encoding="utf-8") as f:
                c = f.read()
            if token_val in c:
                failures.append(f"{os.path.basename(path)} leaked token in content")
            if "REDACTED" not in c:
                # At least one file (the section containing the secret) must show redaction.
                pass
        any_redacted = any(
            "REDACTED" in open(p, "r", encoding="utf-8").read() for p in issue_files
        )
        if not any_redacted:
            failures.append("no issue file showed evidence of redaction")

        # --- Case 2: undivided PRD -> single issue --------------------------
        tmp_repo2 = tempfile.mkdtemp(prefix="laplace-intake-selftest2-")
        state.cmd_init(target=tmp_repo2)
        plain_prd = os.path.join(tmp_repo2, "plain.md")
        with open(plain_prd, "w", encoding="utf-8") as f:
            f.write("Just a single paragraph. No headings at all.\n")
        rc = cmd_intake(plain_prd, target=tmp_repo2)
        if rc != 0:
            failures.append(f"intake(plain) returned {rc}")
        files2 = glob.glob(os.path.join(state._issues_dir(tmp_repo2), "ISSUE-*.md"))
        if len(files2) != 1:
            failures.append(f"plain PRD should yield 1 issue, got {len(files2)}")
        shutil.rmtree(tmp_repo2, ignore_errors=True)

        # --- Case 3: missing PRD path --------------------------------------
        rc = cmd_intake(os.path.join(tmp_repo, "does-not-exist.md"), target=tmp_repo)
        if rc == 0:
            failures.append("intake on missing PRD should return non-zero")

        # --- Case 4: uninitialized repo ------------------------------------
        tmp_repo3 = tempfile.mkdtemp(prefix="laplace-intake-selftest3-")
        rc = cmd_intake(prd_path, target=tmp_repo3)
        if rc == 0:
            failures.append("intake on uninitialized repo should return non-zero")
        shutil.rmtree(tmp_repo3, ignore_errors=True)

        # --- AC-LP-005 spot check: all required semantic fields -------------
        sample_path = os.path.join(issues_dir, "ISSUE-0001.md")
        with open(sample_path, "r", encoding="utf-8") as f:
            sample = f.read()
        ac_checks = {
            "status draft": "**Status**: draft",
            "scope present": "## Scope",
            "acceptance criteria": "## Acceptance Criteria",
            "test requirements": "## Test Requirements",
            "risk release impact": "## Risk / Release Impact",
            "routing route pm-review": "Route: pm-review",
            "source present": "## Source",
        }
        for label, needle in ac_checks.items():
            if needle not in sample:
                failures.append(f"AC-LP-005 check failed: {label} ({needle!r}) absent")
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = saved
        sys.stderr = saved_err
        shutil.rmtree(tmp_repo, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("intake selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Convenience: `intake.py selftest` and `intake.py <prd>` both work without
    # an explicit subcommand. If the first arg is a known subcommand or a
    # --target flag, parse normally; otherwise treat it as the PRD path.
    known_subs = {"intake", "selftest"}
    if raw and (raw[0] in known_subs or raw[0].startswith("-")):
        parser = argparse.ArgumentParser(prog="intake.py",
                                         description="Laplace PRD -> draft issues")
        sub = parser.add_subparsers(dest="cmd", required=True)
        p = sub.add_parser("intake", help="Convert a PRD into draft issues")
        p.add_argument("prd", help="Path to PRD/story markdown file")
        p.add_argument("--target", default=None,
                       help="Repository root containing .harness/ (default: CWD)")
        p.set_defaults(func=lambda a: cmd_intake(a.prd, a.target))
        p = sub.add_parser("selftest", help="Internal sanity checks")
        p.set_defaults(func=lambda a: selftest())
        args = parser.parse_args(argv)
        return args.func(args)
    # Direct form: intake.py <prd> [--target <root>]
    parser = argparse.ArgumentParser(prog="intake.py",
                                     description="Laplace PRD -> draft issues")
    parser.add_argument("prd", help="Path to PRD/story markdown file")
    parser.add_argument("--target", default=None,
                        help="Repository root containing .harness/ (default: CWD)")
    args = parser.parse_args(argv)
    return cmd_intake(args.prd, args.target)


if __name__ == "__main__":
    sys.exit(main())
