#!/usr/bin/env python3
"""Laplace queue runner (ISSUE-0003).

Composes `runner.py` primitives to iterate the approved queue and decide
advance vs halt after each issue reaches a terminal state.

Responsibilities:
  - Iterate approved issues from the queue head (or a named start issue).
  - Per issue: dependency pre-check, lock pre-probe, then delegate to
    `runner.cmd_start` (Python import; NOT subprocess).
  - After each issue's run ends, apply the decision matrix (AC-QR-007):
      review-passed + merge-policy advance + deps satisfied -> advance
      any other terminal state                              -> halt
      non-terminal final state                              -> halt (defensive)
      consecutive-issue counter >= max_queue_run            -> halt (AC-QR-008)
      queue exhausted                                       -> halt
  - Persist a parent queue run log at
    `.harness/state/runs/<queue-run-id>.json` with a `queue_steps` array
    (AC-QR-009).

This module is stdlib-only and reuses state.py atomic helpers. It does NOT
re-implement state transitions, fix-attempt limits, test-evidence gates, or
security checks -- those live inside runner.py primitives. queue_runner only
composes them and maps their exit codes to advance/halt decisions.

Merge execution is out of scope for this issue. `_handle_merge_policy`
(ISSUE-0004) detects whether the human has merged the issue branch into the
base branch via `git merge-base --is-ancestor`. When merged, the queue
advances to the next issue; otherwise it halts with `merge-wait:<id>`.

GATE ROUTING CONTRACT (AC-QR-G2): every git command queue_runner issues is
routed through policy.check_command first. The merge-base probe respects
that contract; on policy denial the probe fails safe (False -> halt ->
re-emit merge-wait).
"""


# Branch prefix mirrored from runner.BRANCH_PREFIX (kept here to avoid a
# runtime cross-module constant dependency; the two MUST stay in sync).
_BRANCH_PREFIX = "laplace"

import argparse
import hashlib
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Peer modules imported after the sys.path bootstrap above (mirrors runner.py).
import state  # noqa: E402
import runner  # noqa: E402
import policy  # noqa: E402

# Exit codes mirrored from runner.py for the decision matrix.
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_INVALID = 2
EXIT_LOCK_HELD = 3
EXIT_EVIDENCE_MISSING = 4
EXIT_FIX_LIMIT_EXCEEDED = 5


def _new_queue_run_id() -> str:
    """Generate a queue-run id using the same scheme as runner._new_run_id."""
    raw = f"queue-{time.time()}-{os.getpid()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _queue_run_log_path(run_id: str, target: Optional[str]) -> str:
    return os.path.join(state._runs_dir(target), f"{run_id}.json")


def _approved_queue(target: Optional[str]) -> List[str]:
    """Return the ordered approved-issue list from queue.json."""
    q = state._load_queue(target)
    return list(q.get("approved", []))


def _read_issue_status(issue_id: str, target: Optional[str]) -> Optional[str]:
    tasks = state._load_tasks(target)
    return tasks.get(issue_id, {}).get("status")


def _read_issue_run_id(issue_id: str, target: Optional[str]) -> Optional[str]:
    tasks = state._load_tasks(target)
    rid = tasks.get(issue_id, {}).get("run_id")
    return rid if rid else None


# ---------------------------------------------------------------------------
# Merge policy (AC-QR-007; ISSUE-0004: wait-for-human-merge detection)
# ---------------------------------------------------------------------------

