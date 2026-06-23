#!/usr/bin/env python3
"""State engine for Laplace's .harness/ runtime workspace.

Responsibilities:
  - Atomic JSON read/write (write to tmp then os.replace; never partial)
  - File-based locking under .harness/state/locks/<issue-id>.lock with PID + ts
  - State-machine validation per SPEC-002 §State Machine
  - CLI entry point for init / status / list / show / approve / transition /
    run-start / run-end / lock / unlock / selftest

All persisted evidence is passed through redaction.py before write (G-LP-003).
stdlib-only.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# --- Loop limit constants (G-LP-004). Mirror policy.py and config.yml template. -

MAX_FIX_ATTEMPTS = 3
MAX_PM_CLARIFICATION_ATTEMPTS = 2
MAX_SECURITY_FIX_ATTEMPTS = 2
MAX_RUNTIME_MINUTES_PER_ISSUE = 60
MAX_FILES_CHANGED_WITHOUT_APPROVAL = 20
MAX_DIFF_LINES_WITHOUT_APPROVAL = 1000
MAX_STOP_HOOK_ITERATIONS = 12
MAX_QUEUE_RUN = 5
MAX_PARALLEL = 2

DEFAULT_MERGE_POLICY = "wait-for-human-merge"
VALID_MERGE_POLICIES = {"wait-for-human-merge", "auto-merge-branch"}

LOCK_TTL_SECONDS = 60 * 60  # stale-lock detection window (60 min default)

# Lock ID for the ID-allocation / draft-mutation critical section. Shared with
# intake.py (which imports this constant) so intake and discard cannot race.
INTAKE_LOCK_ID = "ISSUE-INTAKE"

# --- State machine (SPEC-002 §State Machine) -----------------------------------

VALID_TRANSITIONS: Dict[str, List[str]] = {
    "draft": ["approved"],
    "approved": ["pm-review", "blocked"],
    "pm-review": ["ready-for-dev", "blocked"],
    "ready-for-dev": ["in-progress", "blocked"],
    "in-progress": ["review", "blocked", "needs-fix", "human-approval-required"],
    "review": ["needs-fix", "review-passed", "security-review", "blocked",
               "human-approval-required"],
    "security-review": ["needs-fix", "review-passed", "blocked",
                        "human-approval-required"],
    "needs-fix": ["in-progress", "blocked", "human-approval-required"],
    "review-passed": ["release-candidate", "blocked"],
    "release-candidate": ["done", "blocked"],
    "done": [],
    "blocked": ["human-resolution"],
    "human-resolution": ["draft", "approved", "pm-review", "ready-for-dev",
                          "in-progress", "review", "needs-fix", "review-passed",
                          "release-candidate", "done", "cancelled"],
    "human-approval-required": [],
    "cancelled": [],
}

QUEUE_STATES = ["draft", "approved", "in-progress", "blocked", "release-candidate"]

TERMINAL_STATES = {"review-passed", "security-passed", "release-candidate",
                   "done", "blocked", "max-attempts-exceeded",
                   "human-approval-required", "cancelled"}

DEFAULT_QUEUE: Dict[str, List[str]] = {s: [] for s in QUEUE_STATES}

# --- Paths ----------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))


def _harness_root(target: Optional[str] = None) -> str:
    return os.path.abspath(target or os.getcwd())


def _state_dir(target: Optional[str] = None) -> str:
    return os.path.join(_harness_root(target), ".harness", "state")


def _issues_dir(target: Optional[str] = None) -> str:
    return os.path.join(_harness_root(target), ".harness", "issues")


# --- Redaction integration (G-LP-003) ------------------------------------------

def _redact_evidence(text: str) -> str:
    """Apply redaction to any evidence string before it is persisted."""
    try:
        from redaction import redact  # local import; same-scripts dir
    except ImportError:
        # Fallback: in-place path so tests that import this module directly
        # without scripts/ on sys.path still get redaction via late binding.
        sys.path.insert(0, HERE)
        try:
            from redaction import redact  # type: ignore
        except Exception:
            return text
    return redact(text)


# --- Atomic JSON I/O ------------------------------------------------------------

def _atomic_write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


# --- File locking ---------------------------------------------------------------

def _lock_path(issue_id: str, target: Optional[str] = None) -> str:
    safe = issue_id.replace("/", "_")
    return os.path.join(_state_dir(target), "locks", f"{safe}.lock")


def _read_lock(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"pid": -1, "ts": 0, "corrupt": True}


def acquire_lock(issue_id: str, target: Optional[str] = None,
                 ttl: int = LOCK_TTL_SECONDS) -> Tuple[bool, str]:
    """Try to acquire a lock for an issue. Returns (ok, reason).
    Stale locks (older than ttl seconds, or whose PID is not alive) are reused.
    """
    os.makedirs(os.path.dirname(_lock_path(issue_id, target)), exist_ok=True)
    existing = _read_lock(_lock_path(issue_id, target))
    if existing and not existing.get("corrupt"):
        pid = existing.get("pid", -1)
        age = time.time() - float(existing.get("ts", 0))
        if _pid_alive(pid) and age < ttl:
            return False, f"locked by pid={pid}"
    payload = {"pid": os.getpid(), "ts": time.time(), "issue_id": issue_id}
    _atomic_write_json(_lock_path(issue_id, target), payload)
    return True, "acquired"


def release_lock(issue_id: str, target: Optional[str] = None) -> Tuple[bool, str]:
    path = _lock_path(issue_id, target)
    if not os.path.exists(path):
        return True, "no lock present"
    os.remove(path)
    return True, "released"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# --- State machine --------------------------------------------------------------

def validate_transition(from_state: str, to_state: str) -> Tuple[bool, str]:
    if from_state == to_state:
        return True, "no-op"
    allowed = VALID_TRANSITIONS.get(from_state)
    if allowed is None:
        return False, f"unknown source state: {from_state}"
    if to_state not in allowed:
        return False, f"invalid transition: {from_state} -> {to_state}"
    return True, "ok"


def _check_dependency_graph(issue_id: str,
                            target: Optional[str] = None) -> Tuple[bool, str]:
    """Validate the dependency graph for an issue on approval.

    Builds an id -> deps map from tasks.json (each record's `depends_on` list;
    missing key treated as empty). Two checks:
      1. Missing reference: every dep must exist as a key in tasks.json.
      2. Cycle detection: DFS from `issue_id` over the deps map; a cycle
         (including a length-1 self-reference) is rejected.

    Returns (True, "ok") when valid, else (False, "<human-readable reason>").
    """
    tasks = _load_tasks(target)
    graph: Dict[str, List[str]] = {
        tid: list(rec.get("depends_on", []) or []) for tid, rec in tasks.items()
    }
    deps = graph.get(issue_id, [])
    # 1. Missing reference check.
    for dep in deps:
        if dep not in graph:
            return False, f"cannot approve {issue_id}: dependency {dep} does not exist"
    # 2. Cycle detection (DFS over the whole graph reachable from issue_id).
    WHITE, GREY, BLACK = 0, 1, 2
    color: Dict[str, int] = {}

    def dfs(node: str) -> Optional[str]:
        color[node] = GREY
        for nxt in graph.get(node, []):
            if nxt not in graph:
                # Missing ref reachable transitively; report it too.
                return f"dependency {nxt} of {node} does not exist"
            c = color.get(nxt, WHITE)
            if c == GREY:
                return f"cycle detected: {node} -> {nxt}"
            if c == WHITE:
                cyc = dfs(nxt)
                if cyc is not None:
                    return cyc
        color[node] = BLACK
        return None

    cyc = dfs(issue_id)
    if cyc is not None:
        return False, f"cannot approve {issue_id}: {cyc}"
    return True, "ok"


def _dependencies_satisfied(issue_id: str,
                            target: Optional[str] = None) -> Tuple[bool, str]:
    """Check whether every declared dependency of `issue_id` is satisfied.

    A dependency is considered satisfied when its state is `review-passed`
    or any terminal state (TERMINAL_STATES). Returns (True, "ok") when all
    deps are satisfied, else (False, "unmet dependency: <id> (<state>)").

    NOTE: This is a stub helper. The queue runner (ISSUE-0003) will call it
    before starting an issue. `cmd_approve` only enforces graph *validity*
    (existence + acyclicity) via `_check_dependency_graph`, NOT satisfaction.
    """
    tasks = _load_tasks(target)
    deps = tasks.get(issue_id, {}).get("depends_on", []) or []
    for dep in deps:
        dep_state = tasks.get(dep, {}).get("status", "draft")
        if dep_state == "review-passed" or dep_state in TERMINAL_STATES:
            continue
        return False, f"unmet dependency: {dep} (state={dep_state})"
    return True, "ok"


# --- Tasks / queue helpers ------------------------------------------------------

def _tasks_path(target: Optional[str] = None) -> str:
    return os.path.join(_state_dir(target), "tasks.json")


def _queue_path(target: Optional[str] = None) -> str:
    return os.path.join(_state_dir(target), "queue.json")


def _approvals_path(target: Optional[str] = None) -> str:
    return os.path.join(_state_dir(target), "approvals.jsonl")


def _runs_dir(target: Optional[str] = None) -> str:
    return os.path.join(_state_dir(target), "runs")


def _load_tasks(target: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    data = _read_json(_tasks_path(target), default={})
    return data or {}


def _load_queue(target: Optional[str] = None) -> Dict[str, List[str]]:
    data = _read_json(_queue_path(target), default=None)
    if data is None:
        return json.loads(json.dumps(DEFAULT_QUEUE))
    # Ensure all QUEUE_STATES keys exist.
    out = json.loads(json.dumps(DEFAULT_QUEUE))
    out.update({k: data.get(k, []) for k in QUEUE_STATES})
    return out


def _save_tasks(tasks: Dict[str, Any], target: Optional[str] = None) -> None:
    _atomic_write_json(_tasks_path(target), tasks)


def _save_queue(queue: Dict[str, List[str]], target: Optional[str] = None) -> None:
    _atomic_write_json(_queue_path(target), queue)


def _append_approval(issue_id: str, action: str, user: str,
                     target: Optional[str] = None) -> None:
    line = json.dumps({
        "ts": time.time(),
        "issue_id": _redact_evidence(issue_id),
        "action": _redact_evidence(action),
        "user": _redact_evidence(user),
    })
    path = _approvals_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --- Config template (G-LP-004) -------------------------------------------------

CONFIG_YML_TEMPLATE = """\
# Laplace runtime configuration. Generated by `state.py init`.
# Hard-safety limits below cannot be weakened by lower-precedence policy layers.
limits:
  max_fix_attempts: %d
  max_pm_clarification_attempts: %d
  max_security_fix_attempts: %d
  max_runtime_minutes_per_issue: %d
  max_files_changed_without_approval: %d
  max_diff_lines_without_approval: %d
  max_stop_hook_iterations: %d
  max_queue_run: %d
  max_parallel: %d
