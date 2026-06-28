#!/usr/bin/env python3
"""Laplace run orchestrator (P3, SPEC-002 §State Machine, §Command Surface).

Responsibilities:
  - Drive one issue through phase transitions with branch isolation
  - Capture redacted evidence into the run log
  - Enforce lock discipline around state-changing operations
  - Provide deterministic scaffolding the run skill calls into

This module does NOT invoke LLM agents. The run skill instructs the
model; runner.py provides the deterministic operations the skill composes.

stdlib + subprocess only. subprocess is used for git only and every git
command is routed through policy.check_command first. Reuses state.py
atomic helpers, lock helpers, and state-machine validation — does NOT
reimplement state logic.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer modules: state.py (atomic I/O, locks, state machine) and policy.py
# (hard-safety command/path checks). Imported after sys.path bootstrap above.
# MAX_FIX_ATTEMPTS / MAX_SECURITY_FIX_ATTEMPTS are imported from state.py to
# avoid duplicating limit constants (AC-LP-010 single-source-of-truth).
import state  # noqa: E402
import policy  # noqa: E402

MAX_FIX_ATTEMPTS = state.MAX_FIX_ATTEMPTS
MAX_SECURITY_FIX_ATTEMPTS = state.MAX_SECURITY_FIX_ATTEMPTS

BRANCH_PREFIX = "laplace"
# Phase 4: Extended evidence kinds (LazyCodex/MoAI-ADK patterns)
ALLOWED_EVIDENCE_KINDS = (
    "test", "review", "security", "manual", "command",
    "reproduction", "visual",
    # New evidence kinds for workflow automation
    "spec-validation",     # SPEC/YAML frontmatter validation
    "workflow-plan",       # Workflow plan document
    "metric-capture",      # Performance/metric measurements
    "integration-test",    # Integration test results
    "audit-report",        # Auditor verdicts
)
EVIDENCE_SUMMARY_MAX = 1000

# SPEC-003: per-type evidence requirements on state exits. Keys are issue
# routing types; values map {from_state: [evidence_kind, ...]}. The runner
# refuses to advance out of `from_state` until each listed kind appears in
# the run log. Defaults mirror routing-rules.yml `evidence_requirements:`.
# Users override via routing-rules.yml; this dict is the fallback when the
# file omits a type or the block is absent.
DEFAULT_EVIDENCE_REQUIREMENTS = {
    "bug": {"pm-review": ["reproduction"]},
    "ui": {"review": ["visual"]},
}

# Exit code for fix-attempt limit exceeded (AC-LP-010). Orchestrator routes
# the issue to `blocked` or `human-approval-required` on this signal.
EXIT_FIX_LIMIT_EXCEEDED = 5

# Security-review path triggers (AC-LP-009). A diff touching any of these
# patterns forces a security review regardless of issue-declared risk.
SECURITY_PATH_PATTERNS: Tuple[str, ...] = (
    "**/auth/**",
    "**/security/**",
    "**/.github/workflows/**",
    "**/scripts/**",
    "**/Dockerfile",
    "**/docker-compose*.yml",
    "**/package.json",
    "**/requirements*.txt",
    "**/go.mod",
    "**/Cargo.toml",
    "**/pyproject.toml",
    "**/.mcp.json",
    "**/settings.json",
)

# External-API substring markers (AC-LP-009). Best-effort: a diff that adds
# any of these patterns is treated as touching external I/O.
EXTERNAL_API_MARKERS: Tuple[str, ...] = (
    "fetch(", "requests.", "http.", "axios.",
)


# ---------------------------------------------------------------------------
# Auditor integration (Phase 2: Independent Auditors)
# ---------------------------------------------------------------------------

def _call_plan_auditor(issue_id: str, plan_path: str,
                       harness_root: str, run_id: str) -> Tuple[bool, str]:
    """Call the plan auditor to validate a workflow plan.

    Args:
        issue_id: Issue identifier
        plan_path: Path to workflow plan file
        harness_root: Path to .harness/ directory
        run_id: Run identifier

    Returns:
        (ok, reason) tuple - True if auditor passes
    """
    try:
        # Import component registry
        import sys
        sys.path.insert(0, os.path.join(HERE, ".."))
        from components.verify import plan_auditor

        # Create auditor component
        auditor = plan_auditor.PlanAuditorComponent()

        # Initialize if needed
        auditor.initialize(harness_root)

        # Execute audit
        context = {
            "issue_id": issue_id,
            "plan_path": plan_path,
            "harness_root": harness_root,
            "run_id": run_id,
        }
        result = auditor.execute(context)

        if result["success"]:
            return True, f"Plan audit passed: {result['output']['audit_result']['reasoning']}"
        else:
            return False, f"Plan audit failed: {result.get('error', 'Unknown error')}"
    except Exception as e:
        # Auditor failure should not block - log and continue
        return False, f"Plan audit error: {e}"


def _call_sync_auditor(issue_id: str, run_id: str,
                       harness_root: str) -> Tuple[bool, str]:
    """Call the sync auditor to validate implementation results.

    Args:
        issue_id: Issue identifier
        run_id: Run identifier
        harness_root: Path to .harness/ directory

    Returns:
        (ok, reason) tuple - True if auditor passes
    """
    try:
        # Import component registry
        import sys
        sys.path.insert(0, os.path.join(HERE, ".."))
        from components.verify import sync_auditor

        # Create auditor component
        auditor = sync_auditor.SyncAuditorComponent()

        # Initialize if needed
        auditor.initialize(harness_root)

        # Get run log path
        run_log_path = os.path.join(harness_root, "state", "runs", f"{run_id}.json")

        # Execute audit
        context = {
            "issue_id": issue_id,
            "run_id": run_id,
            "run_log": run_log_path,
            "harness_root": harness_root,
        }
        result = auditor.execute(context)

        if result["success"]:
            return True, f"Sync audit passed: {result['output']['audit_result']['reasoning']}"
        else:
            return False, f"Sync audit failed: {result.get('error', 'Unknown error')}"
    except Exception as e:
        # Auditor failure should not block - log and continue
        return False, f"Sync audit error: {e}"


# Auditor integration points for state transitions
AUDITOR_INTEGRATION = {
    # Plan audit: pm-review -> ready-for-dev
    ("pm-review", "ready-for-dev"): "plan",
    # Sync audit: security-review -> review-passed
    ("security-review", "review-passed"): "sync",
    # Sync audit: cost-review -> review-passed (if cost_watcher enabled)
    ("cost-review", "review-passed"): "sync",
}


def _check_auditor_gate(from_state: str, to_state: str,
                        issue_id: str, harness_root: str,
                        run_id: Optional[str] = None) -> Tuple[bool, str]:
    """Check if auditor gate is required for this transition.

    Args:
        from_state: Current state
        to_state: Target state
        issue_id: Issue identifier
        harness_root: Path to .harness/ directory
        run_id: Run identifier (optional for plan audit)

    Returns:
        (ok, reason) tuple - True if gate passes or not required
    """
    gate_type = AUDITOR_INTEGRATION.get((from_state, to_state))
    if not gate_type:
        return True, "No auditor gate for this transition"

    if gate_type == "plan":
        # Plan audit requires workflow plan file
        plan_path = os.path.join(harness_root, "components", "workflow", "state.json")
        if not os.path.exists(plan_path):
            return False, f"Plan audit required but plan not found: {plan_path}"

        # Use a default run_id for plan audit
        audit_run_id = run_id or f"plan-{issue_id}"
        return _call_plan_auditor(issue_id, plan_path, harness_root, audit_run_id)

    elif gate_type == "sync":
        # Sync audit requires run_id
        if not run_id:
            return False, "Sync audit requires run_id"

        return _call_sync_auditor(issue_id, run_id, harness_root)

    return True, "Unknown gate type - allowing"


# ---------------------------------------------------------------------------
# Branch isolation (worktree-per-issue, ISSUE-0002)
# ---------------------------------------------------------------------------

# Exit code for stale-branch halt at start (AC-WT-004). The orchestrator
# surfaces BRANCH_STALE to the human; the issue stays in `approved`.
EXIT_BRANCH_STALE = 6

# Exit code for dirty-worktree halt at end (AC-WT-008). The orchestrator
# surfaces WORKTREE_DIRTY to the human; the run lock stays held.
EXIT_WORKTREE_DIRTY = 7


class BranchInfo:
    """Outcome of branch setup. status in {created, reused, stale, skipped}.

    `worktree_path` is set for `created` and `reused` outcomes (the absolute
    path to the per-issue worktree). It is None for `skipped` (non-repo /
    policy-denied) and `stale` (branch behind main, no worktree created).
    """

    def __init__(self, name: str, status: str, reason: str = "",
                 worktree_path: Optional[str] = None) -> None:
        self.name = name
        self.status = status
        self.reason = reason
        self.worktree_path = worktree_path

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.worktree_path:
            out["worktree_path"] = self.worktree_path
        return out


def _in_git_repo(target: Optional[str]) -> bool:
    """True iff `target` is inside a git work tree and git is on PATH."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=target or os.getcwd(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _branch_name_for(issue_id: str) -> str:
    safe = issue_id.replace("/", "_")
    return f"{BRANCH_PREFIX}/{safe}"


def _worktree_path(issue_id: str, target: Optional[str]) -> str:
    """Absolute path to the per-issue worktree.

    Lives under ``<target>/.harness/worktrees/<safe-issue-id>/`` so it is
    covered by the ``.harness/`` gitignore (ephemeral local state, never
    committed). The issue-id slash is collapsed to an underscore so the path
    is a single directory segment.
    """
    safe = issue_id.replace("/", "_")
    return os.path.join(
        state._harness_root(target), ".harness", "worktrees", safe)


def _resolve_base_branch(target: Optional[str]) -> Optional[str]:
    """Return 'main' or 'master' if that branch exists in the repo, else None.

    Each probe is routed through policy.check_command first (AC-WT-007).
    """
    cwd = target or os.getcwd()
    for base in ("main", "master"):
        probe = f"git rev-parse --verify {base}"
        ok, _ = policy.check_command(probe)
        if not ok:
            continue
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--verify", base],
                cwd=cwd, capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode == 0:
            return base
    return None


