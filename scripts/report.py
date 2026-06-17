#!/usr/bin/env python3
"""Laplace artifact generator.

Reads issue + run log + evidence and writes REDACTED reports, patches, and
PR drafts under .harness/artifacts/. Does NOT create PRs, push, or any external
side effect. All persisted content passes through redaction.

CLI:
  report.py issue <issue-id> [--target <repo-root>]
  report.py patch <issue-id> [--base <branch>]
  report.py pr-draft <issue-id>
  report.py selftest

stdlib-only.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import policy  # type: ignore
from redaction import redact  # type: ignore
import state  # type: ignore


def _issues_dir(target: Optional[str] = None) -> str:
    return state._issues_dir(target)


def _runs_dir(target: Optional[str] = None) -> str:
    return state._runs_dir(target)


def _artifacts_dir(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), ".harness", "artifacts")


def _read_issue(issue_id: str, target: Optional[str] = None) -> Optional[str]:
    path = os.path.join(_issues_dir(target), f"{issue_id}.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_runs_for_issue(issue_id: str, target: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all run logs whose issue_id matches (best-effort)."""
    runs_dir = _runs_dir(target)
    if not os.path.isdir(runs_dir):
        return []
    out = []
    for name in sorted(os.listdir(runs_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(runs_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                run = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(run, dict) and (run.get("issue_id") == issue_id
                                      or run.get("issue_id") == redact(issue_id)):
            out.append(run)
    return out


def _load_task(issue_id: str, target: Optional[str] = None) -> Dict[str, Any]:
    tasks = state._load_tasks(target)
    return tasks.get(issue_id, {})


def _summary_from_issue(issue_text: str) -> str:
    """Extract a one-line summary from an issue markdown body."""
    for line in issue_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            return s.lstrip("# ").strip()[:120]
    # Fallback: first non-empty line.
    for line in issue_text.splitlines():
        s = line.strip()
        if s:
            return s[:120]
    return "(no summary)"


def _ac_lines(issue_text: str) -> List[str]:
    """Yank lines under an '## Acceptance Criteria' heading (best-effort)."""
    out: List[str] = []
    in_section = False
    for line in issue_text.splitlines():
        s = line.strip()
        if s.lower().startswith("## acceptance"):
            in_section = True
            continue
        if in_section and s.startswith("## "):
            break
        if in_section and s:
            out.append(s)
    return out


def cmd_issue(args: argparse.Namespace) -> int:
    target = args.target
    issue_text = _read_issue(args.issue_id, target)
    if issue_text is None:
        sys.stderr.write(f"issue not found: {args.issue_id}\n")
        return 1
    runs = _load_runs_for_issue(args.issue_id, target)
    task = _load_task(args.issue_id, target)
    summary = _summary_from_issue(issue_text)
    ac_lines = _ac_lines(issue_text)

    lines: List[str] = []
    lines.append(f"# Issue Report: {redact(args.issue_id)}")
    lines.append("")
    lines.append(f"**Summary:** {redact(summary)}")
    lines.append(f"**Status:** {task.get('status', 'unknown')}")
    if task.get("updated_at"):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(task["updated_at"])))
        lines.append(f"**Updated:** {ts}")
    lines.append("")
    lines.append("## Acceptance Criteria")
    if ac_lines:
        for ac in ac_lines:
            lines.append(f"- {redact(ac)}")
    else:
        lines.append("(none recorded)")
    lines.append("")
    lines.append("## Run History")
    if runs:
        for run in runs:
            rid = run.get("run_id", "?")
            agent = run.get("agent", "?")
            outcome = run.get("outcome", "?")
            attempt = run.get("attempt", "?")
            lines.append(f"- run `{rid}` agent={agent} attempt={attempt} outcome={redact(str(outcome))}")
            for ev in run.get("evidence", []) or []:
                lines.append(f"  - evidence: {redact(str(ev))}")
    else:
        lines.append("(no runs recorded)")
    lines.append("")
    lines.append("## Test / Review / Security Outcomes")
    found = False
    for run in runs:
        outcome = str(run.get("outcome", ""))
        if outcome in {"review-passed", "security-passed", "blocked",
                       "needs-fix", "human-approval-required"}:
            found = True
            lines.append(f"- {redact(outcome)} (run {run.get('run_id', '?')})")
    if not found:
        lines.append("(no terminal outcomes yet)")
    lines.append("")
    lines.append("## Next Safe Action")
    status = task.get("status", "")
    if status == "review-passed":
        lines.append(f"Run `report.py patch {args.issue_id}` then `report.py pr-draft {args.issue_id}`.")
    elif status in {"done", "release-candidate"}:
        lines.append("Issue complete.")
    else:
        lines.append(f"Continue current phase for {args.issue_id} via /laplace:run.")
    body = "\n".join(lines) + "\n"

    out_dir = os.path.join(_artifacts_dir(target), "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{args.issue_id}.md")
    state._atomic_write_text(out_path, body)
    print(out_path)
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    target = args.target
    base = args.base or "main"
    issue_branch = f"laplace/{args.issue_id}"
    # Route the git command through policy.check_command first.
    git_cmd = f"git diff {base}...{issue_branch}"
    ok, reason = policy.check_command(git_cmd)
    if not ok:
        sys.stderr.write(f"patch generation blocked by policy: {reason}\n")
        return 2
    # Fail-safe if not a git repo.
    root = state._harness_root(target)
    if not os.path.isdir(os.path.join(root, ".git")):
        sys.stderr.write(f"not a git repo: {root}\n")
        return 1
    try:
        proc = subprocess.run(
            ["git", "-C", root, "diff", f"{base}...{issue_branch}"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        sys.stderr.write(f"git diff failed: {exc}\n")
        return 1
    if proc.returncode != 0:
        # Branch may not exist; emit empty patch file but warn.
        sys.stderr.write(f"git diff nonzero exit ({proc.returncode}): {proc.stderr.strip()}\n")
    raw_diff = proc.stdout or ""
    # Redact any secret-shaped lines in the diff.
    redacted_diff = redact(raw_diff)
    out_dir = os.path.join(_artifacts_dir(target), "patches")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{args.issue_id}.patch")
    header = f"# Laplace patch for {args.issue_id} (base={base}, branch={issue_branch})\n"
    state._atomic_write_text(out_path, header + redacted_diff)
    print(out_path)
    return 0


def cmd_pr_draft(args: argparse.Namespace) -> int:
    target = args.target
    issue_text = _read_issue(args.issue_id, target)
    if issue_text is None:
        sys.stderr.write(f"issue not found: {args.issue_id}\n")
        return 1
    runs = _load_runs_for_issue(args.issue_id, target)
    task = _load_task(args.issue_id, target)
    summary = _summary_from_issue(issue_text)
    ac_lines = _ac_lines(issue_text)

    title = f"[{args.issue_id}] {redact(summary)}"
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("## Summary")
    lines.append(redact(summary))
    lines.append("")
    lines.append("## Acceptance Criteria Mapping")
    if ac_lines:
        for ac in ac_lines:
            lines.append(f"- [ ] {redact(ac)}")
    else:
        lines.append("(none recorded)")
    lines.append("")
    lines.append("## Evidence Summary")
    if runs:
        for run in runs:
            lines.append(f"- run `{run.get('run_id', '?')}` outcome={redact(str(run.get('outcome', '?')))}")
            for ev in run.get("evidence", []) or []:
                lines.append(f"  - {redact(str(ev))}")
    else:
        lines.append("(no runs recorded)")
    lines.append("")
    lines.append("## Risk Notes")
    status = task.get("status", "")
    if status == "review-passed":
        lines.append("Review passed. Patch generated under .harness/artifacts/patches/.")
    else:
        lines.append(f"Current status: {status}. Review before merge.")
    lines.append("")
    lines.append("## Out of Scope")
    lines.append("- No GitHub PR creation, push, or publish performed by Laplace.")
    body = "\n".join(lines) + "\n"

    out_dir = os.path.join(_artifacts_dir(target), "pr-drafts")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{args.issue_id}.md")
    state._atomic_write_text(out_path, body)
    print(out_path)
    return 0


# late import: time used inside cmd_issue only
import time  # noqa: E402


# --- selftest ----------------------------------------------------------------

def selftest() -> int:
    import shutil
    import tempfile

    failures = []
    tmp = tempfile.mkdtemp(prefix="laplace-report-")
    saved_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        # Initialize harness tree.
        state.cmd_init(target=tmp)
        # Seed an issue.
        issue_id = "ISSUE-REPORT-1"
        issue_text = (
            f"# {issue_id}: add feature X\n\n"
            "## Summary\nAdd feature X to module Y.\n\n"
            "## Acceptance Criteria\n"
            "- AC1: feature X works\n"
            "- AC2: tests pass\n"
        )
        issue_path = os.path.join(_issues_dir(tmp), f"{issue_id}.md")
        state._atomic_write_text(issue_path, issue_text)
        # Seed a run with a fake secret in evidence to prove redaction.
        filler = "a" * 24
        run = {
            "run_id": "runreport1",
            "issue_id": issue_id,
            "started_at": time.time(),
            "ended_at": time.time(),
            "outcome": "review-passed",
            "agent": "dev",
            "attempt": 1,
            "evidence": [f"tests: 3/3 passed; token=Bearer {filler}"],
        }
        state._atomic_write_json(os.path.join(_runs_dir(tmp), "runreport1.json"), run)
        # Mark task as review-passed.
        state._save_tasks({issue_id: {"status": "review-passed",
                                       "updated_at": time.time()}}, target=tmp)

        # 1. report.py issue
        ns = argparse.Namespace(issue_id=issue_id, target=tmp)
        rc = cmd_issue(ns)
        if rc != 0:
            failures.append(f"cmd_issue returned {rc}")
        report_path = os.path.join(_artifacts_dir(tmp), "reports", f"{issue_id}.md")
        if not os.path.isfile(report_path):
            failures.append("report not written")
        else:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            if filler in content:
                failures.append("report leaked raw secret token")
            if "review-passed" not in content:
                failures.append("report missing outcome")
            if "AC1" not in content:
                failures.append("report missing AC mapping")

        # 2. report.py patch (not a git repo -> exit 1, no crash).
        ns = argparse.Namespace(issue_id=issue_id, base="main", target=tmp)
        rc = cmd_patch(ns)
        if rc == 0:
            # If it happened to succeed (git repo present), still assert redaction.
            patch_path = os.path.join(_artifacts_dir(tmp), "patches", f"{issue_id}.patch")
            if os.path.isfile(patch_path):
                with open(patch_path, "r", encoding="utf-8") as f:
                    if filler in f.read():
                        failures.append("patch leaked raw secret")

        # 3. report.py pr-draft
        ns = argparse.Namespace(issue_id=issue_id, target=tmp)
        rc = cmd_pr_draft(ns)
        if rc != 0:
            failures.append(f"cmd_pr_draft returned {rc}")
        pr_path = os.path.join(_artifacts_dir(tmp), "pr-drafts", f"{issue_id}.md")
        if not os.path.isfile(pr_path):
            failures.append("pr draft not written")
        else:
            with open(pr_path, "r", encoding="utf-8") as f:
                content = f.read()
            if filler in content:
                failures.append("pr-draft leaked raw secret")
            if "Out of Scope" not in content:
                failures.append("pr-draft missing out-of-scope note")

    finally:
        sys.stdout.close()
        sys.stdout = saved_stdout
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("report selftest: PASS")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="report.py",
                                     description="Laplace artifact generator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("issue", help="Generate issue report")
    p.add_argument("issue_id")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("patch", help="Generate redacted patch")
    p.add_argument("issue_id")
    p.add_argument("--base", default=None)
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("pr-draft", help="Generate PR draft (no API call)")
    p.add_argument("issue_id")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_pr_draft)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