def _issue_branch_is_merged(issue_id: str, target: Optional[str]) -> bool:
    """Return True if the issue's branch has been merged into the base branch.

    Fail-safe: any error condition (non-repo, git missing, branch missing,
    policy denial, timeout, non-zero exit) returns False, which causes the
    caller to halt and re-emit `merge-wait:<id>` until the human completes
    the merge.

    Uses `git merge-base --is-ancestor laplace/<issue_id> <base>` to detect
    ancestry. Base defaults to `main`, falling back to `master` if `main`
    does not exist.

    GATE ROUTING CONTRACT: the git command is routed through
    policy.check_command first. On denial, returns False (fail-safe).
    """
    if not issue_id:
        return False
    safe = issue_id.replace("/", "_")
    branch = f"{_BRANCH_PREFIX}/{safe}"
    root = state._harness_root(target)

    # Fail fast if this isn't a git worktree (no .git). Avoids a noisy
    # subprocess spawn in common non-repo test/dev harnesses.
    if not os.path.isdir(os.path.join(root, ".git")):
        return False

    for base in ("main", "master"):
        cmd = f"git merge-base --is-ancestor {branch} {base}"
        ok, _reason = policy.check_command(cmd)
        if not ok:
            # Policy denied this command; fail safe.
            return False
        try:
            r = subprocess.run(
                ["git", "-C", root, "merge-base", "--is-ancestor",
                 branch, base],
                capture_output=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            # git missing / timed out / OS error -> fail safe.
            return False
        if r.returncode == 0:
            return True
        # returncode 1 means "not an ancestor" (or base/branch missing).
        # Either way, try the next base candidate. If this was the last
        # candidate, fall through to the final False return below.

    return False


def _handle_merge_policy(issue_id: str, target: Optional[str]) -> str:
    """Decide advance vs halt after an issue reaches review-passed.

    ISSUE-0004 (wait-for-human-merge): advances only when the issue's branch
    has been merged into the base branch (main, falling back to master).
    Otherwise halts so the run-queue is re-invoked after the human merges.

    Returns one of {"advance", "halt"}.
    """
    if not issue_id:
        return "halt"
    if _issue_branch_is_merged(issue_id, target):
        return "advance"
    return "halt"


# ---------------------------------------------------------------------------
# Parent queue run log (AC-QR-009)
# ---------------------------------------------------------------------------

def _create_parent_log(run_id: str, start_issue: Optional[str],
                       config: Dict[str, Any],
                       target: Optional[str]) -> str:
    log: Dict[str, Any] = {
        "run_id": run_id,
        "kind": "queue",
        "issue_id": None,
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,
        "max_queue_run": config["max_queue_run"],
        "merge_policy": config["merge_policy"],
        "start_issue": state._redact_evidence(start_issue) if start_issue else None,
        "queue_steps": [],
        "issues": [],
    }
    state._atomic_write_json(_queue_run_log_path(run_id, target), log)
    return run_id


def _finalize_parent_log(run_id: str, outcome: str,
                         target: Optional[str]) -> None:
    path = _queue_run_log_path(run_id, target)
    log = state._read_json(path, default=None)
    if not isinstance(log, dict):
        return
    log["ended_at"] = time.time()
    log["outcome"] = state._redact_evidence(outcome)
    state._atomic_write_json(path, log)


def _append_queue_step(run_id: str, from_issue: str, to_issue: str,
                       from_terminal_state: str, evidence_run_id: str,
                       target: Optional[str]) -> None:
    path = _queue_run_log_path(run_id, target)
    log = state._read_json(path, default=None)
    if not isinstance(log, dict):
        return
    log.setdefault("queue_steps", []).append({
        "ts": time.time(),
        "from_issue": state._redact_evidence(from_issue),
        "to_issue": state._redact_evidence(to_issue),
        "from_terminal_state": from_terminal_state,
        "evidence_run_id": state._redact_evidence(evidence_run_id),
    })
    state._atomic_write_json(path, log)


def _record_child_run(run_id: str, child_run_id: str,
                      target: Optional[str]) -> None:
    path = _queue_run_log_path(run_id, target)
    log = state._read_json(path, default=None)
    if not isinstance(log, dict):
        return
    issues = log.setdefault("issues", [])
    if child_run_id not in issues:
        issues.append(child_run_id)
    state._atomic_write_json(path, log)


# ---------------------------------------------------------------------------
# Per-issue processing
# ---------------------------------------------------------------------------

class IssueResult:
    """Outcome of processing a single issue in the queue loop."""

    def __init__(self, halt: bool, outcome: str,
                 terminal_state: Optional[str] = None,
                 child_run_id: Optional[str] = None) -> None:
        self.halt = halt
        self.outcome = outcome
        self.terminal_state = terminal_state
        self.child_run_id = child_run_id


def _precheck_issue(issue_id: str, target: Optional[str]) \
        -> Optional[Tuple[str, int]]:
    """Pre-checks before invoking runner.cmd_start for an issue.

    Returns (outcome, exit_code) on failure, else None to proceed.
    """
    # AC-QR-DEPS: dependency gate.
    ok, reason = state._dependencies_satisfied(issue_id, target=target)
    if not ok:
        return (f"unmet-dependency:{issue_id}:{reason}", EXIT_INVALID)

    # AC-QR-010: lock probe. If held, halt without force / deletion /
    # foreign release. We probe then immediately release our own probe so
    # the subsequent runner.cmd_start (which re-acquires) succeeds cleanly.
    ok, reason = state.acquire_lock(issue_id, target=target)
    if not ok:
        return (f"held-lock:{issue_id}:{reason}", EXIT_LOCK_HELD)
    state.release_lock(issue_id, target=target)

    status = _read_issue_status(issue_id, target)
    if status != "approved":
        return (f"not-approved:{issue_id}:{status}", EXIT_INVALID)
    return None


def _process_issue(issue_id: str, target: Optional[str],
                   issue_driver: Optional[Callable[[str, Optional[str]], None]]) \
        -> IssueResult:
    """Process a single issue: pre-check, start, await terminal, return result.

    `issue_driver` is an optional callback invoked after `runner.cmd_start`
    to drive the issue through phases to a terminal state (the skill/agent's
    responsibility in production). When None, the issue is expected to already
    be in a terminal state or to reach one externally; if it does not, the
    decision loop reports `non-terminal`.
    """
    pre = _precheck_issue(issue_id, target)
    if pre is not None:
        outcome, code = pre
        return IssueResult(halt=True, outcome=outcome)

    ns = argparse.Namespace(issue_id=issue_id, target=target)
    rc = runner.cmd_start(ns)
    if rc == EXIT_LOCK_HELD:
        return IssueResult(halt=True, outcome=f"held-lock:{issue_id}:start")
    if rc == EXIT_FIX_LIMIT_EXCEEDED:
        return IssueResult(halt=True, outcome=f"fix-limit-exceeded:{issue_id}")
    if rc != EXIT_OK:
        return IssueResult(halt=True, outcome=f"start-failed:{issue_id}:{rc}")

    child_run_id = _read_issue_run_id(issue_id, target)

    if issue_driver is not None:
        issue_driver(issue_id, target)

    terminal_state = _read_issue_status(issue_id, target)
    return IssueResult(
        halt=False,
        outcome="started",
        terminal_state=terminal_state,
        child_run_id=child_run_id,
    )


# ---------------------------------------------------------------------------
# Decision matrix (AC-QR-007)
# ---------------------------------------------------------------------------

def _decide(result: IssueResult, next_issue: Optional[str],
            max_queue_run: int, consecutive: int,
            target: Optional[str],
            policy_override: Optional[Callable[[str, Optional[str]], str]] = None) \
        -> Tuple[bool, str]:
    """Return (halt, outcome) for the post-issue decision.

    halt=True with a descriptive outcome ends the parent queue run.
    halt=False means continue to next_issue (advance).
    """
    final = result.terminal_state

    if final is None or final not in state.TERMINAL_STATES:
        # Defensive: the skill should always end with cmd_end. If the issue
        # is stuck in a non-terminal state, halt rather than spin.
        return True, f"non-terminal:{final}:{result.outcome}"

    if final != "review-passed":
        # blocked / human-approval-required / cancelled / etc.
        return True, f"terminal:{final}"

    # final == review-passed: consult merge policy + deps + counter.
    # _handle_merge_policy takes an issue id (the branch name is derived
    # from it); recover the issue id from the child run log.
    issue_id = _issue_from_run(result, target)
    if policy_override is not None:
        decision = policy_override(issue_id, target)
    else:
        decision = _handle_merge_policy(issue_id, target)
    if decision == "halt":
        return True, f"merge-wait:{issue_id}"

    # decision == "advance"
    if next_issue is None:
        return True, "queue-exhausted"
    if consecutive >= max_queue_run:
        return True, f"max-queue-run-reached:{consecutive}"
    ok, reason = state._dependencies_satisfied(next_issue, target=target)
    if not ok:
        return True, f"unmet-dependency:{next_issue}:{reason}"
    return False, "advance"


def _issue_from_run(result: IssueResult, target: Optional[str]) -> str:
    """Best-effort recover of the issue_id for an IssueResult.

    The child run log records the issue_id; fall back to scanning tasks.json
    for the run_id. Used only for human-readable outcome strings.
    """
    if not result.child_run_id:
        return "?"
    rpath = os.path.join(state._runs_dir(target),
                         f"{result.child_run_id}.json")
    run = state._read_json(rpath, default=None)
    if isinstance(run, dict) and run.get("issue_id"):
        return str(run["issue_id"])
    tasks = state._load_tasks(target)
    for iid, meta in tasks.items():
        if meta.get("run_id") == result.child_run_id:
            return iid
    return "?"


# ---------------------------------------------------------------------------
# Queue loop
# ---------------------------------------------------------------------------

def _run_queue(start_issue: Optional[str], target: Optional[str],
               config: Dict[str, Any],
               issue_driver: Optional[Callable[[str, Optional[str]], None]],
               policy_override: Optional[Callable[[str, Optional[str]], str]] = None) \
        -> Tuple[str, int]:
    """Iterate the approved queue from `start_issue` (or queue head).

    Returns (parent_run_id, exit_code). Creates the parent log, processes
    issues, appends queue-step entries on advances, and finalizes the log
    with the halt outcome.

    Loop contract: process issue N -> decide. On `advance`, record the
    queue_step (from N to N+1) and continue with N+1. On any halt outcome,
    finalize the parent log and return. A queue_step records an actual
    advance transition; halts before any advance record no step.
    """
    queue_run_id = _new_queue_run_id()
    _create_parent_log(queue_run_id, start_issue, config, target)

    approved = _approved_queue(target)
    if not approved:
        _finalize_parent_log(queue_run_id, "noop-empty-queue", target)
        print(f"queue: no approved issues; nothing to do")
        return queue_run_id, EXIT_OK

    if start_issue is not None:
        if start_issue not in approved:
            _finalize_parent_log(queue_run_id,
                                 f"noop-start-not-approved:{start_issue}", target)
            print(f"queue: start issue {start_issue} not in approved queue")
            return queue_run_id, EXIT_OK
        idx = approved.index(start_issue)
    else:
        idx = 0

    max_queue_run = config["max_queue_run"]
    consecutive = 0  # issues that reached a terminal state in this run

    while idx < len(approved):
        issue_id = approved[idx]
        result = _process_issue(issue_id, target, issue_driver)

        # Pre-check or start failure halts immediately (no counter bump).
        if result.halt:
            _finalize_parent_log(queue_run_id, result.outcome, target)
            print(f"queue halted: {result.outcome}")
            return queue_run_id, EXIT_INVALID

        # Record child run id for the parent trail.
        if result.child_run_id:
            _record_child_run(queue_run_id, result.child_run_id, target)

        final = result.terminal_state
        if final in state.TERMINAL_STATES:
            consecutive += 1

        next_issue = approved[idx + 1] if idx + 1 < len(approved) else None
        halt, outcome = _decide(result, next_issue, max_queue_run,
                                consecutive, target,
                                policy_override=policy_override)

        if not halt:
            # AC-QR-009: record the advance transition issue -> next_issue.
            _append_queue_step(queue_run_id, issue_id, next_issue or "",
                               final or "review-passed",
                               result.child_run_id or "", target)
            idx += 1
            continue

        _finalize_parent_log(queue_run_id, outcome, target)
        print(f"queue halted: {outcome}")
        # Expected halts (clean skill handoff) exit 0; unexpected halts
        # (blocked terminal, non-terminal, unmet dep, held lock) non-zero.
        if outcome.startswith("merge-wait") \
                or outcome == "queue-exhausted" \
                or outcome.startswith("max-queue-run-reached") \
                or outcome.startswith("noop-"):
            return queue_run_id, EXIT_OK
        return queue_run_id, EXIT_INVALID

    _finalize_parent_log(queue_run_id, "queue-exhausted", target)
    print(f"queue: exhausted approved issues")
    return queue_run_id, EXIT_OK


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Queue `start` subcommand: iterate the approved queue."""
    target = args.target
    config = state.load_config(target)  # exits 2 on validation failure
    start_issue = getattr(args, "issue_id", None)
    issue_driver = getattr(args, "_issue_driver", None)
    _run_id, rc = _run_queue(start_issue, target, config, issue_driver)
    return rc


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    import shutil

    failures: List[str] = []
    tmp = tempfile.mkdtemp(prefix="laplace-queue-selftest-")

    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        # --- _handle_merge_policy: wait-for-human-merge detection --------
        # Empty issue id -> halt (defensive).
        if _handle_merge_policy("", tmp) != "halt":
            failures.append("_handle_merge_policy('') should return 'halt'")
        # Non-repo tmp target -> halt, no subprocess crash (fail-safe).
        if _handle_merge_policy("ISSUE-X", tmp) != "halt":
            failures.append(
                "_handle_merge_policy non-repo should return 'halt'")
        if _handle_merge_policy("ISSUE-X", None) != "halt":
            failures.append(
                "_handle_merge_policy non-repo (None target) should 'halt'")

        # Real git repo: merged branch -> advance; unmerged -> halt.
        def _git(args, cwd):
            r = subprocess.run(["git", "-C", cwd] + args,
                               capture_output=True, text=True, timeout=10)
            assert r.returncode == 0, \
                f"git {args} failed in {cwd}: {r.stderr}"
            return r

        def _make_repo(base_branch: str) -> str:
            """Create a temp git repo with the given default base branch."""
            repo = tempfile.mkdtemp(prefix="laplace-queue-git-")
            _git(["init", "-q", f"--initial-branch={base_branch}"], repo)
            _git(["config", "user.email", "self@test"], repo)
            _git(["config", "user.name", "selftest"], repo)
            # initial commit on base branch
            with open(os.path.join(repo, "README.md"), "w") as f:
                f.write("init\n")
            _git(["add", "README.md"], repo)
            _git(["commit", "-q", "-m", "init"], repo)
            return repo

        # merged branch on `main` -> advance
        repo_main = _make_repo("main")
        try:
            _git(["checkout", "-q", "-b", "laplace/ISSUE-M"], repo_main)
            with open(os.path.join(repo_main, "f.txt"), "w") as f:
                f.write("change\n")
            _git(["add", "f.txt"], repo_main)
            _git(["commit", "-q", "-m", "issue change"], repo_main)
            _git(["checkout", "-q", "main"], repo_main)
            _git(["merge", "-q", "--no-ff", "laplace/ISSUE-M",
                  "-m", "merge issue"], repo_main)
            if _handle_merge_policy("ISSUE-M", repo_main) != "advance":
                failures.append(
                    "merged branch on main should return 'advance'")
            # _issue_branch_is_merged direct check
            if not _issue_branch_is_merged("ISSUE-M", repo_main):
                failures.append(
                    "_issue_branch_is_merged should be True for merged branch")
        finally:
            shutil.rmtree(repo_main, ignore_errors=True)

        # unmerged branch on `main` -> halt
        repo_unmerged = _make_repo("main")
        try:
            _git(["checkout", "-q", "-b", "laplace/ISSUE-U"],
                 repo_unmerged)
            with open(os.path.join(repo_unmerged, "g.txt"), "w") as f:
                f.write("change\n")
            _git(["add", "g.txt"], repo_unmerged)
            _git(["commit", "-q", "-m", "issue change"], repo_unmerged)
            if _handle_merge_policy("ISSUE-U", repo_unmerged) != "halt":
                failures.append(
                    "unmerged branch should return 'halt'")
            if _issue_branch_is_merged("ISSUE-U", repo_unmerged):
                failures.append(
                    "_issue_branch_is_merged should be False for unmerged")
        finally:
            shutil.rmtree(repo_unmerged, ignore_errors=True)

        # merged branch on `master` (no `main`) -> advance via fallback
        repo_master = _make_repo("master")
        try:
            _git(["checkout", "-q", "-b", "laplace/ISSUE-MS"],
                 repo_master)
            with open(os.path.join(repo_master, "h.txt"), "w") as f:
                f.write("change\n")
            _git(["add", "h.txt"], repo_master)
            _git(["commit", "-q", "-m", "issue change"], repo_master)
            _git(["checkout", "-q", "master"], repo_master)
            _git(["merge", "-q", "--no-ff", "laplace/ISSUE-MS",
                  "-m", "merge issue"], repo_master)
            if _handle_merge_policy("ISSUE-MS", repo_master) != "advance":
                failures.append(
                    "merged branch on master should return 'advance'")
        finally:
            shutil.rmtree(repo_master, ignore_errors=True)

        def _advance_policy(issue_id, target):  # noqa: ARG001
            return "advance"

        # --- AC-QR-NOOP: empty approved queue -> exit 0, no parent log ---
        if state.cmd_init(target=tmp) != 0:
            failures.append("state.cmd_init returned non-zero")
        cfg = state.load_config(tmp)
        rid0, rc0 = _run_queue(None, tmp, cfg, None)
        if rc0 != 0:
            failures.append(f"empty queue should exit 0, got {rc0}")
        # noop outcome still writes a log (records the noop); acceptable.

        # --- AC-QR-NOOP: named start issue not in approved queue ---------
        rid0b, rc0b = _run_queue("ISSUE-NOPE", tmp, cfg, None)
        if rc0b != 0:
            failures.append(
                f"start issue not approved should exit 0, got {rc0b}")

        # --- Helper: seed an approved issue with a draft->approved flow ---
        def seed_approved(issue_id: str) -> None:
            tasks = state._load_tasks(tmp)
            tasks[issue_id] = {"status": "draft", "updated_at": time.time()}
            state._save_tasks(tasks, target=tmp)
            q = state._load_queue(tmp)
            if issue_id not in q["draft"]:
                q["draft"].append(issue_id)
            state._save_queue(q, target=tmp)
            assert state.cmd_approve(argparse.Namespace(
                issue_id=issue_id, user="tester", target=tmp)) == 0, \
                f"cmd_approve failed for {issue_id}"

        # --- Driver: push the current issue to review-passed -------------
        # Simulates the skill/agent's intra-issue phase loop using the same
        # runner primitives (compose, not re-implement).
        def drive_to_review_passed(issue_id: str, target: Optional[str]) -> None:
            run_id = _read_issue_run_id(issue_id, target)
            # pm-review -> ready-for-dev -> in-progress -> review
            for src, dst in (("pm-review", "ready-for-dev"),
                             ("ready-for-dev", "in-progress"),
                             ("in-progress", "review")):
                ns = argparse.Namespace(
                    issue_id=issue_id, from_state=src, to_state=dst,
                    summary="", target=target,
                )
                assert runner.cmd_advance(ns) == 0, \
                    f"drive {src}->{dst} failed for {issue_id}"
            # Capture test evidence (AC-LP-008 gate) then review-passed.
            ns_ev = argparse.Namespace(
                run_id=run_id, kind="test", path_or_text="pytest: ok",
                target=target,
            )
            assert runner.cmd_evidence(ns_ev) == 0, "evidence capture failed"
            ns_pass = argparse.Namespace(
                issue_id=issue_id, from_state="review", to_state="review-passed",
                summary="ok", target=target,
            )
            assert runner.cmd_advance(ns_pass) == 0, "review->passed failed"

        def drive_to_blocked(issue_id: str, target: Optional[str]) -> None:
            for src, dst in (("pm-review", "ready-for-dev"),
                             ("ready-for-dev", "blocked")):
                ns = argparse.Namespace(
                    issue_id=issue_id, from_state=src, to_state=dst,
                    summary="", target=target,
                )
                assert runner.cmd_advance(ns) == 0

        # --- AC-QR-007 + AC-QR-011: review-passed, non-repo -> merge-wait -
        # tmp is not a git repo, so _issue_branch_is_merged fails safe to
        # False, and the default policy halts with merge-wait:ISSUE-A.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-A")
        seed_approved("ISSUE-B")

        rid1, rc1 = _run_queue(None, tmp, cfg, drive_to_review_passed)
        if rc1 != 0:
            failures.append(f"merge-wait halt should exit 0, got {rc1}")
        log1 = state._read_json(_queue_run_log_path(rid1, tmp), default=None)
        if not isinstance(log1, dict) or log1.get("kind") != "queue":
            failures.append("parent queue log missing or wrong kind")
        if log1.get("outcome") != "merge-wait:ISSUE-A":
            failures.append(
                f"expected merge-wait:ISSUE-A, got {log1.get('outcome')}")
        # ISSUE-B must NOT have been started (merge-wait halts).
        if _read_issue_status("ISSUE-B", tmp) != "approved":
            failures.append(
                "ISSUE-B should remain approved when merge-wait halts")
        # queue_steps should be empty (no advance happened).
        if log1.get("queue_steps"):
            failures.append(
                f"queue_steps should be empty on merge-wait halt, got "
                f"{log1.get('queue_steps')}")
        # issues trail should contain ISSUE-A's child run.
        if not log1.get("issues"):
            failures.append("parent log issues trail empty")

        # --- AC-QR-008: max_queue_run cap enforced -----------------------
        # Override merge policy to advance via policy_override, set cap=1.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-C")
        seed_approved("ISSUE-D")
        cfg_cap = {"max_queue_run": 1, "merge_policy": cfg["merge_policy"]}

        rid2, rc2 = _run_queue(None, tmp, cfg_cap, drive_to_review_passed,
                               policy_override=_advance_policy)

        log2 = state._read_json(_queue_run_log_path(rid2, tmp), default=None)
        # With cap=1: ISSUE-C runs to review-passed, policy advances, but
        # consecutive counter (1) >= max_queue_run (1) -> halt before ISSUE-D.
        if not log2 or not log2.get("outcome", "").startswith(
                "max-queue-run-reached"):
            failures.append(
                f"cap=1 should halt max-queue-run-reached, got "
                f"{log2.get('outcome') if log2 else None}")
        if _read_issue_status("ISSUE-D", tmp) == "pm-review":
            failures.append("ISSUE-D should not be started under cap=1")

        # --- AC-QR-007: non-review-passed terminal -> halt ---------------
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-E")
        seed_approved("ISSUE-F")
        rid3, rc3 = _run_queue(None, tmp, cfg, drive_to_blocked)
        log3 = state._read_json(_queue_run_log_path(rid3, tmp), default=None)
        if not log3 or log3.get("outcome") != "terminal:blocked":
            failures.append(
                f"blocked terminal should halt terminal:blocked, got "
                f"{log3.get('outcome') if log3 else None}")
        if _read_issue_status("ISSUE-F", tmp) != "approved":
            failures.append("ISSUE-F should remain approved after blocked halt")

        # --- AC-QR-010: held lock on issue -> halt, lock untouched -------
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-G")
        # Pre-acquire the lock so _precheck_issue's probe fails.
        lok, _ = state.acquire_lock("ISSUE-G", target=tmp)
        if not lok:
            failures.append("could not pre-acquire lock for AC-QR-010")
        else:
            rid4, rc4 = _run_queue(None, tmp, cfg, None)
            log4 = state._read_json(_queue_run_log_path(rid4, tmp), default=None)
            if not log4 or not log4.get("outcome", "").startswith("held-lock"):
                failures.append(
                    f"held lock should halt held-lock:..., got "
                    f"{log4.get('outcome') if log4 else None}")
            # Lock file must still exist (not deleted/released by queue).
            if not os.path.exists(state._lock_path("ISSUE-G", tmp)):
                failures.append("AC-QR-010: lock file was deleted")
            state.release_lock("ISSUE-G", target=tmp)

        # --- AC-QR-DEPS: unmet dependency -> halt ------------------------
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-DEP")
        # Approve a second issue that depends on a non-terminal dep.
        state._save_tasks(
            {"ISSUE-DEP": {"status": "approved", "updated_at": time.time()},
             "ISSUE-CHILD": {"status": "approved", "updated_at": time.time(),
                             "depends_on": ["ISSUE-DEP"]}},
            target=tmp,
        )
        qd = state._load_queue(tmp)
        qd["approved"] = ["ISSUE-CHILD"]
        state._save_queue(qd, tmp)
        rid5, rc5 = _run_queue("ISSUE-CHILD", tmp, cfg, None)
        log5 = state._read_json(_queue_run_log_path(rid5, tmp), default=None)
        if not log5 or not log5.get("outcome", "").startswith(
                "unmet-dependency"):
            failures.append(
                f"unmet dep should halt unmet-dependency:..., got "
                f"{log5.get('outcome') if log5 else None}")

        # --- Integration: advance policy + two issues -> queue_step -------
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-P1")
        seed_approved("ISSUE-P2")
        cfg_cap2 = {"max_queue_run": 5, "merge_policy": cfg["merge_policy"]}
        rid6, rc6 = _run_queue(None, tmp, cfg_cap2, drive_to_review_passed,
                               policy_override=_advance_policy)
        log6 = state._read_json(_queue_run_log_path(rid6, tmp), default=None)
        # Both issues run; queue exhausted -> queue-exhausted outcome.
        if not log6 or log6.get("outcome") != "queue-exhausted":
            failures.append(
                f"two-issue advance should end queue-exhausted, got "
                f"{log6.get('outcome') if log6 else None}")
        steps = log6.get("queue_steps", []) if log6 else []
        if len(steps) != 1:
            failures.append(
                f"expected 1 queue_step on two-issue advance, got {len(steps)}")
        if steps:
            s = steps[0]
            if s.get("from_issue") != "ISSUE-P1" \
                    or s.get("to_issue") != "ISSUE-P2":
                failures.append(f"queue_step from/to wrong: {s}")
            if s.get("from_terminal_state") != "review-passed":
                failures.append(
                    f"queue_step from_terminal_state wrong: "
                    f"{s.get('from_terminal_state')}")
            if not s.get("evidence_run_id"):
                failures.append("queue_step evidence_run_id empty")
            # evidence_run_id should match ISSUE-P1's child run log.
            p1_run = _read_issue_run_id("ISSUE-P1", tmp)
            if s.get("evidence_run_id") != p1_run:
                failures.append(
                    f"queue_step evidence_run_id {s.get('evidence_run_id')} "
                    f"!= ISSUE-P1 run {p1_run}")

        # --- AC-QR-012: resume mechanism (review-passed leaves approved) --
        # After a merge-wait halt, the completed issue is at review-passed
        # (a terminal state) and thus absent from `approved` on the next
        # _run_queue call. The queue naturally resumes at the next issue
        # without an explicit pop -- characterize this implicit resume.
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-R1")
        seed_approved("ISSUE-R2")
        ridR, rcR = _run_queue(None, tmp, cfg, drive_to_review_passed)
        logR = state._read_json(_queue_run_log_path(ridR, tmp), default=None)
        if not logR or logR.get("outcome") != "merge-wait:ISSUE-R1":
            failures.append(
                f"resume char: expected merge-wait:ISSUE-R1, got "
                f"{logR.get('outcome') if logR else None}")
        q_after = state._load_queue(tmp)
        if _read_issue_status("ISSUE-R1", tmp) != "review-passed":
            failures.append(
                "resume char: ISSUE-R1 should be review-passed")
        if "ISSUE-R1" in q_after.get("approved", []):
            failures.append(
                "resume char: ISSUE-R1 should be absent from approved queue")
        if "ISSUE-R2" not in q_after.get("approved", []):
            failures.append(
                "resume char: ISSUE-R2 should still be in approved queue")

        # --- Characterization: runner primitives unaffected --------------
        # Direct runner.cmd_start + cmd_end still works (no queue_runner).
        state._save_tasks({}, target=tmp)
        state._save_queue(state.DEFAULT_QUEUE, target=tmp)
        seed_approved("ISSUE-CHAR")
        ns_st = argparse.Namespace(issue_id="ISSUE-CHAR", target=tmp)
        if runner.cmd_start(ns_st) != 0:
            failures.append("char: runner.cmd_start failed standalone")
        char_run = _read_issue_run_id("ISSUE-CHAR", tmp)
        ns_end = argparse.Namespace(
            run_id=char_run, outcome="blocked", target=tmp)
        if runner.cmd_end(ns_end) != 0:
            failures.append("char: runner.cmd_end failed standalone")
        char_log = state._read_json(
            os.path.join(state._runs_dir(tmp), f"{char_run}.json"))
        if not char_log or char_log.get("outcome") != "blocked":
            failures.append("char: standalone run log outcome wrong")
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
    print("queue_runner selftest: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="queue_runner.py",
        description="Laplace queue orchestrator (ISSUE-0003)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="Iterate the approved queue")
    _add_target_arg(p)
    p.add_argument("issue_id", nargs="?", default=None,
                   help="Optional start issue (must be approved)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