policy:
  require_approval_for:
    - git_push
    - pr_creation
    - release_publish
    - dependency_install
    - mcp_server_add
    - auth_permission_change
    - data_access_change
    - workflow_script_release_change
  merge_policy: %s
redaction:
  enabled: true
  store_raw_command_output: false
""" % (
    MAX_FIX_ATTEMPTS,
    MAX_PM_CLARIFICATION_ATTEMPTS,
    MAX_SECURITY_FIX_ATTEMPTS,
    MAX_RUNTIME_MINUTES_PER_ISSUE,
    MAX_FILES_CHANGED_WITHOUT_APPROVAL,
    MAX_DIFF_LINES_WITHOUT_APPROVAL,
    MAX_STOP_HOOK_ITERATIONS,
    MAX_QUEUE_RUN,
    MAX_PARALLEL,
    DEFAULT_MERGE_POLICY,
)

ROUTING_RULES_TEMPLATE = """\
# Laplace routing rules. Editable; lower precedence than hard safety + config.
routes:
  - match: { type: feature, risk: low }
    agent: dev
    next_phase: review
  - match: { type: feature, risk: high }
    agent: dev
    next_phase: security-review
  - match: { type: bug, risk: low }
    agent: dev
    next_phase: review
  - match: { type: security }
    agent: security
    next_phase: security-review
"""

AGENT_POLICY_TEMPLATE = """\
# Agent policy. Defines per-agent constraints; cannot weaken hard safety.
agents:
  pm:
    model_class: reasoning-heavy
    can_edit_issue: true
    can_run_commands: false
  dev:
    model_class: implementation
    can_edit_code: true
    can_push: false
  review:
    model_class: reasoning-heavy
    can_edit_code: false
  security:
    model_class: reasoning-heavy
    can_edit_code: false
  release:
    model_class: implementation
    can_publish: false
"""

GITIGNORE_TEMPLATE = """\
# Laplace .harness/ mixed tracking policy.
# Tracked (project-wide baseline): config, routing rules, agent policy, memory.
# Ignored (local runtime state, logs, artifacts, issue drafts, worktrees):
state/
logs/
artifacts/
issues/
worktrees/
"""

MEMORY_PROJECT_TEMPLATE = """\
# Project memory