def _branch_exists(name: str, target: Optional[str]) -> bool:
    """True iff branch ``name`` exists in the repo. Policy-checked first."""
    probe = f"git rev-parse --verify {name}"
    ok, _ = policy.check_command(probe)
    if not ok:
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", name],
            cwd=target or os.getcwd(),
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _setup_branch(issue_id: str, target: Optional[str]) -> BranchInfo:
    """Create or reuse an isolated worktree+branch for the issue.

    Decision tree (AC-WT-001..007):
      - Not a git repo / git unavailable → skipped `not-a-git-repo` (AC-WT-006).
      - No `main`/`master` base resolvable → skipped `no-main-base`.
      - `laplace/<id>` does NOT exist → `git worktree add -b laplace/<id>
        <wt_path> <base>` → created (AC-WT-001).
      - `laplace/<id>` EXISTS:
          - `git merge-base --is-ancestor <base> laplace/<id>` exit 0
            (branch is current with or ahead of base) → `git worktree add
            <wt_path> laplace/<id>` → reused (AC-WT-005).
          - exit 1 (branch BEHIND base → stale) → stale, no worktree created
            (AC-WT-004). The caller halts start with BRANCH_STALE.

    Every git op is routed through policy.check_command first (AC-WT-007).
    On policy denial, returns skipped `policy-denied: <reason>` so the run
    proceeds with state transitions only (fail-safe).
    """
    name = _branch_name_for(issue_id)
    if not _in_git_repo(target):
        return BranchInfo(name=name, status="skipped", reason="not-a-git-repo")

    cwd = target or os.getcwd()
    base = _resolve_base_branch(target)
    if base is None:
        return BranchInfo(name=name, status="skipped", reason="no-main-base")

    wt_path = _worktree_path(issue_id, target)

    if not _branch_exists(name, target):
        # Create the branch in a fresh worktree (AC-WT-001).
        create_cmd = f"git worktree add -b {name} {wt_path} {base}"
        ok, reason = policy.check_command(create_cmd)
        if not ok:
            return BranchInfo(name=name, status="skipped",
                              reason=f"policy-denied: {reason}")
        try:
            r = subprocess.run(
                ["git", "worktree", "add", "-b", name, wt_path, base],
                cwd=cwd, capture_output=True, text=True, timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return BranchInfo(name=name, status="skipped",
                              reason=f"git-error: {exc}")
        if r.returncode == 0:
            return BranchInfo(name=name, status="created",
                              worktree_path=wt_path)
        return BranchInfo(
            name=name, status="skipped",
            reason=f"git-error: {(r.stderr or '').strip()}")

    # Branch exists — stale check (AC-WT-004).
    # Direction: --is-ancestor <base> <branch>. Exit 0 = base IS ancestor of
    # branch (branch is current/ahead) → reuse. Exit 1 = base is NOT ancestor
    # (branch is BEHIND main) → stale.
    stale_cmd = f"git merge-base --is-ancestor {base} {name}"
    ok, reason = policy.check_command(stale_cmd)
    if not ok:
        return BranchInfo(name=name, status="skipped",
                          reason=f"policy-denied: {reason}")
    try:
        stale = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, name],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return BranchInfo(name=name, status="skipped",
                          reason=f"git-error: {exc}")
    if stale.returncode == 1:
        # Branch is behind main → stale. Do NOT create a worktree.
        return BranchInfo(name=name, status="stale",
                          reason="branch-not-current-with-main")
    if stale.returncode != 0:
        # Unexpected git failure (not exit 0 or 1).
        return BranchInfo(
            name=name, status="skipped",
            reason=f"git-error: {(stale.stderr or '').strip()}")

    # Branch is current/ahead → reuse in a fresh worktree (AC-WT-005).
    reuse_cmd = f"git worktree add {wt_path} {name}"
    ok, reason = policy.check_command(reuse_cmd)
    if not ok:
        return BranchInfo(name=name, status="skipped",
                          reason=f"policy-denied: {reason}")
    try:
        r = subprocess.run(
            ["git", "worktree", "add", wt_path, name],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return BranchInfo(name=name, status="skipped",
                          reason=f"git-error: {exc}")
    if r.returncode == 0:
        return BranchInfo(name=name, status="reused",
                          worktree_path=wt_path)
    return BranchInfo(name=name, status="skipped",
                      reason=f"git-error: {(r.stderr or '').strip()}")


def _teardown_worktree(issue_id: str, target: Optional[str],
                       run_log: Optional[Dict[str, Any]],
                       force: bool = False) -> Tuple[int, str]:
    """Remove the per-issue worktree at end-of-run (AC-WT-003, AC-WT-008).

    Reads ``worktree_path`` from the run log. Absent (BRANCH_SKIPPED) → skip
    (nothing to remove). If the dir exists:
      - Probe dirty via `git -C <wt> status --porcelain`. Non-empty output
        AND not forced → halt with WORKTREE_DIRTY (AC-WT-008): return a
        non-zero rc, do NOT remove the worktree, do NOT finalize the run.
      - Clean (or forced) → `git worktree remove <wt>` (policy-checked first).
        The branch ``laplace/<id>`` is NOT deleted (preserved for merge,
        AC-WT-003).

    Returns (rc, status) where status is one of:
      "removed"        — worktree removed successfully.
      "dirty-halt"     — dirty worktree, not forced, run MUST halt.
      "skipped"        — no worktree_path in run log, or dir already gone.

    The caller records `worktree_teardown` into the run log and decides
    whether to proceed with state.cmd_run_end (rc==0) or halt (rc!=0).
    """
    wt_path = None
    if isinstance(run_log, dict):
        wt_path = run_log.get("worktree_path")
    if not wt_path:
        return 0, "skipped"
    if not os.path.isdir(wt_path):
        return 0, "skipped"

    # Dirty probe (AC-WT-008).
    dirty_cmd = f"git -C {wt_path} status --porcelain"
    ok, reason = policy.check_command(dirty_cmd)
    if not ok:
        # Policy denial on the probe — treat as halt to be safe (fail-safe
        # preserves dev work; the human can inspect and force).
        return EXIT_WORKTREE_DIRTY, "dirty-halt"
    try:
        r = subprocess.run(
            ["git", "-C", wt_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return EXIT_WORKTREE_DIRTY, "dirty-halt"
    if r.returncode == 0 and r.stdout.strip() and not force:
        return EXIT_WORKTREE_DIRTY, "dirty-halt"

    # Clean (or forced) — remove the worktree. Branch is preserved (AC-WT-003).
    rm_args = ["git", "worktree", "remove"]
    if force:
        rm_args.append("--force")
    rm_args.append(wt_path)
    rm_cmd = " ".join(rm_args)
    ok, reason = policy.check_command(rm_cmd)
    if not ok:
        return EXIT_WORKTREE_DIRTY, "dirty-halt"
    try:
        rr = subprocess.run(
            rm_args,
            cwd=state._harness_root(target),
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return EXIT_WORKTREE_DIRTY, "dirty-halt"
    if rr.returncode != 0:
        # git refused (e.g. dirty despite probe) — fail safe.
        return EXIT_WORKTREE_DIRTY, "dirty-halt"
    return 0, "removed"


# ---------------------------------------------------------------------------
# Run log helpers (compose state.py primitives; do not reimplement logic)
# ---------------------------------------------------------------------------

def _new_run_id(issue_id: str) -> str:
    raw = f"{issue_id}-{time.time()}-{os.getpid()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _run_log_path(run_id: str, target: Optional[str]) -> str:
    return os.path.join(state._runs_dir(target), f"{run_id}.json")


def _create_run_log(issue_id: str, branch: BranchInfo,
                    target: Optional[str]) -> str:
    """Build the run log dict and persist via state._atomic_write_json.

    Mirrors state.cmd_run_start's run-log schema and adds the runner-owned
    `transitions` and `branch` fields. Does NOT transition issue state —
    the caller composes that via state._set_issue_state.
    """
    run_id = _new_run_id(issue_id)
    run: Dict[str, Any] = {
        "run_id": run_id,
        "issue_id": state._redact_evidence(issue_id),
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,
        "agent": "pm",  # P3 starts in pm-review; dev agent takes over in P4
        "attempt": 1,
        "evidence": [],
        "transitions": [],
        "branch": branch.to_dict(),
        # Top-level mirror of branch.worktree_path (AC-WT-009). None when
        # BRANCH_SKIPPED or stale; teardown reads this to know where to
        # `git worktree remove`.
        "worktree_path": branch.worktree_path,
    }
    # SPEC-008: inject the last 20 decision lines as PM context, so the
    # scope-clarification phase sees what was recently decided/rejected.
    tail = state._read_decisions_tail(20, target)
    if tail:
        run["context"] = {"recent_decisions": tail}
    state._atomic_write_json(_run_log_path(run_id, target), run)
    return run_id


def _append_run_history_to_issue(issue_id: str, line: str,
                                 target: Optional[str]) -> None:
    """Append a redacted one-liner under the issue file's Run History field.

    Tolerates absence of the section — issue file display is a secondary
    concern; the run log's `transitions` array is authoritative.
    """
    path = os.path.join(state._issues_dir(target), f"{issue_id}.md")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    redacted = state._redact_evidence(line)
    # Find the Run History header and append after the last non-blank line
    # within that section (before the next `## ` heading or EOF).
    lines = text.splitlines()
    out: list = []
    inserted = False
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if not inserted and lines[i].strip().lower().startswith("## run history") \
                or (not inserted and lines[i].strip().lower().startswith("run history:")):
            # Collect section body.
            j = i + 1
            body: list = []
            while j < len(lines) and not lines[j].lstrip().startswith("## "):
                body.append(lines[j])
                j += 1
            body.append(f"- {redacted}")
            out.extend(body)
            i = j
            inserted = True
            continue
        i += 1
    if not inserted:
        # Section not found; append a minimal one.
        if out and out[-1].strip():
            out.append("")
        out.append("## Run History")
        out.append(f"- {redacted}")
    state._atomic_write_text(path, "\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    issue_id = args.issue_id
    tasks = state._load_tasks(args.target)
    current = tasks.get(issue_id, {}).get("status")
    if current is None:
        print(f"start failed: issue not found: {issue_id}", file=sys.stderr)
        return 1
    if current != "approved":
        print(f"start failed: {issue_id} is {current}, expected approved",
              file=sys.stderr)
        return 2

    # Acquire the run lock; held until runner.py end.
    ok, reason = state.acquire_lock(issue_id, target=args.target)
    if not ok:
        print(f"start failed: locked: {reason}", file=sys.stderr)
        return 3

    try:
        branch = _setup_branch(issue_id, args.target)
        # Stale branch (AC-WT-004): branch exists but is behind main. Halt
        # start without creating a worktree, without transitioning state, and
        # release the lock so the human can resolve (rebase, delete branch).
        if branch.status == "stale":
            state.release_lock(issue_id, target=args.target)
            print(f"BRANCH_STALE:{issue_id}: rebase onto main or delete branch")
            return EXIT_BRANCH_STALE
        run_id = _create_run_log(issue_id, branch, args.target)
        # State transition approved -> pm-review (SPEC-002 normal flow).
        state._set_issue_state(issue_id, "pm-review", target=args.target,
                               run_id=run_id, attempt=1)
        _append_run_history_to_issue(
            issue_id, f"run {run_id} start approved -> pm-review ({branch.status})",
            args.target)
    except Exception as exc:
        # Release the lock on setup/write failure so the issue isn't stranded.
        state.release_lock(issue_id, target=args.target)
        print(f"start failed: {exc}", file=sys.stderr)
        return 1

    branch_note = branch.name
    if branch.status == "skipped":
        branch_note = f"BRANCH_SKIPPED:{branch.reason}"
    print(f"Laplace result: run started")
    print(f"\nIssue: {issue_id}")
    print(f"State: approved -> pm-review")
    print(f"\nEvidence:")
    print(f"  - run log: {_run_log_path(run_id, args.target)}")
    print(f"  - branch: {branch_note}")
    if branch.worktree_path:
        print(f"  - worktree: {branch.worktree_path}")
    print(f"\nArtifacts:")
    print(f"  - .harness/state/runs/{run_id}.json")
    print(f"\nNext:")
    print(f"  /laplace:run  (PM phase: clarify scope/AC, then advance ready-for-dev)")
    return 0


def _run_has_test_evidence(run_id: str, target: Optional[str]) -> bool:
    """True iff the run log has at least one evidence entry with kind=='test'."""
    run = state._read_json(_run_log_path(run_id, target), default=None)
    if not isinstance(run, dict):
        return False
    return any(e.get("kind") == "test" for e in run.get("evidence", []))


def _run_has_evidence_kind(run_id: str, kind: str,
                           target: Optional[str]) -> bool:
    """True iff the run log has at least one evidence entry with kind==`kind`.

    Generalization of `_run_has_test_evidence` for SPEC-003 per-type gates.
    """
    run = state._read_json(_run_log_path(run_id, target), default=None)
    if not isinstance(run, dict):
        return False
    return any(e.get("kind") == kind for e in run.get("evidence", []))


# States that require prior test evidence in the run log (AC-LP-008).
TEST_EVIDENCE_REQUIRED_TARGETS = ("review-passed",)


def _load_evidence_requirements(target: Optional[str]) -> Dict[str, Dict[str, List[str]]]:
    """Parse `evidence_requirements:` block from routing-rules.yml.

    Returns {type: {from_state: [kind, ...]}}. Missing types inherit nothing.
    stdlib-only line parser; tolerates the block being absent (returns {}).
    On any parse error returns {} and lets DEFAULT_EVIDENCE_REQUIREMENTS
    apply via `_evidence_required_for`.
    """
    path = os.path.join(state._harness_root(target), ".harness",
                        "routing-rules.yml")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}

    out: Dict[str, Dict[str, List[str]]] = {}
    in_block = False
    block_indent: Optional[int] = None
    cur_type: Optional[str] = None
    cur_type_indent: Optional[int] = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_block = stripped == "evidence_requirements:"
            block_indent = None
            cur_type = None
            cur_type_indent = None
            continue
        if not in_block:
            continue
        if block_indent is None:
            block_indent = indent
        # Type row: "<type>:" at block indent, optionally "<type>: {}" empty.
        if indent == block_indent:
            base = stripped
            if base.endswith("{}"):
                base = base[:-2].rstrip()
            if base.endswith(":"):
                cur_type = base[:-1].strip()
                if cur_type:
                    out.setdefault(cur_type, {})
                cur_type_indent = None
                continue
            cur_type = None
            continue
        # Nested under a type.
        if cur_type is None:
            continue
        if cur_type_indent is None:
            cur_type_indent = indent
        if indent != cur_type_indent:
            continue
        # "from_state: [k1, k2]" or "from_state: []"
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        from_state = key.strip()
        raw_list = value.strip()
        kinds: List[str] = []
        if raw_list.startswith("[") and raw_list.endswith("]"):
            inner = raw_list[1:-1]
            for tok in inner.split(","):
                tok = tok.strip().strip('"').strip("'").strip()
                if tok:
                    kinds.append(tok)
        if from_state:
            out[cur_type][from_state] = kinds
    return out


def _evidence_required_for(rtype: str, from_state: str,
                           target: Optional[str]) -> List[str]:
    """Resolve the evidence kinds required to exit `from_state` for `rtype`.

    Precedence: routing-rules.yml `evidence_requirements:` > built-in
    DEFAULT_EVIDENCE_REQUIREMENTS. Unknown type or state -> no requirement.
    """
    if not rtype:
        return []
    file_map = _load_evidence_requirements(target)
    # File takes precedence if it declares the type at all; otherwise fall
    # back to built-in defaults. A type declared as `{}` in the file means
    # "intentionally no requirements" and overrides the default.
    if rtype in file_map:
        return list(file_map[rtype].get(from_state, []))
    builtin = DEFAULT_EVIDENCE_REQUIREMENTS.get(rtype, {})
    return list(builtin.get(from_state, []))


def _issue_routing_type(issue_id: str, target: Optional[str]) -> str:
    """Best-effort routing.type lookup for an issue, from its .md file.

    Returns '' on any failure (missing file, missing section, parse error).
    Reuses `_parse_issue_for_security` which already extracts routing.type.
    """
    path = os.path.join(state._issues_dir(target), f"{issue_id}.md")
    if not os.path.exists(path):
        return ""
    try:
        meta = _parse_issue_for_security(path)
    except Exception:
        return ""
    routing = meta.get("routing") or {}
    return str(routing.get("type", "")).strip().lower()


def _required_evidence_for_advance(issue_id: str, from_state: str,
                                   target: Optional[str]) -> List[str]:
    """SPEC-003 gate resolver: evidence kinds required to advance out of
    `from_state` for this issue's routing type."""
    rtype = _issue_routing_type(issue_id, target)
    return _evidence_required_for(rtype, from_state, target)




def cmd_advance(args: argparse.Namespace) -> int:
    issue_id = args.issue_id
    tasks = state._load_tasks(args.target)
    meta = tasks.get(issue_id)
    if not meta:
        print(f"advance failed: issue not found: {issue_id}", file=sys.stderr)
        return 1
    current = meta.get("status")
    if current != args.from_state:
        print(f"advance failed: {issue_id} is {current}, expected {args.from_state}",
              file=sys.stderr)
        return 2

    ok, reason = state.validate_transition(args.from_state, args.to_state)
    if not ok:
        print(f"advance failed: {reason}", file=sys.stderr)
        return 2

    # AC-LP-010: fix-attempt limits. Dev fix cycle (review->needs-fix) bounded
    # by MAX_FIX_ATTEMPTS; security fix cycle (security-review->needs-fix)
    # bounded by MAX_SECURITY_FIX_ATTEMPTS. Reject with exit 5 when the limit
    # would be exceeded; orchestrator routes to `blocked` or
    # `human-approval-required`. Counter persists in tasks.json across the
    # needs-fix->in-progress->review round trip because _set_issue_state
    # preserves existing task keys on reload.
    fix_bumped = False
    if args.from_state == "review" and args.to_state == "needs-fix":
        cur = int(meta.get("fix_attempts", 0))
        if cur >= MAX_FIX_ATTEMPTS:
            print(
                f"advance failed: {issue_id} fix_attempts {cur} >= max "
                f"{MAX_FIX_ATTEMPTS}; transition to blocked or "
                f"human-approval-required instead",
                file=sys.stderr,
            )
            return EXIT_FIX_LIMIT_EXCEEDED
        meta["fix_attempts"] = cur + 1
        fix_bumped = True
    elif args.from_state == "security-review" and args.to_state == "needs-fix":
        cur = int(meta.get("security_fix_attempts", 0))
        if cur >= MAX_SECURITY_FIX_ATTEMPTS:
            print(
                f"advance failed: {issue_id} security_fix_attempts {cur} >= "
                f"max {MAX_SECURITY_FIX_ATTEMPTS}; transition to "
                f"human-approval-required instead",
                file=sys.stderr,
            )
            return EXIT_FIX_LIMIT_EXCEEDED
        meta["security_fix_attempts"] = cur + 1
        fix_bumped = True

    if fix_bumped:
        # Persist counter bump before _set_issue_state. _set_issue_state
        # reloads tasks from disk and preserves existing keys, so the bump
        # survives the state change.
        state._save_tasks(tasks, target=args.target)

    # Phase 2: Auditor integration (independent validation)
    # Check auditor gates before transition - auditors can block transitions
    # with FAIL verdicts. Fresh context prevents bias.
    run_id = meta.get("run_id")
    auditor_ok, auditor_reason = _check_auditor_gate(
        args.from_state, args.to_state, issue_id, args.target, run_id
    )
    if not auditor_ok:
        print(
            f"advance failed: auditor gate blocked transition: {auditor_reason}",
            file=sys.stderr,
        )
        return 4  # Use exit code 4 for auditor block (same as evidence gate)

    # Phase 4: Evidence-driven completion gates
    # Check evidence requirements before transition - required evidence kinds
    # must be present in the run log.
    if run_id:
        evidence_ok, evidence_reason = state.check_evidence_gate(
            run_id, args.from_state, args.to_state, args.target
        )
        if not evidence_ok:
            print(
                f"advance failed: evidence gate blocked transition: {evidence_reason}",
                file=sys.stderr,
            )
            return 4  # Use exit code 4 for evidence gate block

    # SPEC-006: cost watcher. Two integration points:
    # 1. security-review -> review-passed when cost_watcher.enabled: redirect
    #    to cost-review first (transparent to callers). Done here, BEFORE the
    #    AC-LP-008 test-evidence gate, so the redirect is not blocked by the
    #    review-passed evidence check (which applies on the later
    #    cost-review -> review-passed hop).
    # 2. cost-review -> review-passed: invoke cost_review.decide. On block,
    #    cost_review moves the issue to human-approval-required and returns 4.
    if args.from_state == "security-review" and args.to_state == "review-passed":
        cfg = state.load_config(args.target)
        if (cfg.get("cost_watcher") or {}).get("enabled"):
            state._set_issue_state(issue_id, "cost-review", target=args.target)
            print(f"advance: cost_watcher enabled -> routed to cost-review "
                  f"(from security-review) for {issue_id}", file=sys.stderr)
            return 0

    # AC-LP-008: review-passed requires at least one test-evidence entry in
    # the run log. Enforced here so the gate cannot be bypassed by the skill
    # or agent; the model cannot self-declare review-passed without evidence.
    if args.to_state in TEST_EVIDENCE_REQUIRED_TARGETS:
        run_id = meta.get("run_id")
        if not run_id or not _run_has_test_evidence(run_id, args.target):
            print(
                f"advance failed: {args.to_state} requires test evidence in run log "
                f"(run_id={run_id}); capture via `runner.py evidence <run> test "
                f"<test-output-path>` before retrying",
                file=sys.stderr,
            )
            return 4

    # SPEC-003: per-type evidence requirements on the FROM state. The issue's
    # routing type (parsed from its .md file) selects a {from_state: [kind]}
    # map; each listed kind must appear in the run log before the transition
    # proceeds. Missing type or empty map -> no extra requirement (regression
    # guard: feature path unchanged).
    required_kinds = _required_evidence_for_advance(issue_id, args.from_state,
                                                    args.target)
    if required_kinds:
        run_id = meta.get("run_id")
        missing = [k for k in required_kinds
                   if not run_id or not _run_has_evidence_kind(run_id, k, args.target)]
        if missing:
            print(
                f"advance failed: type={_issue_routing_type(issue_id, args.target) or '?'} "
                f"requires evidence kinds {required_kinds} at {args.from_state} exit; "
                f"missing {missing} (run_id={run_id}); capture via "
                f"`runner.py evidence <run> <kind> <path-or-text>`",
                file=sys.stderr,
            )
            return 4

    # SPEC-006 (cont.): cost-review -> review-passed invokes the cost watcher
    # decision. On block, cost_review moves the issue to
    # human-approval-required and returns 4. On pass/warn, fall through so
    # AC-LP-008 (already checked above) and the normal set-state run.
    if args.from_state == "cost-review" and args.to_state == "review-passed":
        import cost_review  # local import; same-scripts dir
        rc = cost_review.cmd_review(argparse.Namespace(
            issue_id=issue_id, target=args.target))
        if rc != 0:
            return rc

    state._set_issue_state(issue_id, args.to_state, target=args.target)

    # Append transition entry to run log + issue Run History.
    run_id = meta.get("run_id")
    summary = state._redact_evidence(args.summary or "")
    entry = {"ts": time.time(), "from": args.from_state, "to": args.to_state,
             "summary": summary}
    if run_id:
        rpath = _run_log_path(run_id, args.target)
        run = state._read_json(rpath, default=None)
        if isinstance(run, dict):
            run.setdefault("transitions", []).append(entry)
            state._atomic_write_json(rpath, run)
    hist_line = f"run {run_id or '?'} advance {args.from_state} -> {args.to_state}"
    if summary:
        hist_line += f" :: {summary}"
    _append_run_history_to_issue(issue_id, hist_line, args.target)

    # SPEC-008: append decision-worthy transitions to memory/decisions.md.
    # Uses the post-redaction `summary` local (audit Q1: redaction happens
    # AFTER _set_issue_state, so this MUST sit below line 788 to avoid
    # leaking raw secrets).
    if (args.from_state, args.to_state) in state.DECISION_WORTHY_TRANSITIONS:
        verdict = state._DECISION_VERDICT.get(args.to_state, args.to_state)
        state._append_decision(issue_id, args.from_state, verdict,
                               summary, target=args.target)

    print(f"Laplace result: advanced")
    print(f"\nIssue: {issue_id}")
    print(f"State: {args.from_state} -> {args.to_state}")
    print(f"\nEvidence:")
    print(f"  - transition legal per state machine")
    if summary:
        print(f"  - summary: {summary}")
    print(f"\nNext:")
    print(f"  (see /laplace:run skill for phase-specific next action)")
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    if args.kind not in ALLOWED_EVIDENCE_KINDS:
        print(f"evidence failed: kind must be one of {ALLOWED_EVIDENCE_KINDS}",
              file=sys.stderr)
        return 2

    rpath = _run_log_path(args.run_id, args.target)
    run = state._read_json(rpath, default=None)
    if not isinstance(run, dict):
        print(f"evidence failed: run not found: {args.run_id}", file=sys.stderr)
        return 1

    source_path: Optional[str] = None
    # Treat the argument as a file path if it exists on disk; else as text.
    candidate = args.path_or_text
    if candidate and os.path.isfile(candidate):
        source_path = candidate
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(EVIDENCE_SUMMARY_MAX + 1)
        except OSError as exc:
            print(f"evidence failed: cannot read {candidate}: {exc}",
                  file=sys.stderr)
            return 1
    else:
        raw = candidate

    summary = state._redact_evidence(raw[:EVIDENCE_SUMMARY_MAX])
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "kind": args.kind,
        "summary": summary,
    }
    if source_path:
        entry["source_path"] = source_path

    run.setdefault("evidence", []).append(entry)
    state._atomic_write_json(rpath, run)

    print(f"Laplace result: evidence recorded")
    print(f"\nRun: {args.run_id}")
    print(f"Kind: {args.kind}")
    print(f"\nEvidence:")
    if source_path:
        print(f"  - source: {source_path}")
    print(f"  - summary ({len(summary)} chars, redacted)")
    print(f"\nNext:")
    print(f"  (capture more evidence or advance state)")
    return 0


# Phase 4: Extended evidence capture functions for new evidence kinds

def capture_spec_validation(run_id: str, spec_path: str,
                            target: Optional[str] = None) -> Tuple[bool, str]:
    """Capture SPEC document validation evidence.

    Args:
        run_id: Run identifier
        spec_path: Path to SPEC file
        target: Repository root

    Returns:
        (ok, reason) tuple
    """
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec_content = f.read()

        # Basic validation: check YAML frontmatter
        has_frontmatter = spec_content.strip().startswith("---")
        has_gears_markers = any(
            marker in spec_content.lower()
            for marker in ["shall", "shall not", "where", "when", "while"]
        )

        summary = f"SPEC validation: frontmatter={has_frontmatter}, GEARS={has_gears_markers}"

        # Record as evidence
        ns = argparse.Namespace(
            run_id=run_id,
            kind="spec-validation",
            summary=summary,
            source_path=spec_path,
            target=target,
        )
        return cmd_evidence(ns) == 0, summary

    except Exception as e:
        return False, f"SPEC validation failed: {e}"


def capture_workflow_plan(run_id: str, plan: Dict[str, Any],
                         target: Optional[str] = None) -> Tuple[bool, str]:
    """Capture workflow plan as evidence.

    Args:
        run_id: Run identifier
        plan: Workflow plan dictionary
        target: Repository root

    Returns:
        (ok, reason) tuple
    """
    try:
        # Serialize plan as JSON summary
        plan_summary = json.dumps(plan, indent=2)
        steps_count = len(plan.get("steps", []))
        gates_count = len(plan.get("gates", []))

        summary = f"Workflow plan: {steps_count} steps, {gates_count} gates"

        # Record as evidence
        ns = argparse.Namespace(
            run_id=run_id,
            kind="workflow-plan",
            summary=summary,
            source_path=None,  # Plan is in memory
            target=target,
        )
        return cmd_evidence(ns) == 0, summary

    except Exception as e:
        return False, f"Workflow plan capture failed: {e}"


def capture_metrics(run_id: str, metrics: Dict[str, Any],
                    target: Optional[str] = None) -> Tuple[bool, str]:
    """Capture performance/metric measurements as evidence.

    Args:
        run_id: Run identifier
        metrics: Metrics dictionary
        target: Repository root

    Returns:
        (ok, reason) tuple
    """
    try:
        # Format metrics as summary
        metric_lines = [f"{k}: {v}" for k, v in metrics.items()]
        summary = f"Metrics: {', '.join(metric_lines[:5])}"  # Limit to 5 metrics

        # Record as evidence
        ns = argparse.Namespace(
            run_id=run_id,
            kind="metric-capture",
            summary=summary,
            source_path=None,
            target=target,
        )
        return cmd_evidence(ns) == 0, summary

    except Exception as e:
        return False, f"Metrics capture failed: {e}"


def capture_integration_test(run_id: str, test_output: str,
                            target: Optional[str] = None) -> Tuple[bool, str]:
    """Capture integration test results as evidence.

    Args:
        run_id: Run identifier
        test_output: Test output string
        target: Repository root

    Returns:
        (ok, reason) tuple
    """
    try:
        # Parse test output for summary
        lines = test_output.strip().split("\n")
        passed = sum(1 for line in lines if "PASS" in line or "pass" in line.lower())
        failed = sum(1 for line in lines if "FAIL" in line or "fail" in line.lower())

        summary = f"Integration test: {passed} passed, {failed} failed"

        # Record as evidence
        ns = argparse.Namespace(
            run_id=run_id,
            kind="integration-test",
            summary=summary,
            source_path=None,
            target=target,
        )
        return cmd_evidence(ns) == 0, summary

    except Exception as e:
        return False, f"Integration test capture failed: {e}"


def cmd_end(args: argparse.Namespace) -> int:
    # Worktree teardown happens BEFORE state.cmd_run_end (AC-WT-003/008).
    # Read the run log to find the worktree path. If the worktree is dirty
    # and --force-worktree-remove is not set, halt with WORKTREE_DIRTY and
    # leave the run lock held (state.cmd_run_end is NOT called, so the run
    # is not finalized).
    run_log = state._read_json(
        _run_log_path(args.run_id, args.target), default=None)
    issue_id = None
    if isinstance(run_log, dict):
        issue_id = run_log.get("issue_id")
    force = bool(getattr(args, "force_worktree_remove", False))
    wt_rc, wt_status = _teardown_worktree(
        issue_id, args.target, run_log, force=force)
    # Persist the teardown status into the run log (AC-WT-009) when we can.
    if isinstance(run_log, dict):
        run_log["worktree_teardown"] = wt_status
        state._atomic_write_json(_run_log_path(args.run_id, args.target), run_log)
    if wt_rc != 0:
        # Dirty worktree halt (AC-WT-008). Lock stays held; run NOT finalized.
        print(f"WORKTREE_DIRTY:{issue_id}: uncommitted changes in worktree; "
              f"re-run with --force-worktree-remove to discard",
              file=sys.stderr)
        return wt_rc

    # Delegate finalization + lock release to state.cmd_run_end. It reads the
    # run log, sets ended_at + outcome, and releases the issue lock.
    ns = argparse.Namespace(
        run_id=args.run_id, outcome=args.outcome, evidence=None,
        target=args.target,
    )
    rc = state.cmd_run_end(ns)
    if rc != 0:
        return rc
    print(f"\nArtifacts:")
    print(f"  - .harness/state/runs/{args.run_id}.json")
    print(f"\nNext:")
    print(f"  /laplace:status  (or /laplace:run <next-issue>)")
    return 0


# ---------------------------------------------------------------------------
# Security-review trigger (AC-LP-009)
# ---------------------------------------------------------------------------

def _glob_to_regex(glob: str) -> "re.Pattern[str]":
    """Translate a glob with `**` support into a compiled regex.

    Semantics:
      `**/` matches zero or more path segments (any depth, including none)
      `**`  (not followed by /) matches any characters including separators
      `*`   matches any characters except `/`
      `?`   matches a single non-slash character
    """
    out: List[str] = ["^"]
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            # Check for `**`
            if i + 1 < n and glob[i + 1] == "*":
                # `**/` -> match zero or more leading path segments
                if i + 2 < n and glob[i + 2] == "/":
                    out.append(r"(?:.*/)?")
                    i += 3
                    continue
                # `**` at end or before non-slash -> match anything (incl. /)
                out.append(r".*")
                i += 2
                continue
            # Single `*` -> match anything except `/`
            out.append(r"[^/]*")
            i += 1
            continue
        if c == "?":
            out.append(r"[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


# Pre-compiled regexes for SECURITY_PATH_PATTERNS (compiled once at import).
_SECURITY_PATH_REGEXES: List[Tuple[str, "re.Pattern[str]"]] = [
    (pat, _glob_to_regex(pat)) for pat in SECURITY_PATH_PATTERNS
]


def _path_matches_security_pattern(path: str) -> Optional[str]:
    """Return the matched pattern if `path` hits a security path trigger,
    else None. Handles `**` globstar semantics."""
    norm = path.replace("\\", "/")
    # Strip a single leading "./" prefix only (do NOT strip leading dots from
    # hidden paths like `.github/...` or `.mcp.json`).
    if norm.startswith("./"):
        norm = norm[2:]
    for pat, regex in _SECURITY_PATH_REGEXES:
        if regex.match(norm):
            return pat
    return None


def requires_security_review(issue_meta: Dict[str, Any],
                             diff_stats: Optional[Dict[str, Any]]) \
        -> Tuple[bool, List[str]]:
    """Decide whether a change requires a security review (AC-LP-009).

    issue_meta keys (all optional, best-effort):
        risk.security_sensitivity: low|medium|high
        risk.risk_level: low|medium|high
        routing.type: feature|bug|chore|security|...

    diff_stats keys (all optional):
        paths: list[str] of file paths in the diff
        text:  concatenated diff text for external-API substring scan

    Returns (required, triggers). Conservative: on any parse ambiguity in
    the caller (issue file unparseable), the caller should treat the issue
    as security-sensitive and pass risk.security_sensitivity='high'.
    """
    triggers: List[str] = []
    if diff_stats is None:
        diff_stats = {}
    risk = issue_meta.get("risk") or {}
    routing = issue_meta.get("routing") or {}
    sens = str(risk.get("security_sensitivity", "")).strip().lower()
    rlevel = str(risk.get("risk_level", "")).strip().lower()
    rtype = str(routing.get("type", "")).strip().lower()

    # Security sensitivity declared medium/high -> always trigger.
    if sens in {"medium", "high"}:
        triggers.append(f"security_sensitivity={sens}")
    # Risk level high -> trigger.
    if rlevel == "high":
        triggers.append("risk_level=high")

    paths = diff_stats.get("paths") or []
    # Routing type security -> always trigger.
    if rtype == "security":
        triggers.append("routing.type=security")
    # Routing type chore -> trigger only if diff touches workflow/script files.
    chore_sensitive = False
    if rtype == "chore":
        for p in paths:
            if _path_matches_security_pattern(p):
                chore_sensitive = True
                break
        if chore_sensitive:
            triggers.append("routing.type=chore+config-paths")

    # Path-trigger scan (applies to any routing type).
    for p in paths:
        hit = _path_matches_security_pattern(p)
        if hit:
            tag = f"path:{hit}:{p}"
            if tag not in triggers:
                triggers.append(tag)

    # External-API substring scan on diff text.
    text = diff_stats.get("text") or ""
    if text:
        for marker in EXTERNAL_API_MARKERS:
            if marker in text:
                triggers.append(f"external-api:{marker}")
                break
        # Best-effort: external URL string that's not localhost/relative.
        if re.search(r"https?://(?!localhost|127\.0\.0\.1)[^\s\"'<>)]+", text):
            triggers.append("external-api:url-string")

    return (len(triggers) > 0, triggers)


# --- Issue-file parsing for security-check -----------------------------------

_RISK_SECTION_RE = re.compile(
    r"(?im)^\s*##\s*Risk\s*/\s*Release Impact\s*$")
_ROUTING_SECTION_RE = re.compile(
    r"(?im)^\s*##\s*Routing Metadata\s*$")
_NEXT_SECTION_RE = re.compile(r"(?im)^\s*##\s+")
_FIELD_RE = re.compile(r"^\s*-?\s*([A-Za-z /]+?):\s*(.+?)\s*$")


def _extract_section(text: str, header_re: "re.Pattern[str]") -> str:
    """Return the body of a `## <Header>` section (up to the next `## ` heading)."""
    m = header_re.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = _NEXT_SECTION_RE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def _field_in_section(section: str, label: str) -> str:
    """Find `- Label: value` (or `Label: value`) within a section body."""
    target = label.lower()
    for line in section.splitlines():
        m = _FIELD_RE.match(line)
        if m and m.group(1).strip().lower() == target:
            return m.group(2).strip()
    return ""


def _parse_issue_for_security(issue_path: str) -> Dict[str, Any]:
    """Parse risk + routing fields from a Laplace issue file.

    Returns a dict with shape:
        {risk: {security_sensitivity, risk_level}, routing: {type}, parse_ok: bool}

    Missing fields default to empty string; parse_ok=False if either section
    is absent (caller applies conservative defaults).
    """
    meta: Dict[str, Any] = {"risk": {}, "routing": {}, "parse_ok": True}
    try:
        with open(issue_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        meta["parse_ok"] = False
        return meta

    risk_body = _extract_section(text, _RISK_SECTION_RE)
    routing_body = _extract_section(text, _ROUTING_SECTION_RE)
    if not risk_body and not routing_body:
        meta["parse_ok"] = False
    meta["risk"]["security_sensitivity"] = _field_in_section(
        risk_body, "Security Sensitivity")
    meta["risk"]["risk_level"] = _field_in_section(risk_body, "Risk Level")
    meta["routing"]["type"] = _field_in_section(routing_body, "Type")
    return meta


def _parse_diff_paths(diff_text: str) -> List[str]:
    """Extract file paths from a unified diff. Handles both
    `diff --git a/X b/X` and `+++ b/X` forms."""
    paths: List[str] = []
    seen = set()
    for line in diff_text.splitlines():
        m = re.match(r"^\+\+\+\s+b/(.+)$", line)
        if m:
            p = m.group(1).strip()
            if p != "/dev/null" and p not in seen:
                seen.add(p)
                paths.append(p)
            continue
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if m:
            p = m.group(2).strip()
            if p != "/dev/null" and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def cmd_security_check(args: argparse.Namespace) -> int:
    """Advisory security-review trigger check (AC-LP-009). Always exits 0;
    prints `required: true|false` + trigger list. The orchestrator (skill)
    decides whether to transition `review -> security-review`."""
    issue_path = os.path.join(state._issues_dir(args.target),
                              f"{args.issue_id}.md")
    if not os.path.exists(issue_path):
        print(f"security-check failed: issue not found: {args.issue_id}",
              file=sys.stderr)
        return 1

    meta = _parse_issue_for_security(issue_path)
    # Conservative default on parse failure: treat as security-sensitive.
    if not meta.get("parse_ok", True):
        meta.setdefault("risk", {})
        meta["risk"]["security_sensitivity"] = "high"

    diff_stats: Dict[str, Any] = {"paths": [], "text": ""}
    if args.diff:
        if not os.path.isfile(args.diff):
            print(f"security-check failed: diff file not found: {args.diff}",
                  file=sys.stderr)
            return 1
        try:
            with open(args.diff, "r", encoding="utf-8", errors="replace") as f:
                diff_text = f.read()
        except OSError as exc:
            print(f"security-check failed: cannot read diff: {exc}",
                  file=sys.stderr)
            return 1
        diff_stats["paths"] = _parse_diff_paths(diff_text)
        diff_stats["text"] = diff_text

    required, triggers = requires_security_review(meta, diff_stats)
    print(f"required: {'true' if required else 'false'}")
    if triggers:
        print("triggers:")
        for t in triggers:
            print(f"  - {t}")
    else:
        print("triggers: []")
    print(f"\nNext:")
    if required:
        print(f"  advance {args.issue_id} review security-review")
    else:
        print(f"  advance {args.issue_id} review review-passed "
              f"(requires test evidence per AC-LP-008)")
    return 0


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    import shutil

    failures: list = []
    tmp = tempfile.mkdtemp(prefix="laplace-runner-selftest-")

    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        # Bootstrap: init harness + seed a draft issue.
        if state.cmd_init(target=tmp) != 0:
            failures.append("state.cmd_init returned non-zero")
        state._save_tasks(
            {"ISSUE-0001": {"status": "draft", "updated_at": time.time()}},
            target=tmp,
        )
        q = state._load_queue(target=tmp)
        q["draft"].append("ISSUE-0001")
        state._save_queue(q, target=tmp)
        # Mark the fixture dir as non-repo: ensure no .git anywhere up the tree
        # affects detection. tempfile.mkdtemp is outside any repo by default.

        # approve draft -> approved
        ns_ap = argparse.Namespace(issue_id="ISSUE-0001", user="tester", target=tmp)
        if state.cmd_approve(ns_ap) != 0:
            failures.append("cmd_approve returned non-zero")

        # start: approved -> pm-review, run log created, BRANCH_SKIPPED recorded
        ns_st = argparse.Namespace(issue_id="ISSUE-0001", target=tmp)
        rc = cmd_start(ns_st)
        if rc != 0:
            failures.append(f"start returned {rc}")
        tasks = state._load_tasks(target=tmp)
        if tasks.get("ISSUE-0001", {}).get("status") != "pm-review":
            failures.append(
                f"start did not transition to pm-review: {tasks.get('ISSUE-0001')}"
            )
        runs = [f for f in os.listdir(state._runs_dir(target=tmp))
                if f.endswith(".json")]
        if len(runs) != 1:
            failures.append(f"expected 1 run log, got {runs}")
        rid = runs[0][:-len(".json")] if runs else ""
        run = state._read_json(_run_log_path(rid, tmp)) if rid else None
        if not isinstance(run, dict):
            failures.append("run log missing or not a dict")
        else:
            binfo = run.get("branch", {})
            if binfo.get("status") != "skipped" \
                    or "not-a-git-repo" not in binfo.get("reason", ""):
                failures.append(
                    f"BRANCH_SKIPPED:not-a-git-repo not recorded: {binfo}"
                )

        # advance: legal transition pm-review -> ready-for-dev
        ns_adv_ok = argparse.Namespace(
            issue_id="ISSUE-0001", from_state="pm-review",
            to_state="ready-for-dev", summary="AC clarified; ready",
            target=tmp,
        )
        if cmd_advance(ns_adv_ok) != 0:
            failures.append("legal advance pm-review -> ready-for-dev failed")
        if state._load_tasks(target=tmp).get("ISSUE-0001", {}).get("status") \
                != "ready-for-dev":
            failures.append("legal advance did not change state")

        # advance: illegal transition ready-for-dev -> review (must go via in-progress)
        ns_adv_bad = argparse.Namespace(
            issue_id="ISSUE-0001", from_state="ready-for-dev",
            to_state="review", summary="", target=tmp,
        )
        rc_bad = cmd_advance(ns_adv_bad)
        if rc_bad == 0:
            failures.append("illegal advance ready-for-dev -> review unexpectedly succeeded")

        # AC-LP-008: review-passed requires test evidence in run log.
        # Drive ISSUE-0001 forward: ready-for-dev -> in-progress -> review.
        for src, dst in (("ready-for-dev", "in-progress"), ("in-progress", "review")):
            ns = argparse.Namespace(
                issue_id="ISSUE-0001", from_state=src, to_state=dst,
                summary="", target=tmp,
            )
            if cmd_advance(ns) != 0:
                failures.append(f"advance {src} -> {dst} failed")

        # review -> review-passed WITHOUT test evidence: must fail with code 4.
        ns_no_ev = argparse.Namespace(
            issue_id="ISSUE-0001", from_state="review", to_state="review-passed",
            summary="", target=tmp,
        )
        rc_no_ev = cmd_advance(ns_no_ev)
        if rc_no_ev != 4:
            failures.append(
                f"review -> review-passed without test evidence should fail with "
                f"code 4, got {rc_no_ev}"
            )

        # Capture test evidence, then review-passed succeeds.
        ns_ev_test = argparse.Namespace(
            run_id=rid, kind="test",
            path_or_text="pytest: 5 passed", target=tmp,
        )
        if cmd_evidence(ns_ev_test) != 0:
            failures.append("evidence(test) for AC-LP-008 returned non-zero")
        ns_pass = argparse.Namespace(
            issue_id="ISSUE-0001", from_state="review", to_state="review-passed",
            summary="tests green; AC met", target=tmp,
        )
        if cmd_advance(ns_pass) != 0:
            failures.append("review -> review-passed with test evidence failed")
        if state._load_tasks(target=tmp).get("ISSUE-0001", {}).get("status") \
                != "review-passed":
            failures.append("review-passed state not set after evidence-backed advance")

        # evidence: text with fake bearer token — must be redacted
        ns_ev_text = argparse.Namespace(
            run_id=rid, kind="review",
            path_or_text="Authorization: Bearer " + "a" * 24,
            target=tmp,
        )
        if cmd_evidence(ns_ev_text) != 0:
            failures.append("evidence(text) returned non-zero")
        run = state._read_json(_run_log_path(rid, tmp))
        if "a" * 24 in json.dumps(run):
            failures.append("evidence text not redacted in run log")

        # evidence: file path — reads + redacts + stores summary + source_path
        ev_file = os.path.join(tmp, "test-output.txt")
        with open(ev_file, "w", encoding="utf-8") as f:
            f.write("pytest: 12 passed\nDB_PASSWORD=hunter2\n")
        ns_ev_file = argparse.Namespace(
            run_id=rid, kind="test", path_or_text=ev_file, target=tmp,
        )
        if cmd_evidence(ns_ev_file) != 0:
            failures.append("evidence(file) returned non-zero")
        run = state._read_json(_run_log_path(rid, tmp))
        ev_entries = run.get("evidence", []) if isinstance(run, dict) else []
        file_entry = next((e for e in ev_entries
                           if e.get("source_path") == ev_file), None)
        if not file_entry:
            failures.append("evidence file entry missing source_path")
        if file_entry and "hunter2" in json.dumps(file_entry):
            failures.append("evidence file content not redacted")

        # evidence: bad kind rejected
        ns_ev_bad = argparse.Namespace(
            run_id=rid, kind="bogus", path_or_text="x", target=tmp,
        )
        if cmd_evidence(ns_ev_bad) == 0:
            failures.append("evidence accepted invalid kind")

        # lock: holding the issue lock before start must make cmd_start fail
        # with code 3 (locked). We hold the lock manually so the issue stays
        # in `approved` and the lock check is the first gate that fires.
        state._save_tasks(
            {"ISSUE-0002": {"status": "draft", "updated_at": time.time()}},
            target=tmp,
        )
        q2 = state._load_queue(target=tmp)
        q2["draft"].append("ISSUE-0002")
        state._save_queue(q2, target=tmp)
        state.cmd_approve(argparse.Namespace(
            issue_id="ISSUE-0002", user="tester", target=tmp))
        lok, lreason = state.acquire_lock("ISSUE-0002", target=tmp)
        if not lok:
            failures.append(f"could not pre-acquire lock for contention test: {lreason}")
        else:
            ns_lock = argparse.Namespace(issue_id="ISSUE-0002", target=tmp)
            rc_lock = cmd_start(ns_lock)
            if rc_lock != 3:
                failures.append(
                    f"start under held lock should fail with code 3, got {rc_lock}"
                )
            state.release_lock("ISSUE-0002", target=tmp)

        # end: finalizes run, releases lock
        ns_end = argparse.Namespace(run_id=rid, outcome="blocked", target=tmp)
        if cmd_end(ns_end) != 0:
            failures.append("end returned non-zero")
        run = state._read_json(_run_log_path(rid, tmp))
        if not run or run.get("outcome") != "blocked":
            failures.append(f"end did not set outcome: {run}")
        # Lock for ISSUE-0001 should be released after end.
        lok, _ = state.acquire_lock("ISSUE-0001", target=tmp)
        if not lok:
            failures.append("end did not release lock for ISSUE-0001")
        else:
            state.release_lock("ISSUE-0001", target=tmp)

        # After lock release, advance is allowed to proceed if state matches.
        # (Confirms end truly released the run lock.)

        # --- AC-LP-010: dev fix-attempt limit (review -> needs-fix) ----------
        # Drive ISSUE-0003 to `review`, then loop review->needs-fix (counter
        # bumps 1, 2, 3) with needs-fix->in-progress->review round trips in
        # between. The 4th review->needs-fix must fail with exit 5.
        state._save_tasks(
            {"ISSUE-0003": {"status": "draft", "updated_at": time.time()}},
            target=tmp,
        )
        q3 = state._load_queue(target=tmp)
        q3["draft"].append("ISSUE-0003")
        state._save_queue(q3, target=tmp)
        state.cmd_approve(argparse.Namespace(
            issue_id="ISSUE-0003", user="tester", target=tmp))
        if cmd_start(argparse.Namespace(
                issue_id="ISSUE-0003", target=tmp)) != 0:
            failures.append("start ISSUE-0003 returned non-zero")
        # Drive to review: pm-review -> ready-for-dev -> in-progress -> review.
        for src, dst in (("pm-review", "ready-for-dev"),
                         ("ready-for-dev", "in-progress"),
                         ("in-progress", "review")):
            if cmd_advance(argparse.Namespace(
                    issue_id="ISSUE-0003", from_state=src, to_state=dst,
                    summary="", target=tmp)) != 0:
                failures.append(f"ISSUE-0003 advance {src}->{dst} failed")
        # Three successful review->needs-fix cycles.
        for attempt in range(1, 4):
            ns_fix = argparse.Namespace(
                issue_id="ISSUE-0003", from_state="review",
                to_state="needs-fix", summary=f"fix#{attempt}", target=tmp,
            )
            rc = cmd_advance(ns_fix)
            if rc != 0:
                failures.append(
                    f"ISSUE-0003 review->needs-fix #{attempt} should succeed, "
                    f"got rc={rc}"
                )
            t3 = state._load_tasks(target=tmp).get("ISSUE-0003", {})
            if int(t3.get("fix_attempts", -1)) != attempt:
                failures.append(
                    f"ISSUE-0003 fix_attempts should be {attempt}, got "
                    f"{t3.get('fix_attempts')}"
                )
            # Round trip back to review for next attempt (or for the 4th try).
            for src, dst in (("needs-fix", "in-progress"),
                             ("in-progress", "review")):
                if cmd_advance(argparse.Namespace(
                        issue_id="ISSUE-0003", from_state=src, to_state=dst,
                        summary="", target=tmp)) != 0:
                    failures.append(
                        f"ISSUE-0003 round-trip {src}->{dst} failed")
        # 4th review->needs-fix must be rejected with exit 5 (AC-LP-010).
        ns_over = argparse.Namespace(
            issue_id="ISSUE-0003", from_state="review", to_state="needs-fix",
            summary="fix#4", target=tmp,
        )
        rc_over = cmd_advance(ns_over)
        if rc_over != EXIT_FIX_LIMIT_EXCEEDED:
            failures.append(
                f"ISSUE-0003 4th review->needs-fix should return exit "
                f"{EXIT_FIX_LIMIT_EXCEEDED}, got {rc_over}"
            )
        # State must remain `review` after the rejected transition.
        if state._load_tasks(target=tmp).get("ISSUE-0003", {}).get("status") \
                != "review":
            failures.append("ISSUE-0003 state changed despite exit-5 reject")
        # Orchestrator fallback: review -> blocked is allowed from review.
        ns_blocked = argparse.Namespace(
            issue_id="ISSUE-0003", from_state="review", to_state="blocked",
            summary="fix limit exceeded", target=tmp,
        )
        if cmd_advance(ns_blocked) != 0:
            failures.append("ISSUE-0003 review->blocked fallback failed")
        state.release_lock("ISSUE-0003", target=tmp)

        # --- AC-LP-010: security fix-attempt limit (security-review -> needs-fix)
        # Drive ISSUE-0004 to security-review, then loop
        # security-review->needs-fix (counter 1, 2) with round trips back to
        # security-review. The 3rd must fail with exit 5.
        state._save_tasks(
            {"ISSUE-0004": {"status": "draft", "updated_at": time.time()}},
            target=tmp,
        )
        q4 = state._load_queue(target=tmp)
        q4["draft"].append("ISSUE-0004")
        state._save_queue(q4, target=tmp)
        state.cmd_approve(argparse.Namespace(
            issue_id="ISSUE-0004", user="tester", target=tmp))
        if cmd_start(argparse.Namespace(
                issue_id="ISSUE-0004", target=tmp)) != 0:
            failures.append("start ISSUE-0004 returned non-zero")
        for src, dst in (("pm-review", "ready-for-dev"),
                         ("ready-for-dev", "in-progress"),
                         ("in-progress", "review"),
                         ("review", "security-review")):
            if cmd_advance(argparse.Namespace(
                    issue_id="ISSUE-0004", from_state=src, to_state=dst,
                    summary="", target=tmp)) != 0:
                failures.append(f"ISSUE-0004 advance {src}->{dst} failed")
        # Two successful security-review->needs-fix cycles (limit is 2).
        for attempt in range(1, 3):
            ns_sfix = argparse.Namespace(
                issue_id="ISSUE-0004", from_state="security-review",
                to_state="needs-fix", summary=f"secfix#{attempt}", target=tmp,
            )
            rc = cmd_advance(ns_sfix)
            if rc != 0:
                failures.append(
                    f"ISSUE-0004 security-review->needs-fix #{attempt} should "
                    f"succeed, got rc={rc}"
                )
            t4 = state._load_tasks(target=tmp).get("ISSUE-0004", {})
            if int(t4.get("security_fix_attempts", -1)) != attempt:
                failures.append(
                    f"ISSUE-0004 security_fix_attempts should be {attempt}, "
                    f"got {t4.get('security_fix_attempts')}"
                )
            # Round trip back to security-review.
            for src, dst in (("needs-fix", "in-progress"),
                             ("in-progress", "review"),
                             ("review", "security-review")):
                if cmd_advance(argparse.Namespace(
                        issue_id="ISSUE-0004", from_state=src, to_state=dst,
                        summary="", target=tmp)) != 0:
                    failures.append(
                        f"ISSUE-0004 round-trip {src}->{dst} failed")
        # 3rd security-review->needs-fix must be rejected with exit 5.
        ns_sover = argparse.Namespace(
            issue_id="ISSUE-0004", from_state="security-review",
            to_state="needs-fix", summary="secfix#3", target=tmp,
        )
        rc_sover = cmd_advance(ns_sover)
        if rc_sover != EXIT_FIX_LIMIT_EXCEEDED:
            failures.append(
                f"ISSUE-0004 3rd security-review->needs-fix should return "
                f"exit {EXIT_FIX_LIMIT_EXCEEDED}, got {rc_sover}"
            )
        if state._load_tasks(target=tmp).get("ISSUE-0004", {}).get("status") \
                != "security-review":
            failures.append(
                "ISSUE-0004 state changed despite security exit-5 reject")
        state.release_lock("ISSUE-0004", target=tmp)

        # --- AC-LP-009: security-check helper (pure function) ----------------
        # High sensitivity -> required=True.
        req, trig = requires_security_review(
            {"risk": {"security_sensitivity": "high",
                      "risk_level": "low"},
             "routing": {"type": "feature"}},
            None,
        )
        if not req or not any("security_sensitivity=high" in t for t in trig):
            failures.append(
                f"security-check high-sensitivity should trigger: "
                f"({req}, {trig})"
            )
        # Low/feature, no diff -> required=False.
        req, trig = requires_security_review(
            {"risk": {"security_sensitivity": "low",
                      "risk_level": "low"},
             "routing": {"type": "feature"}},
            {"paths": ["src/app/view.py"], "text": ""},
        )
        if req:
            failures.append(
                f"security-check low/feature plain path should not trigger: "
                f"({req}, {trig})"
            )
        # Feature + workflow path -> required=True (path trigger).
        req, trig = requires_security_review(
            {"risk": {"security_sensitivity": "low",
                      "risk_level": "low"},
             "routing": {"type": "feature"}},
            {"paths": [".github/workflows/ci.yml"], "text": ""},
        )
        if not req or not any(".github/workflows" in t for t in trig):
            failures.append(
                f"security-check workflow path should trigger: "
                f"({req}, {trig})"
            )
        # External API marker in diff text -> required=True.
        req, trig = requires_security_review(
            {"risk": {"security_sensitivity": "low",
                      "risk_level": "low"},
             "routing": {"type": "feature"}},
            {"paths": ["src/app/fetcher.py"],
             "text": "resp = fetch('https://api.example.com/users')"},
        )
        if not req or not any("external-api" in t for t in trig):
            failures.append(
                f"security-check external-API text should trigger: "
                f"({req}, {trig})"
            )
        # routing.type=security -> always trigger.
        req, trig = requires_security_review(
            {"risk": {"security_sensitivity": "low",
                      "risk_level": "low"},
             "routing": {"type": "security"}},
            None,
        )
        if not req or not any("routing.type=security" in t for t in trig):
            failures.append(
                f"security-check routing.type=security should trigger: "
                f"({req}, {trig})")

        # --- AC-LP-009: cmd_security_check end-to-end -----------------------
        # Write three issue files with different risk/routing fields, then call
        # cmd_security_check and verify printed output. Redirect stdout to a
        # StringIO to capture the output (default selftest stdout is devnull).
        import io
        issue_high = (
            "# ISSUE-0005: high-sensitivity\n\n"
            "## Risk / Release Impact\n"
            "- Risk Level: high\n"
            "- Release Type: patch\n"
            "- Security Sensitivity: high\n\n"
            "## Routing Metadata\n"
            "- Type: feature\n"
            "- Priority: p1\n"
            "- Area: auth\n"
            "- Route: pm-review\n"
        )
        issue_low = (
            "# ISSUE-0006: low-risk feature\n\n"
            "## Risk / Release Impact\n"
            "- Risk Level: low\n"
            "- Release Type: patch\n"
            "- Security Sensitivity: low\n\n"
            "## Routing Metadata\n"
            "- Type: feature\n"
            "- Priority: p2\n"
            "- Area: docs\n"
            "- Route: pm-review\n"
        )
        with open(os.path.join(state._issues_dir(tmp), "ISSUE-0005.md"),
                  "w", encoding="utf-8") as f:
            f.write(issue_high)
        with open(os.path.join(state._issues_dir(tmp), "ISSUE-0006.md"),
                  "w", encoding="utf-8") as f:
            f.write(issue_low)

        # high sensitivity -> required=true
        cap = io.StringIO()
        old_out = sys.stdout
        sys.stdout = cap
        try:
            rc = cmd_security_check(argparse.Namespace(
                issue_id="ISSUE-0005", diff=None, target=tmp))
        finally:
            sys.stdout = old_out
        if rc != 0:
            failures.append(f"security-check ISSUE-0005 returned {rc}")
        if "required: true" not in cap.getvalue():
            failures.append(
                f"security-check ISSUE-0005 should print required: true; "
                f"got: {cap.getvalue()!r}")

        # low/feature no diff -> required=false
        cap = io.StringIO()
        sys.stdout = cap
        try:
            rc = cmd_security_check(argparse.Namespace(
                issue_id="ISSUE-0006", diff=None, target=tmp))
        finally:
            sys.stdout = old_out
        if rc != 0:
            failures.append(f"security-check ISSUE-0006 returned {rc}")
        if "required: false" not in cap.getvalue():
            failures.append(
                f"security-check ISSUE-0006 should print required: false; "
                f"got: {cap.getvalue()!r}")

        # low/feature + workflow diff -> required=true (path trigger)
        diff_path = os.path.join(tmp, "sample.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(
                "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                "index 111..222 100644\n"
                "--- a/.github/workflows/ci.yml\n"
                "+++ b/.github/workflows/ci.yml\n"
                "@@ -1 +1 @@\n"
                "+- run: make test\n"
            )
        cap = io.StringIO()
        sys.stdout = cap
        try:
            rc = cmd_security_check(argparse.Namespace(
                issue_id="ISSUE-0006", diff=diff_path, target=tmp))
        finally:
            sys.stdout = old_out
        if rc != 0:
            failures.append(
                f"security-check ISSUE-0006 with diff returned {rc}")
        if "required: true" not in cap.getvalue() \
                or ".github/workflows" not in cap.getvalue():
            failures.append(
                f"security-check ISSUE-0006+workflow diff should trigger; "
                f"got: {cap.getvalue()!r}")

        # --- AC-SI-008 + ISSUE-0002 worktree characterization ----------------
        # Invariant: after the dev phase, the issue branch HEAD is ahead of
        # its base. With worktree-per-issue the dev agent operates inside
        # binfo.worktree_path; the main tree stays on `main` and is clean.
        # Also exercised: stale branch detection, teardown removes the dir
        # but preserves the branch.
        gitrepo = tempfile.mkdtemp(prefix="laplace-runner-git-")
        try:
            def _git(args: list, cwd: str = gitrepo) -> subprocess.CompletedProcess:
                r = subprocess.run(["git", "-C", cwd] + args,
                                   capture_output=True, text=True, timeout=10)
                return r

            _git(["init", "-q", "--initial-branch=main"])
            _git(["config", "user.email", "self@test"])
            _git(["config", "user.name", "selftest"])
            with open(os.path.join(gitrepo, "README.md"), "w") as f:
                f.write("base\n")
            # .harness/ holds the ephemeral worktrees (and is the convention
            # in production repos via `state.cmd_init`). Gitignore it so the
            # main tree stays clean after worktree creation (AC-WT-002).
            with open(os.path.join(gitrepo, ".gitignore"), "w") as f:
                f.write(".harness/\n")
            _git(["add", "README.md", ".gitignore"])
            _git(["commit", "-q", "-m", "base"])
            base_sha = _git(["rev-parse", "main"]).stdout.strip()
            main_head_before = base_sha

            # _setup_branch now creates a worktree on laplace/ISSUE-0007.
            binfo = _setup_branch("ISSUE-0007", gitrepo)
            if binfo.status != "created":
                failures.append(
                    f"AC-WT-001: _setup_branch should create worktree in git "
                    f"repo, got {binfo.status}/{binfo.reason}")
            if not binfo.worktree_path or not os.path.isdir(binfo.worktree_path):
                failures.append(
                    f"AC-WT-001: worktree dir not created at "
                    f"{binfo.worktree_path}")
            # AC-WT-002: main tree stays on `main`, clean.
            cur_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            if cur_branch != "main":
                failures.append(
                    f"AC-WT-002: main tree switched to {cur_branch!r}, expected main")
            main_status = _git(["status", "--porcelain"]).stdout.strip()
            if main_status:
                failures.append(
                    f"AC-WT-002: main tree dirty after _setup_branch: {main_status!r}")

            # Simulate the dev agent: write + commit INSIDE the worktree.
            wt = binfo.worktree_path
            with open(os.path.join(wt, "change.txt"), "w") as f:
                f.write("dev work\n")
            commit_cmd = "git add change.txt && git commit -m feat(x): dev (ISSUE-0007)"
            ok, reason = policy.check_command(commit_cmd)
            if not ok:
                failures.append(
                    f"AC-SI-008: policy denied simulated dev commit: {reason}")
            else:
                r = _git(["add", "change.txt"], cwd=wt)
                r2 = _git(["commit", "-q", "-m",
                           "feat(x): dev (ISSUE-0007)"], cwd=wt)
                if r.returncode != 0 or r2.returncode != 0:
                    failures.append(
                        f"AC-SI-008: simulated dev commit failed: "
                        f"{r.stderr}{r2.stderr}")
            # Branch HEAD is the worktree HEAD, ahead of base.
            branch_head = _git(["rev-parse", "laplace/ISSUE-0007"]).stdout.strip()
            if branch_head == base_sha:
                failures.append(
                    "AC-SI-008: issue branch HEAD equals base after dev commit; "
                    "review would see an empty diff")
            ahead = _git(["rev-list", "--count", f"{base_sha}..laplace/ISSUE-0007"])
            if not (ahead.returncode == 0 and int(ahead.stdout.strip() or "0") >= 1):
                failures.append(
                    f"AC-SI-008: branch not ahead of base after dev: {ahead.stdout}")
            # AC-WT-002: main tree HEAD still on base, main tree still clean.
            main_head_after = _git(["rev-parse", "main"]).stdout.strip()
            if main_head_after != main_head_before:
                failures.append(
                    f"AC-WT-002: main tree HEAD moved ({main_head_before} -> "
                    f"{main_head_after}); dev commit leaked into main tree")
            main_status2 = _git(["status", "--porcelain"]).stdout.strip()
            if main_status2:
                failures.append(
                    f"AC-WT-002: main tree dirty after dev commit: {main_status2!r}")

            # AC-WT-008: dirty worktree halts teardown. Leave a change.txt mod
            # (already committed above), so add a NEW uncommitted file.
            with open(os.path.join(wt, "uncommitted.txt"), "w") as f:
                f.write("dirty\n")
            dirty_run_log = {"worktree_path": wt}
            wt_rc, wt_st = _teardown_worktree(
                "ISSUE-0007", gitrepo, dirty_run_log, force=False)
            if wt_rc != EXIT_WORKTREE_DIRTY or wt_st != "dirty-halt":
                failures.append(
                    f"AC-WT-008: dirty worktree should halt teardown, got "
                    f"rc={wt_rc} status={wt_st}")
            if not os.path.isdir(wt):
                failures.append(
                    "AC-WT-008: dirty worktree was removed despite halt")
            # Force-removing the dirty worktree is allowed.
            wt_rc2, wt_st2 = _teardown_worktree(
                "ISSUE-0007", gitrepo, dirty_run_log, force=True)
            if wt_rc2 != 0 or wt_st2 != "removed":
                failures.append(
                    f"AC-WT-008: forced teardown should succeed, got "
                    f"rc={wt_rc2} status={wt_st2}")
            if os.path.isdir(wt):
                failures.append(
                    "AC-WT-003/008: worktree dir still present after teardown")
            # AC-WT-003: branch preserved after worktree removal.
            if not _branch_exists("laplace/ISSUE-0007", gitrepo):
                failures.append(
                    "AC-WT-003: laplace/ISSUE-0007 branch deleted by teardown")

            # AC-WT-005: re-run _setup_branch on the existing branch — main is
            # an ancestor (branch is ahead), so status=reused in a fresh wt.
            binfo2 = _setup_branch("ISSUE-0007", gitrepo)
            if binfo2.status != "reused" or not binfo2.worktree_path:
                failures.append(
                    f"AC-WT-005: reuse should rebuild worktree, got {binfo2}")
            if binfo2.worktree_path and os.path.isdir(binfo2.worktree_path):
                # Clean up the reused worktree.
                _teardown_worktree(
                    "ISSUE-0007", gitrepo, {"worktree_path": binfo2.worktree_path},
                    force=True)

            # AC-WT-004: stale branch. Create laplace/ISSUE-0008 behind main,
            # then advance main so the branch is no longer current.
            stale_wt = _worktree_path("ISSUE-0008", gitrepo)
            stale_setup = _setup_branch("ISSUE-0008", gitrepo)
            if stale_setup.status != "created":
                failures.append(
                    f"AC-WT-004 setup: expected created for ISSUE-0008, got "
                    f"{stale_setup}")
            # Teardown so we can re-setup without 'already checked out' errors.
            _teardown_worktree("ISSUE-0008", gitrepo,
                               {"worktree_path": stale_wt}, force=True)
            # Advance main past the branch.
            with open(os.path.join(gitrepo, "main2.txt"), "w") as f:
                f.write("advance\n")
            _git(["add", "main2.txt"])
            _git(["commit", "-q", "-m", "advance main"])
            stale_check = _setup_branch("ISSUE-0008", gitrepo)
            if stale_check.status != "stale":
                failures.append(
                    f"AC-WT-004: stale branch should be detected, got "
                    f"{stale_check.status}/{stale_check.reason}")
            if os.path.isdir(stale_wt):
                failures.append(
                    f"AC-WT-004: stale branch must NOT create a worktree; "
                    f"wt dir exists at {stale_wt}")
        finally:
            shutil.rmtree(gitrepo, ignore_errors=True)
    finally:
        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("runner selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.py",
                                     description="Laplace run orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="Start a run: approved -> pm-review")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("advance", help="Transition issue state")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.add_argument("from_state")
    p.add_argument("to_state")
    p.add_argument("--summary", default=None)
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("evidence", help="Append redacted evidence to a run log")
    _add_target_arg(p)
    p.add_argument("run_id")
    p.add_argument("kind", help=f"one of {ALLOWED_EVIDENCE_KINDS}")
    p.add_argument("path_or_text",
                   help="File path (if exists) or raw text evidence")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("end", help="Finalize a run and release the lock")
    _add_target_arg(p)
    p.add_argument("run_id")
    p.add_argument("--outcome", default="completed")
    p.add_argument("--force-worktree-remove", action="store_true",
                   default=False,
                   help="Remove a dirty worktree at end (discards uncommitted "
                        "dev work; default halts with WORKTREE_DIRTY)")
    p.set_defaults(func=cmd_end)

    p = sub.add_parser(
        "security-check",
        help="Advisory security-review trigger (AC-LP-009). Exits 0 always.",
    )
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.add_argument("--diff", default=None,
                   help="Optional unified diff file path for path-trigger scan")
    p.set_defaults(func=cmd_security_check)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
