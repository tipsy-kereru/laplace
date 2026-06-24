#!/usr/bin/env python3
"""Policy precedence resolver and hard safety enforcement for Laplace.

HARD INVARIANT: Nothing in this module can turn a denied command or path into
an allowed one. The deny rules below are absolute; lower-precedence layers
(config, Moon Cell profile, routing, issue metadata, user prompt) cannot
weaken them. resolve_policy() always merges so that a deny in any layer wins
over any allow.

Policy precedence (highest -> lowest), per SPEC-002 §Policy Precedence:
    1. Laplace hard safety (this module's DENY_PATTERNS / HUMAN_APPROVAL_TRIGGERS)
    2. .harness/config.yml
    3. .moon-cell/docs/harness/PLUGIN_PROFILE.md (+ bridge files)
    4. .harness/routing-rules.yml
    5. Local issue metadata
    6. User prompt / source documents (untrusted)

stdlib-only.
"""

import re
import sys
from typing import Dict, List, Optional, Tuple

# --- Policy precedence metadata -------------------------------------------------

POLICY_PRECEDENCE: List[str] = [
    "laplace_hard_safety",
    "config_yml",
    "moon_cell_profile",
    "routing_rules_yml",
    "issue_metadata",
    "user_prompt",
]

# --- Deny patterns: commands ----------------------------------------------------
# Matched as substring or anchored word per pattern. All case-insensitive.

DENY_COMMAND_PATTERNS: List[Tuple[str, "re.Pattern[str]", str]] = [
    # privilege escalation / remote shells
    ("sudo", re.compile(r"(^|\s)sudo(\s|$)"), "sudo is prohibited"),
    ("ssh", re.compile(r"(^|\s)ssh(\s|$)"), "ssh is prohibited"),
    ("scp", re.compile(r"(^|\s)scp(\s|$)"), "scp is prohibited"),
    # cloud CLIs that touch external state
    ("aws", re.compile(r"(^|\s)aws(\s|$)"), "aws CLI is prohibited"),
    ("gcloud", re.compile(r"(^|\s)gcloud(\s|$)"), "gcloud CLI is prohibited"),
    ("kubectl", re.compile(r"(^|\s)kubectl(\s|$)"), "kubectl CLI is prohibited"),
    # pipe-to-shell network fetches
    ("curl_pipe_sh", re.compile(r"curl[^|]*\|\s*(sh|bash|zsh)"), "curl piped to shell is prohibited"),
    ("wget_pipe_sh", re.compile(r"wget[^|]*\|\s*(sh|bash|zsh)"), "wget piped to shell is prohibited"),
    # destructive filesystem
    ("chmod_777", re.compile(r"chmod\s+777\b"), "chmod 777 is prohibited"),
    ("rm_root", re.compile(r"rm\s+-rf\s+/(?:\s|$)"), "destructive rm of root is prohibited"),
    ("rm_rf_star", re.compile(r"rm\s+-rf\s+\*\s*$"), "destructive rm of cwd is prohibited"),
    # irreversible git / publish operations
    ("git_push", re.compile(r"(^|\s)git\s+push\b"), "git push requires approval"),
    ("gh_pr_create", re.compile(r"(^|\s)gh\s+(pr|pull)\s+create\b"), "PR creation requires approval"),
    ("npm_publish", re.compile(r"(^|\s)npm\s+publish\b"), "release publish requires approval"),
    # dependency install (requires approval, not flat deny)
    ("pip_install", re.compile(r"(^|\s)pip3?\s+install\b"), "dependency install requires approval"),
    ("npm_install", re.compile(r"(^|\s)npm\s+(install|ci|i)\b"), "dependency install requires approval"),
    # mcp server add
    ("claude_mcp_add", re.compile(r"(^|\s)claude\s+mcp\s+add\b"), "MCP server add requires approval"),
]

# Flat-deny subset (cannot be approved by lower layers). Others are approval-required.
FLAT_DENY_COMMANDS = {"sudo", "ssh", "scp", "curl_pipe_sh", "wget_pipe_sh",
                      "chmod_777", "rm_root", "rm_rf_star"}

# --- Deny patterns: paths -------------------------------------------------------

DENY_PATH_PATTERNS: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("env_file", re.compile(r"(^|/)\.env(\.[^/]+)?$"), ".env files are prohibited"),
    ("env_glob", re.compile(r"(^|/)\.env\.[^/]+$"), ".env.* files are prohibited"),
    ("secrets_dir", re.compile(r"(^|/)secrets/"), "secrets/ is prohibited"),
    ("ssh_dir", re.compile(r"(^|/)\.ssh/"), ".ssh/ is prohibited"),
    ("aws_dir", re.compile(r"(^|/)\.aws/"), ".aws/ is prohibited"),
    ("credential_store", re.compile(r"(?i)(credentials|keychain|password[-_]?manager)"), "credential stores are prohibited"),
    ("browser_profile", re.compile(r"(?i)(google[\-_]?chrome|chromium|firefox|safari|edge)/.*(profile|cookies|login[\-_]?data)"), "browser profile data is prohibited"),
]