One-paragraph description of the project. Updated by PM agent during intake.
"""

MEMORY_DECISIONS_TEMPLATE = """\
# Decisions

Append-only decision log. Each entry: date - decision - rationale.
"""

MEMORY_CONSTRAINTS_TEMPLATE = """\
# Constraints

Non-negotiable project constraints (compliance, security, architectural).
"""

MEMORY_KNOWN_FAILURES_TEMPLATE = """\
# Known failures

Patterns that previously caused review/security failures. Used to short-circuit
the fix loop when the same failure shape recurs.
"""

DIRECTORY_TREE = [
    "issues",
    "state",
    "state/locks",
    "state/runs",
    "memory",
    "logs",
    "logs/agent-runs",
    "logs/test-runs",
    "artifacts",
    "artifacts/patches",
    "artifacts/pr-drafts",
    "artifacts/reports",
    "artifacts/release",
]


def cmd_init(target: Optional[str] = None) -> int:
    """Create the .harness/ tree per SPEC-002 §Runtime State Layout. Does NOT
    modify any source code outside .harness/.
    """
    root = _harness_root(target)
    harness = os.path.join(root, ".harness")
    os.makedirs(harness, exist_ok=True)
    for rel in DIRECTORY_TREE:
        os.makedirs(os.path.join(harness, rel), exist_ok=True)
    _atomic_write_text(os.path.join(harness, "config.yml"), CONFIG_YML_TEMPLATE)
    _atomic_write_text(os.path.join(harness, "routing-rules.yml"), ROUTING_RULES_TEMPLATE)
    _atomic_write_text(os.path.join(harness, "agent-policy.yml"), AGENT_POLICY_TEMPLATE)
    _atomic_write_text(os.path.join(harness, ".gitignore"), GITIGNORE_TEMPLATE)
    # State seed files
    _save_tasks({}, target=target)
    _save_queue(DEFAULT_QUEUE, target=target)
    # Empty approvals.jsonl (touch)
    ap = _approvals_path(target)
    if not os.path.exists(ap):
        _atomic_write_text(ap, "")
    # Memory seeds
    _atomic_write_text(os.path.join(harness, "memory", "project.md"), MEMORY_PROJECT_TEMPLATE)
    _atomic_write_text(os.path.join(harness, "memory", "decisions.md"), MEMORY_DECISIONS_TEMPLATE)
    _atomic_write_text(os.path.join(harness, "memory", "constraints.md"), MEMORY_CONSTRAINTS_TEMPLATE)
    _atomic_write_text(os.path.join(harness, "memory", "known-failures.md"), MEMORY_KNOWN_FAILURES_TEMPLATE)
    # Profile snapshot placeholder (filled by profile.py in P6 when .moon-cell/ present)
    snapshot_path = os.path.join(harness, "state", "profile-snapshot.json")
    if not os.path.exists(snapshot_path):
        _atomic_write_json(snapshot_path, {
            "status": "not-yet-consumed",
            "moon_cell_present": os.path.isdir(os.path.join(root, ".moon-cell")),
            "ts": time.time(),
        })
    print(f"Initialized {harness}")
    if not os.path.isdir(os.path.join(root, ".moon-cell")):
        print(
            "Moon Cell profile not found.\n"
            "Laplace can run with default local policy.\n"
            "Recommended: use Moon Cell to generate a project-specific harness profile."
        )
    else:
        print("Moon Cell profile detected. Snapshot will be populated by profile.py (P6).")
    print("Next: /laplace:doctor")
    return 0


# --- Config loading + validation (G-LP-004, queue runner config) ---------------

def _parse_config_block(text: str, block: str) -> Dict[str, str]:
    """Minimal hand-parser: extract top-level ``key: value`` lines under the
    given ``block:`` header (e.g. "limits:" or "policy:"). Returns a dict of
    key -> raw value string. stdlib only; no yaml dependency.

    Notes:
      - Only flat scalar keys are supported. Nested blocks (e.g. the
        ``require_approval_for:`` list under ``policy:``) are skipped by
        detecting further indentation.
      - Lines beginning with ``#`` and blank lines are ignored.
    """
    out: Dict[str, str] = {}
    lines = text.splitlines()
    in_block = False
    block_indent: Optional[int] = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if not raw.startswith(" "):
            # Top-level key. Enter block if it matches our target header.
            in_block = stripped == f"{block}:" or stripped.startswith(f"{block}:")
            block_indent = None
            continue
        if not in_block:
            continue
        if block_indent is None:
            block_indent = indent
        # Once we know the block's child indent, only consume lines at that
        # indent. Deeper-indented lines (nested lists/sub-blocks) are skipped.
        if indent != block_indent:
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        out[key.strip()] = value.strip()
    return out


def load_config(target: Optional[str] = None) -> Dict[str, Any]:
    """Read ``<target>/.harness/config.yml`` (target defaults to CWD), parse the
    flat ``limits:`` and ``policy:`` blocks, apply defaults for missing keys,
    and validate values. Returns a dict.

    Exits with code 2 on validation failure (invalid merge_policy or
    non-positive max_queue_run).
    """
    path = os.path.join(_harness_root(target), ".harness", "config.yml")
    if not os.path.exists(path):
        print(f"config.yml not found: {path}", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    limits = _parse_config_block(text, "limits")
    policy = _parse_config_block(text, "policy")

    # max_queue_run: int, default 5, must be positive.
    raw_queue_run = limits.get("max_queue_run")
    if raw_queue_run is None or raw_queue_run == "":
        max_queue_run = MAX_QUEUE_RUN
    else:
        try:
            max_queue_run = int(raw_queue_run)
        except ValueError:
            print(f"invalid max_queue_run (not an int): {raw_queue_run!r}",
                  file=sys.stderr)
            sys.exit(2)
        if max_queue_run <= 0:
            print(f"invalid max_queue_run (must be positive int): {max_queue_run}",
                  file=sys.stderr)
            sys.exit(2)

    # max_parallel: int, default 2, must be positive (ISSUE-0004).
    raw_parallel = limits.get("max_parallel")
    if raw_parallel is None or raw_parallel == "":
        max_parallel = MAX_PARALLEL
    else:
        try:
            max_parallel = int(raw_parallel)
        except ValueError:
            print(f"invalid max_parallel (not an int): {raw_parallel!r}",
                  file=sys.stderr)
            sys.exit(2)
        if max_parallel <= 0:
            print(f"invalid max_parallel (must be positive int): {max_parallel}",
                  file=sys.stderr)
            sys.exit(2)

    # merge_policy: enum, default wait-for-human-merge.
    merge_policy = policy.get("merge_policy") or DEFAULT_MERGE_POLICY
    if merge_policy not in VALID_MERGE_POLICIES:
        print(f"invalid merge_policy: {merge_policy!r} "
              f"(valid: {sorted(VALID_MERGE_POLICIES)})", file=sys.stderr)
        sys.exit(2)

    return {
        "max_queue_run": max_queue_run,
        "max_parallel": max_parallel,
        "merge_policy": merge_policy,
    }


def _find_resumable_queue_run(target: Optional[str] = None) \
        -> Optional[Dict[str, Any]]:
    """Find the most-recent resumable queue run log under .harness/state/runs/.

    A queue run is "resumable" when it is halted on a merge-state outcome
    (ISSUE-0007): ``outcome`` startswith ``"merge-"`` (merge-wait,
    merge-conflict, merge-not-a-git-repo, merge-policy-denied). Queue runs are
    synchronous -- by the time status runs every queue log has ``ended_at``
    set -- so resumable means "halted but resumable", not "live".

    Scans ``*.json`` logs with ``kind == "queue"`` and returns the most recent
    by ``started_at`` (falling back to ``ended_at``), or None.
    """
    runs_dir = _runs_dir(target)
    if not os.path.isdir(runs_dir):
        return None
    candidates: List[Dict[str, Any]] = []
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = _read_json(os.path.join(runs_dir, name), default=None)
        if not isinstance(log, dict):
            continue
        if log.get("kind") != "queue":
            continue
        outcome = log.get("outcome") or ""
        if not isinstance(outcome, str) or not outcome.startswith("merge-"):
            continue
        candidates.append(log)
    if not candidates:
        return None
    candidates.sort(
        key=lambda l: float(l.get("started_at") or l.get("ended_at") or 0.0),
        reverse=True)
    return candidates[0]


def _resumable_queue_current_issue(log: Dict[str, Any],
                                   target: Optional[str] = None) -> str:
    """Resolve the "current issue" for a resumable queue run log.

    Preference order (ISSUE-0007):
      1. The outcome token suffix (``merge-wait:ISSUE-A`` -> ``ISSUE-A``).
      2. The issue_id of the last child run recorded in ``log["issues"]``.
      3. ``"?"`` fallback.
    """
    outcome = log.get("outcome") or ""
    if isinstance(outcome, str) and ":" in outcome:
        suffix = outcome.split(":", 1)[1].strip()
        if suffix:
            return suffix
    issues = log.get("issues") or []
    if issues:
        last_child = issues[-1]
        runs_dir = _runs_dir(target)
        child_log = _read_json(
            os.path.join(runs_dir, f"{last_child}.json"), default=None)
        if isinstance(child_log, dict):
            cid = child_log.get("issue_id")
            if isinstance(cid, str) and cid:
                return cid
    return "?"


def _find_active_parallel_run(target: Optional[str] = None) \
        -> Optional[Dict[str, Any]]:
    """Find the most-recent active parallel-run log under .harness/state/runs/.

    A parallel-run log (ISSUE-0004) is "active" when it is not finalized:
    ``kind == "parallel-queue"`` AND ``outcome`` is None or a wave-dispatched
    interim outcome (``wave-dispatched`` or ``wave-dispatched:waiting``).
    Finalized outcomes (``queue-exhausted``, ``cancelled``, ``start-failed:*``)
    are inactive. Returns the most recent by ``started_at``, or None.

    Used by ``_format_status`` (AC-PQ-009) and ``cancel`` (AC-PQ-010).
    """
    runs_dir = _runs_dir(target)
    if not os.path.isdir(runs_dir):
        return None
    candidates: List[Dict[str, Any]] = []
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = _read_json(os.path.join(runs_dir, name), default=None)
        if not isinstance(log, dict):
            continue
        if log.get("kind") != "parallel-queue":
            continue
        outcome = log.get("outcome")
        if outcome is not None and outcome not in (
                "wave-dispatched", "wave-dispatched:waiting"):
            continue
        candidates.append(log)
    if not candidates:
        return None
    candidates.sort(
        key=lambda l: float(l.get("started_at") or 0.0),
        reverse=True)
    return candidates[0]


def _parallel_in_flight_pairs(log: Dict[str, Any],
                              target: Optional[str] = None) \
        -> List[Tuple[str, str]]:
    """Return (issue_id, worktree_path) pairs for in-flight children.

    In-flight = the child run's issue is in a non-terminal status (matches
    the scheduler's in_flight set). Reads each child run log for its
    ``issue_id`` and ``worktree_path``. Pairs with a missing worktree path
    fall back to "(no worktree)".
    """
    tasks = _load_tasks(target)
    runs_dir = _runs_dir(target)
    pairs: List[Tuple[str, str]] = []
    for child_id in (log.get("issues") or []):
        child_path = os.path.join(runs_dir, f"{child_id}.json")
        child = _read_json(child_path, default=None)
        if not isinstance(child, dict):
            continue
        issue_id = child.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id:
            continue
        status = tasks.get(issue_id, {}).get("status")
        if status in TERMINAL_STATES:
            continue
        wt = child.get("worktree_path") or "(no worktree)"
        pairs.append((issue_id, wt))
    return pairs


def _format_status(target: Optional[str] = None) -> str:
    tasks = _load_tasks(target)
    queue = _load_queue(target)
    # Find an active in-progress run, if any.
    active_run: Optional[Dict[str, Any]] = None
    active_issue: Optional[str] = None
    runs_dir = _runs_dir(target)
    for tid, meta in tasks.items():
        if meta.get("status") == "in-progress" and meta.get("run_id"):
            run_path = os.path.join(runs_dir, f"{meta['run_id']}.json")
            run = _read_json(run_path, default=None)
            if run:
                active_run = run
                active_issue = tid
                break
    lines: List[str] = ["Harness status.", ""]
    lines.append("Queue:")
    for state in QUEUE_STATES:
        lines.append(f"  {state}: {len(queue.get(state, []))}")
    lines.append("")
    lines.append("Active run:")
    if active_run and active_issue:
        lines.append(f"  Run: {active_run.get('run_id', '?')}")
        lines.append(f"  Issue: {active_issue}")
        lines.append(f"  State: {tasks[active_issue].get('status', '?')}")
        lines.append(f"  Agent: {active_run.get('agent', '?')}")
        attempt = active_run.get("attempt", 0)
        lines.append(f"  Attempt: {attempt}/{MAX_FIX_ATTEMPTS}")
        evidence = active_run.get("evidence", []) or []
        last = evidence[-1] if evidence else "none"
        lines.append(f"  Last evidence: {last}")
    else:
        lines.append("  (no active run)")
    # Resumable queue run block (ISSUE-0007). Only emitted when a resumable
    # merge-* queue log exists, so AC-QR-019 byte-identical output holds
    # when no such log is present.
    resumable = _find_resumable_queue_run(target)
    if resumable is not None:
        step = len(resumable.get("issues") or [])
        lines.append("")
        lines.append("Queue run:")
        lines.append(f"  run id: {resumable.get('run_id', '?')}")
        lines.append(
            f"  current issue: "
            f"{_resumable_queue_current_issue(resumable, target)}")
        lines.append(f"  step: {step}")
        lines.append(
            f"  merge policy: {resumable.get('merge_policy', '?')}")
        lines.append(f"  consecutive: {step}")
    # Active parallel-run block (ISSUE-0004, AC-PQ-009). Only emitted when
    # an active (non-finalized) parallel-queue log exists, so the output is
    # byte-identical when no parallel run is active (AC-PQ-011).
    parallel = _find_active_parallel_run(target)
    if parallel is not None:
        waves = parallel.get("waves") or []
        wave_count = len(waves)
        in_flight_pairs = _parallel_in_flight_pairs(parallel, target)
        halted = parallel.get("halted") or []
        lines.append("")
        lines.append("Parallel run:")
        lines.append(f"  run id: {parallel.get('run_id', '?')}")
        lines.append(f"  wave: {wave_count}")
        lines.append(f"  in-flight: {len(in_flight_pairs)}")
        for iid, wt in in_flight_pairs:
            lines.append(f"    {iid} @ {wt}")
        lines.append(f"  halted: {len(halted)}")
        if halted:
            lines.append(f"    {', '.join(halted)}")
    lines.append("")
    lines.append("Next action:")
    if queue.get("approved") and not active_run:
        first = queue["approved"][0]
        lines.append(f"  /laplace:run {first}")
    elif queue.get("draft"):
        first = queue["draft"][0]
        lines.append(f"  /laplace:approve {first}")
    elif active_run:
        lines.append("  await current run completion or /laplace:status")
    else:
        lines.append("  /laplace:intake <prd> to create draft issues")
    return "\n".join(lines)


def cmd_status(target: Optional[str] = None) -> int:
    print(_format_status(target))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.target)
    rows = []
    for tid, meta in sorted(tasks.items()):
        if args.status and meta.get("status") != args.status:
            continue
        rows.append(f"{tid}\t{meta.get('status', '?')}\t{meta.get('updated_at', '?')}")
    if not rows:
        print("(no issues)")
    else:
        print("\n".join(rows))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    issue_path = os.path.join(_issues_dir(args.target), f"{args.issue_id}.md")
    if not os.path.exists(issue_path):
        print(f"issue not found: {args.issue_id}", file=sys.stderr)
        return 1
    with open(issue_path, "r", encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def _set_issue_state(issue_id: str, new_state: str, target: Optional[str] = None,
                     run_id: Optional[str] = None, attempt: Optional[int] = None) -> None:
    tasks = _load_tasks(target)
    t = tasks.get(issue_id, {})
    t["status"] = new_state
    t["updated_at"] = time.time()
    if run_id is not None:
        t["run_id"] = run_id
    if attempt is not None:
        t["attempts"] = attempt
    tasks[issue_id] = t
    _save_tasks(tasks, target=target)
    # Update queue: insert into the matching queue state if it's a queue-tracked state.
    queue = _load_queue(target)
    for state in QUEUE_STATES:
        if issue_id in queue[state] and state != new_state:
            queue[state] = [x for x in queue[state] if x != issue_id]
    if new_state in QUEUE_STATES and issue_id not in queue[new_state]:
        queue[new_state].append(issue_id)
    _save_queue(queue, target=target)


def cmd_approve(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.target)
    issue_id = args.issue_id
    current = tasks.get(issue_id, {}).get("status", "draft")
    ok, reason = validate_transition(current, "approved")
    if not ok:
        print(f"cannot approve {issue_id}: {reason}", file=sys.stderr)
        return 2
    ok, reason = _check_dependency_graph(issue_id, target=args.target)
    if not ok:
        print(reason, file=sys.stderr)
        return 2
    _set_issue_state(issue_id, "approved", target=args.target)
    _append_approval(issue_id, "approve", args.user or os.environ.get("USER", "unknown"),
                     target=args.target)
    print(f"approved {issue_id}: {current} -> approved")
    return 0


def cmd_transition(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.target)
    issue_id = args.issue_id
    current = tasks.get(issue_id, {}).get("status", "draft")
    ok, reason = validate_transition(current, args.new_state)
    if not ok:
        print(f"invalid transition: {reason}", file=sys.stderr)
        return 2
    _set_issue_state(issue_id, args.new_state, target=args.target)
    print(f"transitioned {issue_id}: {current} -> {args.new_state}")
    return 0


def cmd_run_start(args: argparse.Namespace) -> int:
    issue_id = args.issue_id
    tasks = _load_tasks(args.target)
    current = tasks.get(issue_id, {}).get("status")
    if current not in {"approved", "pm-review", "ready-for-dev", "needs-fix"}:
        print(f"cannot start run for {issue_id} in state {current}", file=sys.stderr)
        return 2
    ok, reason = acquire_lock(issue_id, target=args.target)
    if not ok:
        print(f"lock failed for {issue_id}: {reason}", file=sys.stderr)
        return 3
    run_id = hashlib.sha1(f"{issue_id}-{time.time()}".encode("utf-8")).hexdigest()[:12]
    run = {
        "run_id": run_id,
        "issue_id": _redact_evidence(issue_id),
        "started_at": time.time(),
        "ended_at": None,
        "outcome": None,
        "agent": args.agent or "dev",
        "attempt": args.attempt or 1,
        "evidence": [],
    }
    _atomic_write_json(os.path.join(_runs_dir(args.target), f"{run_id}.json"), run)
    _set_issue_state(issue_id, "in-progress", target=args.target, run_id=run_id,
                     attempt=args.attempt or 1)
    print(f"run-start {run_id} for {issue_id}")
    return 0


def cmd_run_end(args: argparse.Namespace) -> int:
    run_path = os.path.join(_runs_dir(args.target), f"{args.run_id}.json")
    run = _read_json(run_path, default=None)
    if not run:
        print(f"run not found: {args.run_id}", file=sys.stderr)
        return 1
    run["ended_at"] = time.time()
    run["outcome"] = args.outcome or "completed"
    if args.evidence:
        run.setdefault("evidence", []).append(_redact_evidence(args.evidence))
    _atomic_write_json(run_path, run)
    issue_id = run.get("issue_id") or ""
    # Release lock
    if issue_id:
        release_lock(issue_id, target=args.target)
    print(f"run-end {args.run_id}: outcome={run['outcome']}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    ok, reason = acquire_lock(args.issue_id, target=args.target)
    print(f"lock {args.issue_id}: {'ok' if ok else 'failed'} ({reason})")
    return 0 if ok else 3


def cmd_unlock(args: argparse.Namespace) -> int:
    ok, reason = release_lock(args.issue_id, target=args.target)
    print(f"unlock {args.issue_id}: {reason}")
    return 0


def _issue_has_run_history(issue_id: str, target: Optional[str] = None) -> bool:
    """Defense-in-depth check: returns True if any run log under
    .harness/state/runs/ references this issue_id. Used by cmd_discard to
    refuse deletion of issues with run history even if status was forced
    back to draft.
    """
    runs_dir = _runs_dir(target)
    if not os.path.isdir(runs_dir):
        return False
    for name in os.listdir(runs_dir):
        if not name.endswith(".json"):
            continue
        log = _read_json(os.path.join(runs_dir, name), default=None)
        if isinstance(log, dict) and log.get("issue_id") == issue_id:
            return True
    return False


def cmd_discard(args: argparse.Namespace) -> int:
    """Remove a DRAFT issue atomically (ISSUE-0011, AC-SI-004/005).

    Draft-only safety boundary: refuses any non-draft issue (exit 2). Also
    refuses if the issue has any run history (exit 2) as defense-in-depth
    even when status is draft. Removes the issue from tasks.json, ALL queue
    states (QUEUE_STATES), and deletes .harness/issues/<id>.md. Atomic per
    file: on any write failure the JSON files are rolled back to their
    pre-mutation snapshot and the command exits 1.
    """
    issue_id = args.issue_id
    target = args.target
    ok, reason = acquire_lock(INTAKE_LOCK_ID, target=target)
    if not ok:
        print(f"discard lock failed for {issue_id}: {reason}", file=sys.stderr)
        return 3
    try:
        tasks = _load_tasks(target)
        if issue_id not in tasks:
            print(f"cannot discard {issue_id}: not found", file=sys.stderr)
            return 2
        rec = tasks.get(issue_id, {}) or {}
        if rec.get("status") != "draft":
            print(f"cannot discard {issue_id}: only draft allowed "
                  f"(status={rec.get('status')})", file=sys.stderr)
            return 2
        # Defense-in-depth: refuse if any run-log references this issue, or
        # the tasks record carries a run_id (shouldn't happen for draft, but
        # the guard is cheap and the safety boundary is load-bearing).
        if rec.get("run_id") or _issue_has_run_history(issue_id, target=target):
            print(f"cannot discard {issue_id}: run history exists", file=sys.stderr)
            return 2

        # Snapshot prior state for rollback.
        prior_tasks = json.loads(json.dumps(tasks))
        prior_queue = _load_queue(target)

        try:
            # Mutate tasks.json
            new_tasks = json.loads(json.dumps(tasks))
            new_tasks.pop(issue_id, None)
            _save_tasks(new_tasks, target=target)
            # Mutate queue.json: remove from every queue state.
            new_queue = json.loads(json.dumps(prior_queue))
            for state in QUEUE_STATES:
                if issue_id in new_queue.get(state, []):
                    new_queue[state] = [x for x in new_queue[state]
                                        if x != issue_id]
            _save_queue(new_queue, target=target)
            # Delete the issue file last.
            issue_path = os.path.join(_issues_dir(target), f"{issue_id}.md")
            if os.path.exists(issue_path):
                os.remove(issue_path)
        except Exception as exc:  # noqa: BLE001 — rollback path
            # Restore JSON snapshots; best-effort file restore.
            try:
                _save_tasks(prior_tasks, target=target)
            except Exception:
                pass
            try:
                _save_queue(prior_queue, target=target)
            except Exception:
                pass
            print(f"discard {issue_id} failed: {exc} (state rolled back)",
                  file=sys.stderr)
            return 1
    finally:
        release_lock(INTAKE_LOCK_ID, target=target)

    print(f"discarded {issue_id}: draft -> (removed)")
    return 0


# --- selftest -------------------------------------------------------------------

def selftest() -> int:
    import tempfile
    failures: List[str] = []
    tmp = tempfile.mkdtemp(prefix="laplace-selftest-")
    # Silence stdout noise from init/approve/etc. so selftest output is clean.
    saved_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        # 1. init creates expected tree, and only writes under .harness/.
        before = set(os.listdir(tmp))
        rc = cmd_init(target=tmp)
        if rc != 0:
            failures.append(f"init returned {rc}")
        after = set(os.listdir(tmp))
        new_entries = after - before
        if new_entries != {".harness"}:
            failures.append(f"init created entries outside .harness/: {new_entries}")
        for rel in ["config.yml", "routing-rules.yml", "agent-policy.yml", ".gitignore",
                    "issues", "state/tasks.json", "state/queue.json", "state/approvals.jsonl",
                    "state/runs", "state/locks", "memory/project.md", "logs",
                    "artifacts/patches"]:
            p = os.path.join(tmp, ".harness", rel)
            if not os.path.exists(p):
                failures.append(f"init missing {rel}")

        # 2. atomic write round-trip
        path = os.path.join(tmp, ".harness", "state", "rt.json")
        _atomic_write_json(path, {"x": 1, "nested": {"y": [1, 2]}})
        back = _read_json(path)
        if back != {"x": 1, "nested": {"y": [1, 2]}}:
            failures.append(f"atomic round-trip mismatch: {back}")
        if os.path.exists(path + ".tmp"):
            failures.append("tmp file left behind after atomic write")

        # 3. lock acquire + release + stale reuse
        ok, _ = acquire_lock("ISSUE-LOCK", target=tmp)
        if not ok:
            failures.append("lock acquire failed")
        ok2, _ = acquire_lock("ISSUE-LOCK", target=tmp)
        if ok2:
            failures.append("double-acquire should fail")
        ok3, _ = release_lock("ISSUE-LOCK", target=tmp)
        if not ok3:
            failures.append("release failed")
        ok4, _ = acquire_lock("ISSUE-LOCK", target=tmp)
        if not ok4:
            failures.append("re-acquire after release failed")
        release_lock("ISSUE-LOCK", target=tmp)  # cleanup for later steps

        # 4. state machine: reject invalid transition
        v, _ = validate_transition("draft", "approved")
        if not v:
            failures.append("draft->approved should be valid")
        v, _ = validate_transition("draft", "in-progress")
        if v:
            failures.append("draft->in-progress should be invalid")
        v, _ = validate_transition("review", "security-review")
        if not v:
            failures.append("review->security-review should be valid")

        # 5. approve writes approval + transitions state
        # Seed a draft issue into tasks/queue.
        _save_tasks({"ISSUE-0001": {"status": "draft", "updated_at": time.time()}}, target=tmp)
        q = _load_queue(target=tmp)
        q["draft"].append("ISSUE-0001")
        _save_queue(q, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0001", user="tester", target=tmp)
        rc = cmd_approve(ns)
        if rc != 0:
            failures.append(f"approve returned {rc}")
        tasks = _load_tasks(target=tmp)
        if tasks.get("ISSUE-0001", {}).get("status") != "approved":
            failures.append(f"approve did not transition: {tasks}")
        with open(_approvals_path(target=tmp), "r", encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        if not lines:
            failures.append("no approval recorded")

        # 5b. dependency graph: missing ref rejected (rc=2)
        _save_tasks({
            "ISSUE-0001": {"status": "approved", "updated_at": time.time()},
            "ISSUE-0100": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-9999"]},
        }, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0100", user="tester", target=tmp)
        rc = cmd_approve(ns)
        if rc != 2:
            failures.append(f"approve with missing dep should rc=2, got {rc}")

        # 5c. dependency graph: self-cycle rejected (rc=2)
        _save_tasks({
            "ISSUE-0001": {"status": "approved", "updated_at": time.time()},
            "ISSUE-0200": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-0200"]},
        }, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0200", user="tester", target=tmp)
        rc = cmd_approve(ns)
        if rc != 2:
            failures.append(f"approve with self-cycle should rc=2, got {rc}")

        # 5d. dependency graph: two-node cycle A->B->A rejected (rc=2)
        _save_tasks({
            "ISSUE-0001": {"status": "approved", "updated_at": time.time()},
            "ISSUE-0300": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-0301"]},
            "ISSUE-0301": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-0300"]},
        }, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0300", user="tester", target=tmp)
        rc = cmd_approve(ns)
        if rc != 2:
            failures.append(f"approve with A->B->A cycle should rc=2, got {rc}")

        # 5e. dependency graph: valid existing deps approve succeeds
        _save_tasks({
            "ISSUE-0001": {"status": "review-passed", "updated_at": time.time()},
            "ISSUE-0400": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-0001"]},
        }, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0400", user="tester", target=tmp)
        rc = cmd_approve(ns)
        if rc != 0:
            failures.append(f"approve with valid existing dep should succeed, got {rc}")
        tasks = _load_tasks(target=tmp)
        if tasks.get("ISSUE-0400", {}).get("status") != "approved":
            failures.append("ISSUE-0400 should be approved after valid dep")

        # 5f. _dependencies_satisfied stub semantics
        ok, _ = _dependencies_satisfied("ISSUE-0400", target=tmp)
        if not ok:
            failures.append("_dependencies_satisfied should pass when dep is review-passed")
        _save_tasks({
            "ISSUE-0500": {"status": "draft", "updated_at": time.time(),
                           "depends_on": ["ISSUE-0501"]},
            "ISSUE-0501": {"status": "in-progress", "updated_at": time.time()},
        }, target=tmp)
        ok, reason = _dependencies_satisfied("ISSUE-0500", target=tmp)
        if ok or "unmet dependency" not in reason:
            failures.append(f"_dependencies_satisfied should fail on in-progress dep: {reason}")

        # 6. run-start / run-end round trip
        # Reset ISSUE-0001 to approved so run-start accepts it (block 5 left it
        # in review-passed to exercise the dep-satisfaction stub).
        _save_tasks({
            "ISSUE-0001": {"status": "approved", "updated_at": time.time()},
        }, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-0001", agent="dev", attempt=1, target=tmp)
        rc = cmd_run_start(ns)
        if rc != 0:
            failures.append(f"run-start returned {rc}")
        run_files = [f for f in os.listdir(_runs_dir(target=tmp)) if f.endswith(".json")]
        if len(run_files) != 1:
            failures.append(f"expected 1 run file, got {run_files}")
        rid = run_files[0][:-len(".json")] if run_files else ""
        ns_end = argparse.Namespace(run_id=rid, outcome="review-passed",
                                     evidence="tests: 12/12 passed", target=tmp)
        rc = cmd_run_end(ns_end)
        if rc != 0:
            failures.append(f"run-end returned {rc}")
        run = _read_json(os.path.join(_runs_dir(target=tmp), f"{rid}.json"))
        if not run or run.get("outcome") != "review-passed":
            failures.append(f"run outcome not recorded: {run}")
        if run and not run.get("evidence"):
            failures.append("evidence not recorded on run-end")

        # 7. redaction is applied to persisted evidence (G-LP-003)
        ns_ev = argparse.Namespace(run_id=rid, outcome="blocked",
                                    evidence="Authorization: Bearer " + "a" * 24, target=tmp)
        rc = cmd_run_end(ns_ev)
        if rc != 0:
            failures.append(f"run-end(ev) returned {rc}")
        run = _read_json(os.path.join(_runs_dir(target=tmp), f"{rid}.json"))
        if "a" * 24 in json.dumps(run):
            failures.append("evidence not redacted in run log")

        # 8. config.yml contains all G-LP-004 limits
        with open(os.path.join(tmp, ".harness", "config.yml"), "r", encoding="utf-8") as f:
            cfg_text = f.read()
        for limit in ["max_fix_attempts", "max_pm_clarification_attempts",
                      "max_security_fix_attempts", "max_runtime_minutes_per_issue",
                      "max_files_changed_without_approval", "max_diff_lines_without_approval",
                      "max_stop_hook_iterations", "max_queue_run", "max_parallel",
                      "merge_policy"]:
            if limit not in cfg_text:
                failures.append(f"config.yml missing {limit}")

        # 9. cmd_discard: draft issue removed atomically (AC-SI-004)
        # Seed a fresh draft issue + .md file.
        _save_tasks({
            "ISSUE-DISCARD-1": {"status": "draft", "updated_at": time.time()},
        }, target=tmp)
        dq = _load_queue(target=tmp)
        dq["draft"].append("ISSUE-DISCARD-1")
        _save_queue(dq, target=tmp)
        dpath = os.path.join(_issues_dir(target=tmp), "ISSUE-DISCARD-1.md")
        _atomic_write_text(dpath, "# ISSUE-DISCARD-1\n")
        ns = argparse.Namespace(issue_id="ISSUE-DISCARD-1", target=tmp)
        rc = cmd_discard(ns)
        if rc != 0:
            failures.append(f"discard draft should rc=0, got {rc}")
        t = _load_tasks(target=tmp)
        if "ISSUE-DISCARD-1" in t:
            failures.append("discard did not remove issue from tasks.json")
        qq = _load_queue(target=tmp)
        if any("ISSUE-DISCARD-1" in qq.get(s, []) for s in QUEUE_STATES):
            failures.append("discard did not remove issue from all queue states")
        if os.path.exists(dpath):
            failures.append("discard did not delete issue .md file")

        # 10. cmd_discard: non-draft issue exits 2 (AC-SI-005)
        _save_tasks({
            "ISSUE-DISCARD-2": {"status": "approved", "updated_at": time.time()},
        }, target=tmp)
        dq2 = _load_queue(target=tmp)
        dq2["approved"].append("ISSUE-DISCARD-2")
        _save_queue(dq2, target=tmp)
        ns = argparse.Namespace(issue_id="ISSUE-DISCARD-2", target=tmp)
        rc = cmd_discard(ns)
        if rc != 2:
            failures.append(f"discard non-draft should rc=2, got {rc}")
        if "ISSUE-DISCARD-2" not in _load_tasks(target=tmp):
            failures.append("discard non-draft mutated tasks.json")

        # 11. cmd_discard: missing issue exits 2
        ns = argparse.Namespace(issue_id="ISSUE-NOPE", target=tmp)
        rc = cmd_discard(ns)
        if rc != 2:
            failures.append(f"discard missing should rc=2, got {rc}")

        # 12. cmd_discard: run-history defense — draft status but a run log
        # references the issue -> rc=2, no state change.
        _save_tasks({
            "ISSUE-DISCARD-3": {"status": "draft", "updated_at": time.time()},
        }, target=tmp)
        dq3 = _load_queue(target=tmp)
        dq3["draft"].append("ISSUE-DISCARD-3")
        _save_queue(dq3, target=tmp)
        _atomic_write_json(
            os.path.join(_runs_dir(target=tmp), "fakedeadbeef.json"),
            {"run_id": "fakedeadbeef", "issue_id": "ISSUE-DISCARD-3",
             "started_at": time.time(), "ended_at": None, "outcome": None,
             "agent": "dev", "attempt": 1, "evidence": []})
        ns = argparse.Namespace(issue_id="ISSUE-DISCARD-3", target=tmp)
        rc = cmd_discard(ns)
        if rc != 2:
            failures.append(f"discard with run history should rc=2, got {rc}")
        if "ISSUE-DISCARD-3" not in _load_tasks(target=tmp):
            failures.append("discard with run history mutated tasks.json")
        # cleanup so subsequent lock test isn't polluted
        os.remove(os.path.join(_runs_dir(target=tmp), "fakedeadbeef.json"))

        # 12b. cmd_discard: malformed runs/*.json must not crash (rc in {0,2}).
        _save_tasks({
            "ISSUE-DISCARD-3B": {"status": "draft", "updated_at": time.time()},
        }, target=tmp)
        dq3b = _load_queue(target=tmp)
        dq3b["draft"].append("ISSUE-DISCARD-3B")
        _save_queue(dq3b, target=tmp)
        with open(os.path.join(_runs_dir(target=tmp), "bad.json"), "w") as bf:
            bf.write("{ this is not valid json")
        ns = argparse.Namespace(issue_id="ISSUE-DISCARD-3B", target=tmp)
        try:
            rc = cmd_discard(ns)
        except Exception as exc:  # noqa: BLE001 - any crash is a failure here
            failures.append(f"discard crashed on malformed runs json: {exc!r}")
            rc = -1
        if rc not in (0, 2):
            failures.append(f"discard with malformed runs json should rc in (0,2), got {rc}")
        os.remove(os.path.join(_runs_dir(target=tmp), "bad.json"))

        # 13. cmd_discard: lock contention exits 3
        ok, _ = acquire_lock(INTAKE_LOCK_ID, target=tmp)
        if not ok:
            failures.append("setup acquire INTAKE_LOCK_ID failed")
        else:
            _save_tasks({
                "ISSUE-DISCARD-4": {"status": "draft", "updated_at": time.time()},
            }, target=tmp)
            ns = argparse.Namespace(issue_id="ISSUE-DISCARD-4", target=tmp)
            rc = cmd_discard(ns)
            if rc != 3:
                failures.append(f"discard under lock contention should rc=3, got {rc}")
            if "ISSUE-DISCARD-4" not in _load_tasks(target=tmp):
                failures.append("discard under contention mutated tasks.json")
            release_lock(INTAKE_LOCK_ID, target=tmp)
    finally:
        sys.stdout.close()
        sys.stdout = saved_stdout
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("state selftest: PASS")
    return 0


def _add_target_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", default=None,
                   help="Repository root containing .harness/ (default: CWD)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="state.py", description="Laplace state engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Create the .harness/ tree")
    _add_target_arg(p)
    p.set_defaults(func=lambda a: cmd_init(a.target))

    p = sub.add_parser("status", help="Print harness status")
    _add_target_arg(p)
    p.set_defaults(func=lambda a: cmd_status(a.target))

    p = sub.add_parser("list", help="List issues")
    _add_target_arg(p)
    p.add_argument("--status", default=None, help="Filter by state")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="Show an issue file")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="Transition draft -> approved")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.add_argument("--user", default=None)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("transition", help="Generic state-machine transition")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.add_argument("new_state")
    p.set_defaults(func=cmd_transition)

    p = sub.add_parser("run-start", help="Start a run")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.add_argument("--agent", default=None)
    p.add_argument("--attempt", type=int, default=None)
    p.set_defaults(func=cmd_run_start)

    p = sub.add_parser("run-end", help="End a run")
    _add_target_arg(p)
    p.add_argument("run_id")
    p.add_argument("--outcome", default=None)
    p.add_argument("--evidence", default=None)
    p.set_defaults(func=cmd_run_end)

    p = sub.add_parser("lock", help="Acquire issue lock")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("unlock", help="Release issue lock")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("discard", help="Remove a draft issue (atomic, draft-only)")
    _add_target_arg(p)
    p.add_argument("issue_id")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
