#!/usr/bin/env python3
"""SPEC-007: freerange scope override.

Human-only, scope-bounded, time-limited suppression of Laplace's
approval layer. NOT a security boundary (SPEC-002 NG-007); a determined
model with Bash can defeat it. The deny layer (FLAT_DENY_COMMANDS) is
never consulted or affected by this module.

Three scopes map to real approval gates:
  flow    -> issue_approval (draft->approved)
  publish -> git_push, gh_pr_create, npm_publish
  supply  -> pip_install, npm_install, claude_mcp_add
  all     -> union

State file: .harness/state/freerange.json (untrusted; fail-closed on any
malformation). Audit log: .harness/logs/freerange.jsonl.

stdlib only.
"""
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Set

import state  # noqa: E402

TTL_DEFAULT_HOURS = 24
TTL_MAX_HOURS = 168  # 7 days

# Real approval keys. Map to DENY_COMMAND_PATTERNS rule names (publish/supply)
# plus the cmd_approve gate key (flow). NOT the trigger-style labels in
# HUMAN_APPROVAL_TRIGGERS (those are aspirational documentation).
REAL_APPROVAL_KEYS: Set[str] = {
    "issue_approval",
    "git_push", "gh_pr_create", "npm_publish",
    "pip_install", "npm_install", "claude_mcp_add",
}

SCOPE_TRIGGERS: Dict[str, Set[str]] = {
    "flow":    {"issue_approval"},
    "publish": {"git_push", "gh_pr_create", "npm_publish"},
    "supply":  {"pip_install", "npm_install", "claude_mcp_add"},
    "all":     REAL_APPROVAL_KEYS,
}


def _state_path(target: Optional[str]) -> str:
    return os.path.join(state._state_dir(target), "freerange.json")


def _log_path(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), ".harness", "logs",
                        "freerange.jsonl")


def _audit(event: str, target: Optional[str], **fields: Any) -> None:
    path = _log_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"ts": time.time(), "event": event}
    entry.update(fields)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_state(target: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read and validate freerange.json. Returns the state dict if active,
    None on absence/expiry/malformation (fail-closed). Logs tamper/expired
    once."""
    path = _state_path(target)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _audit("tamper", target, reason="unreadable")
        return None
    if not isinstance(data, dict):
        _audit("tamper", target, reason="not-dict")
        return None
    scope = data.get("scope")
    enabled = data.get("enabled")
    expires_at = data.get("expires_at")
    if enabled is not True or scope not in SCOPE_TRIGGERS:
        _audit("tamper", target, reason="bad-fields", raw_scope=scope,
               raw_enabled=enabled)
        return None
    if not isinstance(expires_at, (int, float)) or expires_at <= 0:
        _audit("tamper", target, reason="bad-expires", raw=expires_at)
        return None
    if time.time() > float(expires_at):
        if not data.get("expired_recorded"):
            _audit("expired", target, scope=scope,
                   expired_at=expires_at)
            data["expired_recorded"] = True
            try:
                state._atomic_write_json(path, data)
            except OSError:
                pass
        return None
    return data


def suppressed_by_freerange(approval_key: str,
                            target: Optional[str] = None) -> bool:
    """True iff `approval_key` is in the active freerange scope.

    Unknown keys (including FLAT_DENY_COMMANDS names) return False
    (fail-closed). Never imports or consults the deny layer. Returns False
    on any file error, expiry, or malformed content.
    """
    if approval_key not in REAL_APPROVAL_KEYS:
        return False
    data = read_state(target)
    if data is None:
        return False
    scope = data.get("scope")
    return approval_key in SCOPE_TRIGGERS.get(scope, set())


def active_scope(target: Optional[str] = None) -> Optional[str]:
    """Return the active scope name, or None."""
    data = read_state(target)
    return data.get("scope") if data else None


def _write_state(scope: str, ttl_hours: int, acknowledger: str,
                 target: Optional[str]) -> Dict[str, Any]:
    now = time.time()
    data = {
        "enabled": True,
        "scope": scope,
        "enabled_at": now,
        "ttl_hours": ttl_hours,
        "expires_at": now + ttl_hours * 3600.0,
        "acknowledger": acknowledger,
    }
    state._atomic_write_json(_state_path(target), data)
    return data


def _remove_state(target: Optional[str]) -> None:
    path = _state_path(target)
    if os.path.exists(path):
        os.remove(path)


def cmd_on(args: argparse.Namespace) -> int:
    if args.scope not in SCOPE_TRIGGERS:
        print(f"invalid scope: {args.scope!r} "
              f"(valid: {sorted(SCOPE_TRIGGERS)})", file=sys.stderr)
        return 2
    ttl = args.ttl
    if ttl <= 0 or ttl > TTL_MAX_HOURS:
        print(f"invalid --ttl: {ttl} (must be 1..{TTL_MAX_HOURS})",
              file=sys.stderr)
        return 2
    acknowledger = args.user or os.environ.get("USER", "unknown")
    data = _write_state(args.scope, ttl, acknowledger, args.target)
    _audit("on", args.target, scope=args.scope, ttl_hours=ttl,
           expires_at=data["expires_at"], acknowledger=acknowledger)
    remaining = (data["expires_at"] - data["enabled_at"]) / 3600.0
    expires_iso = time.strftime('%Y-%m-%dT%H:%M:%S',
                                time.localtime(data["expires_at"]))
    print(f"freerange ON: scope={args.scope} ttl={ttl}h "
          f"expires_at={expires_iso} ({remaining:.1f}h)")
    print("NOTE: freerange is a convenience aid, not a security boundary "
          "(SPEC-002 NG-007). A determined agent can defeat it.")
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    prev = read_state(args.target)
    _remove_state(args.target)
    _audit("off", args.target,
           prev_scope=prev.get("scope") if prev else None)
    print("freerange OFF" if prev else "freerange already off")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data = read_state(args.target)
    if not data:
        print("freerange: off")
        return 0
    remaining_h = (float(data["expires_at"]) - time.time()) / 3600.0
    expires_iso = time.strftime('%Y-%m-%dT%H:%M:%S',
                                time.localtime(data["expires_at"]))
    print(f"freerange: ON scope={data['scope']} "
          f"remaining={remaining_h:.1f}h expires_at={expires_iso}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SPEC-007 freerange scope override")
    sub = p.add_subparsers(dest="cmd", required=True)
    on = sub.add_parser("on", help="Enable a scope")
    on.add_argument("scope", choices=sorted(SCOPE_TRIGGERS))
    on.add_argument("--ttl", type=int, default=TTL_DEFAULT_HOURS,
                    help=f"TTL in hours (1..{TTL_MAX_HOURS}, default {TTL_DEFAULT_HOURS})")
    on.add_argument("--user", default=None)
    on.add_argument("--target", default=None)
    on.set_defaults(func=cmd_on)
    off = sub.add_parser("off", help="Disable all scopes")
    off.add_argument("--target", default=None)
    off.set_defaults(func=cmd_off)
    st = sub.add_parser("status", help="Show active scope")
    st.add_argument("--target", default=None)
    st.set_defaults(func=cmd_status)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
