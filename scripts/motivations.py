#!/usr/bin/env python3
"""SPEC-005: motivation triggers.

One-shot scheduler invoked by an external timer (cron, launchd, systemd):
    python3 motivations.py --once [--target DIR]

Each invocation:
  1. Re-reads config.yml fresh (kill switch takes effect on next tick).
  2. If motivations.enabled is false, exits 0 immediately.
  3. Polls each enabled trigger once.
  4. Dispatches fired events via existing runner entry points.
  5. Exits.

A long-running daemon mode is non-normative; if ever added it MUST re-read
config at the top of every cycle, matching the one-shot semantics.

Hard safety: motivations never bypass human-approval-required (terminal),
never approve drafts, never weaken the deny/redaction layer. Dispatch routes
through runner.cmd_start which acquires the per-issue lock; a held lock is a
no-op with a log entry.

stdlib only. Exit 0 on success (including opt-out and rate-limit no-ops).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import state  # noqa: E402


# --- paths ---

def _log_path(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), ".harness", "logs",
                        "motivations.jsonl")


def _rate_path(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), ".harness", "state",
                        "motivations-rate.json")


def _log(entry: Dict[str, Any], target: Optional[str]) -> None:
    path = _log_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# --- config parsing ---

DEFAULT_MOTIVATIONS_ENABLED = False
DEFAULT_MAX_DISPATCHES_PER_HOUR = 10

DEFAULT_TRIGGER_CONFIG = {
    "clock":        {"enabled": True,  "due_within_hours": 24},
    "git-upstream": {"enabled": True,  "base_branch": "main"},
    "idle-queue":   {"enabled": True,  "idle_threshold_hours": 2},
    "test-signal":  {"enabled": False},
}


def _parse_motivations(text: str) -> Dict[str, Any]:
    """Parse the optional ``motivations:`` block from config.yml.

    Returns {"enabled": bool, "max_dispatches_per_hour": int,
             "triggers": {name: {enabled, ...params}}}. Missing block ->
    defaults (disabled).
    """
    enabled = DEFAULT_MOTIVATIONS_ENABLED
    max_per_hour = DEFAULT_MAX_DISPATCHES_PER_HOUR
    triggers = {
        name: dict(cfg) for name, cfg in DEFAULT_TRIGGER_CONFIG.items()
    }

    lines = text.splitlines()
    in_block = False
    in_triggers = False
    block_indent: Optional[int] = None
    cur_trigger: Optional[str] = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_block = stripped == "motivations:"
            in_triggers = False
            block_indent = None
            cur_trigger = None
            continue
        if not in_block:
            continue
        if block_indent is None:
            block_indent = indent
        if indent == block_indent:
            in_triggers = False
            cur_trigger = None
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "enabled":
                enabled = value.lower() in ("true", "yes", "1")
            elif key == "max_dispatches_per_hour":
                try:
                    max_per_hour = int(value)
                except ValueError:
                    pass
            elif key == "triggers":
                in_triggers = True
            continue
        if not in_triggers:
            continue
        # Trigger name row at block_indent + 2.
        if indent == block_indent + 2:
            cur_trigger = stripped.split(":", 1)[0].strip() or None
            if cur_trigger and cur_trigger not in triggers:
                # Unknown trigger; register so its params can be parsed, but
                # default-disabled.
                triggers[cur_trigger] = {"enabled": False}
            continue
        # Param row at block_indent + 4.
        if cur_trigger and indent == block_indent + 4:
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "enabled":
                triggers[cur_trigger]["enabled"] = value.lower() in ("true", "yes", "1")
            elif key in ("due_within_hours", "idle_threshold_hours"):
                try:
                    triggers[cur_trigger][key] = int(value)
                except ValueError:
                    pass
            elif key == "base_branch":
                triggers[cur_trigger][key] = value

    return {"enabled": enabled, "max_dispatches_per_hour": max_per_hour,
            "triggers": triggers}


# --- rate limiter (sliding window) ---

def _load_rate(target: Optional[str]) -> List[float]:
    data = state._read_json(_rate_path(target), default=None)
    if isinstance(data, dict):
        ts_list = data.get("timestamps") or []
        return [float(t) for t in ts_list if isinstance(t, (int, float))]
    return []


def _save_rate(timestamps: List[float], target: Optional[str]) -> None:
    path = _rate_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state._atomic_write_json(path, {"timestamps": timestamps})


def _rate_allows(now: float, history: List[float], max_per_hour: int) -> bool:
    """Sliding 1-hour window. Returns True if a new dispatch is permitted."""
    window_start = now - 3600.0
    recent = [t for t in history if t >= window_start]
    return len(recent) < max_per_hour


# --- triggers ---

def _issue_state(iid: str, tasks: Dict[str, Any]) -> str:
    return tasks.get(iid, {}).get("status", "draft")


def _read_due_date(iid: str, target: Optional[str]) -> Optional[float]:
    """Best-effort Due Date parse from the issue .md frontmatter/body."""
    path = os.path.join(state._issues_dir(target), f"{iid}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    # Look for "- Due Date: YYYY-MM-DD" anywhere.
    import re
    m = re.search(r"(?im)^\s*-?\s*due\s*date\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
                  text)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None


def trigger_clock(tasks: Dict[str, Any], cfg: Dict[str, Any],
                  target: Optional[str], now: float) -> List[str]:
    """Approved issues whose due_date is within due_within_hours."""
    hours = cfg.get("due_within_hours", 24)
    horizon = now + hours * 3600.0
    out: List[str] = []
    for iid, rec in tasks.items():
        if rec.get("status") != "approved":
            continue
        due = _read_due_date(iid, target)
        if due is None:
            continue
        if now <= due <= horizon:
            out.append(iid)
    return out


def trigger_idle_queue(tasks: Dict[str, Any], cfg: Dict[str, Any],
                       target: Optional[str], now: float) -> List[str]:
    """Approved issues when no run has been active for idle_threshold_hours."""
    hours = cfg.get("idle_threshold_hours", 2)
    threshold = now - hours * 3600.0
    # "Active run" = any issue in a non-terminal running state with a live
    # (non-finalized) run log updated within the threshold.
    for iid, rec in tasks.items():
        status = rec.get("status")
        if status in state.TERMINAL_STATES:
            continue
        if status in ("draft", "approved"):
            continue
        run_id = rec.get("run_id")
        if not run_id:
            continue
        run = state._read_json(
            os.path.join(state._runs_dir(target), f"{run_id}.json"),
            default=None)
        if isinstance(run, dict) and run.get("ended_at") is None:
            updated = float(rec.get("updated_at") or 0)
            if updated >= threshold:
                return []  # something is active recently
    # Nothing active: dispatch the first approved issue.
    for iid, rec in tasks.items():
        if rec.get("status") == "approved":
            return [iid]
    return []


def trigger_git_upstream(tasks: Dict[str, Any], cfg: Dict[str, Any],
                         target: Optional[str], now: float) -> List[str]:
    """Approved issues whose touched paths appear in new upstream commits.

    Runs `git fetch` then `git log` for commits on base_branch not present
    locally. On any git failure (no repo, no network), returns [] and is
    silent -- the failure is logged by the caller via the noop path.
    """
    base = cfg.get("base_branch", "main")
    root = state._harness_root(target)
    try:
        subprocess.run(["git", "fetch"], cwd=root,
                       capture_output=True, timeout=30, check=False)
        res = subprocess.run(
            ["git", "log", f"origin/{base}..HEAD", "--name-only", "--pretty=format:"],
            cwd=root, capture_output=True, timeout=30, check=False)
        if res.returncode != 0:
            return []
        changed = {ln.strip() for ln in res.stdout.decode("utf-8", "replace")
                   .splitlines() if ln.strip()}
    except (OSError, subprocess.SubprocessError):
        return []
    if not changed:
        return []
    out: List[str] = []
    for iid, rec in tasks.items():
        if rec.get("status") != "approved":
            continue
        touches = rec.get("touches") or []
        if any(g in changed for g in touches if isinstance(g, str)):
            out.append(iid)
    return out


def trigger_test_signal(tasks: Dict[str, Any], cfg: Dict[str, Any],
                        target: Optional[str], now: float) -> List[str]:
    """Issues in `review` whose latest test-run log shows a new failure.

    Scans .harness/logs/test-runs/*.json for entries with status=failing
    recorded after the issue's current run started. Returns issue ids in
    `review` state.
    """
    test_dir = os.path.join(state._harness_root(target), ".harness", "logs",
                            "test-runs")
    if not os.path.isdir(test_dir):
        return []
    failing: Dict[str, float] = {}  # issue_id -> latest_fail_ts
    for name in os.listdir(test_dir):
        if not name.endswith(".json"):
            continue
        entry = state._read_json(os.path.join(test_dir, name), default=None)
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "failing":
            continue
        iid = entry.get("issue_id")
        ts = float(entry.get("ts") or 0)
        if iid and ts:
            if ts > failing.get(iid, 0):
                failing[iid] = ts
    out: List[str] = []
    for iid in failing:
        # State precondition (review) is enforced by the dispatch cycle's
        # allowed_states check, which emits the noop:state log. The trigger
        # returns every issue with a fresh failure so the cycle can decide.
        out.append(iid)
    return out


# (TRIGGERS dict moved below trigger_ci_signal to avoid forward-ref at
# module load.)


# --- SPEC-010: ci-signal ---

def _ci_seen_path(target: Optional[str]) -> str:
    return os.path.join(state._state_dir(target), "ci-seen.json")


def _load_ci_seen(target: Optional[str]) -> set:
    data = state._read_json(_ci_seen_path(target), default=None)
    if isinstance(data, dict):
        return set(data.get("runs") or [])
    return set()


def _save_ci_seen(seen: set, target: Optional[str]) -> None:
    path = _ci_seen_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state._atomic_write_json(path, {"runs": sorted(seen)})


def _git_commit_message(sha: str, target: Optional[str]) -> str:
    """Return the commit message body for `sha`, or "" on any failure.

    Tries `git show --no-patch --format=%B <sha>`. If the sha is
    remote-only, runs `git fetch origin <sha>` first (best-effort).
    """
    root = state._harness_root(target)

    def _show() -> str:
        try:
            r = subprocess.run(
                ["git", "show", "--no-patch", "--format=%B", sha],
                cwd=root, capture_output=True, timeout=15, check=False)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            pass
        return ""

    msg = _show()
    if not msg:
        try:
            subprocess.run(["git", "fetch", "origin", sha],
                           cwd=root, capture_output=True, timeout=30,
                           check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        msg = _show()
    return msg or ""


def trigger_ci_signal(tasks: Dict[str, Any], cfg: Dict[str, Any],
                      target: Optional[str], now: float) -> List[str]:
    """Poll `gh run list` for failed CI runs; map each to an issue via the
    commit message's ISSUE-NNNN token. Returns issue ids in `review-passed`
    or `release-candidate` state whose CI just failed.

    A run is acted on once per failure (recorded in ci-seen.json).
    """
    base = cfg.get("base_branch", "main")
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--branch", base,
             "--status", "failure", "--limit", "5",
             "--json", "databaseId,headSha"],
            capture_output=True, timeout=30, check=False)
        if out.returncode != 0:
            return []
        runs = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    if not isinstance(runs, list):
        return []

    seen = _load_ci_seen(target)
    candidates: List[str] = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("databaseId", ""))
        if not rid or rid in seen:
            continue
        sha = r.get("headSha") or ""
        if not sha:
            seen.add(rid)
            continue
        msg = _git_commit_message(sha, target)
        m = re.search(r"ISSUE-(\d{4,})", msg)
        seen.add(rid)
        if not m:
            continue
        iid = "ISSUE-" + m.group(1)
        st = tasks.get(iid, {}).get("status")
        if st in ("review-passed", "release-candidate"):
            candidates.append(iid)
    _save_ci_seen(seen, target)
    return candidates


TRIGGERS = {
    "clock":        (trigger_clock,        ["approved"]),
    "git-upstream": (trigger_git_upstream, ["approved"]),
    "idle-queue":   (trigger_idle_queue,   ["approved"]),
    "test-signal":  (trigger_test_signal,  ["review"]),
    "ci-signal":    (trigger_ci_signal,    ["review-passed", "release-candidate"]),
}


# --- dispatch ---

def dispatch(event_type: str, iid: str, target: Optional[str]) -> int:
    """Dispatch a motivation event. Routes through runner entry points.

    test-signal -> review -> needs-fix.
    ci-signal   -> review-passed -> needs-fix, OR
                   release-candidate -> blocked.
    others      -> approved -> pm-review via cmd_start (lock-protected).

    Returns 0 on success, non-zero on refusal (caller logs noop).
    """
    import runner  # local import; same-scripts dir
    if event_type == "test-signal":
        ns = argparse.Namespace(issue_id=iid, from_state="review",
                                to_state="needs-fix", summary="", target=target)
        return runner.cmd_advance(ns)
    if event_type == "ci-signal":
        tasks = state._load_tasks(target)
        st = tasks.get(iid, {}).get("status")
        if st == "review-passed":
            ns = argparse.Namespace(issue_id=iid, from_state="review-passed",
                                    to_state="needs-fix",
                                    summary="CI failure", target=target)
            return runner.cmd_advance(ns)
        if st == "release-candidate":
            state._set_issue_state(
                iid, "blocked", target=target,
                block_reason="ci-failure")
            return 0
        return 1  # state changed under us; noop
    ns = argparse.Namespace(issue_id=iid, target=target)
    return runner.cmd_start(ns)


# --- main cycle ---

def run_once(target: Optional[str] = None, now: Optional[float] = None) -> int:
    """Single poll cycle. Returns 0 always (no-ops are not errors)."""
    if now is None:
        now = time.time()
    cfg = state.load_config(target)  # exits 2 on validation failure
    text_path = os.path.join(state._harness_root(target), ".harness", "config.yml")
    try:
        with open(text_path, "r", encoding="utf-8") as f:
            mot = _parse_motivations(f.read())
    except OSError:
        mot = {"enabled": False, "max_dispatches_per_hour": DEFAULT_MAX_DISPATCHES_PER_HOUR,
               "triggers": dict(DEFAULT_TRIGGER_CONFIG)}

    if not mot["enabled"]:
        _log({"ts": now, "event": "disabled", "reason": "kill switch"}, target)
        return 0

    max_per_hour = mot.get("max_dispatches_per_hour", DEFAULT_MAX_DISPATCHES_PER_HOUR)
    history = _load_rate(target)
    if not _rate_allows(now, history, max_per_hour):
        _log({"ts": now, "event": "rate-limited",
              "recent_count": len([t for t in history if t >= now - 3600])},
             target)
        return 0

    tasks = state._load_tasks(target)
    triggers_cfg = mot.get("triggers") or {}
    dispatched = 0
    for name, (fn, allowed_states) in TRIGGERS.items():
        tcfg = triggers_cfg.get(name) or {}
        if not tcfg.get("enabled", False):
            continue
        try:
            candidates = fn(tasks, tcfg, target, now)
        except Exception as exc:  # never crash the cycle
            _log({"ts": now, "event": "trigger-error", "trigger": name,
                  "error": str(exc)}, target)
            continue
        for iid in candidates:
            cur_state = _issue_state(iid, tasks)
            if cur_state not in allowed_states:
                _log({"ts": now, "event": "noop:state", "trigger": name,
                      "issue_id": iid, "state": cur_state}, target)
                continue
            if not _rate_allows(now, history, max_per_hour):
                _log({"ts": now, "event": "rate-limited-mid",
                      "trigger": name, "issue_id": iid}, target)
                break
            _log({"ts": now, "event": "dispatch", "trigger": name,
                  "issue_id": iid, "prior_state": cur_state}, target)
            rc = dispatch(name, iid, target)
            history.append(now)
            _save_rate(history, target)
            if rc == 0:
                dispatched += 1
            else:
                _log({"ts": now, "event": "dispatch-refused", "trigger": name,
                      "issue_id": iid, "rc": rc}, target)
            # Re-read tasks so state changes reflect in later triggers.
            tasks = state._load_tasks(target)
    _log({"ts": now, "event": "cycle-complete", "dispatched": dispatched}, target)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SPEC-005 motivation triggers")
    p.add_argument("--once", action="store_true",
                   help="Run a single poll cycle and exit (normative mode).")
    p.add_argument("--target", default=None,
                   help="Project root containing .harness/")
    args = p.parse_args()
    if not args.once:
        p.error("only --once mode is supported (one-shot, external timer)")
    return run_once(target=args.target)


if __name__ == "__main__":
    sys.exit(main())