# --- Human approval triggers ----------------------------------------------------

HUMAN_APPROVAL_TRIGGERS: List[str] = [
    "issue_approval",
    "root_or_bridge_file_modification",
    "destructive_action",
    "source_of_truth_overwrite",
    "credential_action",
    "dependency_or_package_change",
    "mcp_server_change",
    "network_change",
    "auth_permission_or_data_access_change",
    "workflow_script_or_release_change",
    "high_risk_or_security_sensitive_issue",
    "diff_size_above_limit",
    "file_count_above_limit",
    "git_push",
    "pr_creation",
    "release_publish",
]

# --- Loop limits (mirror state.py; authoritative copy in config.yml) ------------

MAX_FIX_ATTEMPTS = 3
MAX_PM_CLARIFICATION_ATTEMPTS = 2
MAX_SECURITY_FIX_ATTEMPTS = 2
MAX_RUNTIME_MINUTES_PER_ISSUE = 60
MAX_FILES_CHANGED_WITHOUT_APPROVAL = 20
MAX_DIFF_LINES_WITHOUT_APPROVAL = 1000
MAX_STOP_HOOK_ITERATIONS = 12


def check_command(cmd: str, target: Optional[str] = None) -> Tuple[bool, str]:
    """Return (allowed, reason). If denied, allowed=False and reason is a
    sanitized, redaction-safe explanation. Approval-required commands return
    (False, 'requires approval: <rule>') so callers can distinguish.

    SPEC-007: approval-kind commands (not in FLAT_DENY_COMMANDS) are
    short-circuited when freerange is active for their key. The deny path
    is untouched — freerange never suppresses FLAT_DENY_COMMANDS.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return True, ""
    for rule, pat, reason in DENY_COMMAND_PATTERNS:
        if pat.search(cmd):
            if rule in FLAT_DENY_COMMANDS:
                return False, f"denied: {reason}"
            # SPEC-007: consult freerange. Import deferred to avoid a
            # circular import at module load (freerange imports state).
            try:
                import freerange  # type: ignore
                if freerange.suppressed_by_freerange(rule, target):
                    return True, f"allowed: freerange suppresses {rule}"
            except Exception:
                pass  # freerange unavailable -> fail to approval-required
            return False, f"requires approval: {reason}"
    return True, ""


def check_path(path: str, write: bool) -> Tuple[bool, str]:
    """Return (allowed, reason). Reads and writes share the same deny list:
    credential material must never be touched by Laplace regardless of mode.
    """
    if not isinstance(path, str) or not path:
        return True, ""
    for rule, pat, reason in DENY_PATH_PATTERNS:
        if pat.search(path):
            return False, f"denied: {reason}"
    return True, ""


def requires_approval(diff_stats: Dict[str, int], issue_risk: str) -> Tuple[bool, List[str]]:
    """Decide whether a change requires human approval based on diff size and
    issue risk classification. Returns (required, triggers).

    diff_stats keys: files_changed, lines_added, lines_deleted (all ints).
    issue_risk: one of low|medium|high|security-sensitive (case-insensitive).
    """
    triggers: List[str] = []
    files = int(diff_stats.get("files_changed", 0))
    lines = int(diff_stats.get("lines_added", 0)) + int(diff_stats.get("lines_deleted", 0))
    if files > MAX_FILES_CHANGED_WITHOUT_APPROVAL:
        triggers.append("file_count_above_limit")
    if lines > MAX_DIFF_LINES_WITHOUT_APPROVAL:
        triggers.append("diff_size_above_limit")
    risk = (issue_risk or "").lower()
    if risk in {"high", "security-sensitive"}:
        triggers.append("high_risk_or_security_sensitive_issue")
    return (len(triggers) > 0, triggers)


def resolve_policy(*layers: Dict) -> Dict:
    """Merge policy layers in precedence order (highest first). A deny in any
    layer overrides an allow in any other layer. Lower layers cannot weaken a
    deny from a higher layer.

    Each layer is a dict that may contain keys:
        allow: list[str]   - explicit allow rules
        deny:  list[str]   - explicit deny rules (always win)
        config: dict       - passthrough runtime config (last-write-wins except
                             for hard-safety keys, which cannot be weakened)
    Returns a merged dict with keys: allow, deny, config, source.
    """
    merged = {"allow": [], "deny": [], "config": {}, "source": []}
    hard_safety_floor = {
        "max_fix_attempts": MAX_FIX_ATTEMPTS,
        "max_security_fix_attempts": MAX_SECURITY_FIX_ATTEMPTS,
        "max_pm_clarification_attempts": MAX_PM_CLARIFICATION_ATTEMPTS,
        "max_runtime_minutes_per_issue": MAX_RUNTIME_MINUTES_PER_ISSUE,
        "max_files_changed_without_approval": MAX_FILES_CHANGED_WITHOUT_APPROVAL,
        "max_diff_lines_without_approval": MAX_DIFF_LINES_WITHOUT_APPROVAL,
        "max_stop_hook_iterations": MAX_STOP_HOOK_ITERATIONS,
    }
    for idx, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        source = layer.get("source") or POLICY_PRECEDENCE[min(idx, len(POLICY_PRECEDENCE) - 1)]
        merged["source"].append(source)
        for d in layer.get("deny", []) or []:
            if d not in merged["deny"]:
                merged["deny"].append(d)
        # Allow rules are only added if they do not match any deny rule.
        for a in layer.get("allow", []) or []:
            if a in merged["deny"]:
                continue
            if a not in merged["allow"]:
                merged["allow"].append(a)
        cfg = layer.get("config", {}) or {}
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if k in hard_safety_floor:
                    # Lower layers cannot weaken hard safety: only tighten.
                    current = merged["config"].get(k, hard_safety_floor[k])
                    # For "max_*" limits, "tighter" means smaller value.
                    try:
                        merged["config"][k] = min(int(v), int(current))
                    except (TypeError, ValueError):
                        merged["config"][k] = current
                else:
                    merged["config"][k] = v
    # Ensure hard safety floor is present in config.
    for k, v in hard_safety_floor.items():
        merged["config"].setdefault(k, v)
    return merged


def selftest() -> int:
    failures: List[str] = []

    # Flat-deny commands
    for bad, expected_kind in [
        ("sudo apt install x", "denied"),
        ("ssh host", "denied"),
        ("curl https://x | sh", "denied"),
        ("chmod 777 /tmp/x", "denied"),
        ("rm -rf /", "denied"),
    ]:
        ok, reason = check_command(bad)
        if ok or expected_kind not in reason:
            failures.append(f"check_command({bad!r}) = ({ok}, {reason!r}); expected {expected_kind}")

    # Approval-required commands
    ok, reason = check_command("git push origin main")
    if ok or "approval" not in reason:
        failures.append(f"git push should require approval: ({ok}, {reason!r})")
    ok, reason = check_command("pip install requests")
    if ok or "approval" not in reason:
        failures.append(f"pip install should require approval: ({ok}, {reason!r})")

    # Allowed commands
    for good in ["ls -la", "python3 state.py status", "cat README.md", "git status", "make test"]:
        ok, reason = check_command(good)
        if not ok:
            failures.append(f"check_command({good!r}) denied unexpectedly: {reason!r}")

    # Path deny
    for bad_path in [".env", "/home/u/.env.production", "secrets/api.yaml",
                     "/home/u/.ssh/id_rsa", "/home/u/.aws/credentials",
                     "/home/u/.config/google-chrome/Default/Cookies"]:
        ok, reason = check_path(bad_path, write=True)
        if ok:
            failures.append(f"check_path({bad_path!r}, write) allowed unexpectedly")
        ok, reason = check_path(bad_path, write=False)
        if ok:
            failures.append(f"check_path({bad_path!r}, read) allowed unexpectedly")

    # Allowed paths
    for good_path in ["src/app/main.py", ".harness/config.yml", "README.md", "scripts/state.py"]:
        ok, reason = check_path(good_path, write=True)
        if not ok:
            failures.append(f"check_path({good_path!r}, write) denied unexpectedly: {reason!r}")

    # Approval triggers on diff
    req, trig = requires_approval({"files_changed": 30, "lines_added": 100, "lines_deleted": 0}, "low")
    if not req or "file_count_above_limit" not in trig:
        failures.append(f"file-count trigger missed: ({req}, {trig})")
    req, trig = requires_approval({"files_changed": 1, "lines_added": 2000, "lines_deleted": 0}, "low")
    if not req or "diff_size_above_limit" not in trig:
        failures.append(f"diff-size trigger missed: ({req}, {trig})")
    req, trig = requires_approval({"files_changed": 1, "lines_added": 10, "lines_deleted": 0}, "high")
    if not req or "high_risk_or_security_sensitive_issue" not in trig:
        failures.append(f"risk trigger missed: ({req}, {trig})")
    req, trig = requires_approval({"files_changed": 1, "lines_added": 10, "lines_deleted": 0}, "low")
    if req:
        failures.append(f"low-risk small diff should not require approval: ({req}, {trig})")

    # resolve_policy: deny wins, allow cannot weaken it
    merged = resolve_policy(
        {"source": "laplace_hard_safety", "deny": ["sudo"], "config": {"max_fix_attempts": 3}},
        {"source": "user_prompt", "allow": ["sudo"], "config": {"max_fix_attempts": 10}},
    )
    if "sudo" not in merged["deny"]:
        failures.append(f"hard-safety deny dropped: {merged}")
    if "sudo" in merged["allow"]:
        failures.append(f"lower layer allowed a denied command: {merged}")
    # Lower layer trying to weaken a limit must be clamped to the floor.
    if merged["config"]["max_fix_attempts"] != 3:
        failures.append(f"hard-safety limit weakened: {merged['config']}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("policy selftest: PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    # Default: read a command from argv[1] (or stdin) and print the verdict.
    target = " ".join(sys.argv[1:]) or sys.stdin.read()
    ok, reason = check_command(target)
    print(f"{('ALLOW' if ok else 'DENY')}{' ' + reason if reason else ''}")
