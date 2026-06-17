#!/usr/bin/env python3
"""Moon Cell profile consumer for Laplace.

Per SPEC-002 §Moon Cell Integration. Reads .moon-cell/docs/harness/PLUGIN_PROFILE.md
when present, validates required sections, hashes the source, and snapshots
metadata into .harness/state/profile-snapshot.json.

When Moon Cell is absent: writes a snapshot with present=false and a SPEC-mandated
recommendation. When present but invalid: records errors, marks valid=false.
Laplace NEVER silently ignores an invalid profile — it falls back to default.

CLI:
  profile.py snapshot [--target <repo-root>]
  profile.py show [--target <repo-root>]
  profile.py selftest

stdlib-only.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import state  # type: ignore

REQUIRED_SECTIONS = [
    "policy_precedence",
    "quality_gates",
    "approval_gates",
    "task_routing",
    "model_classes",
    "runtime_assumptions",
    "fallback",
]

# Deeper schema: each section body must mention at least one expected keyword
# (case-insensitive substring). Lenient by design — catches grossly malformed
# profiles without rejecting valid variations. A section failing this check
# records an error and marks the snapshot invalid (Laplace falls back).
SECTION_EXPECTED_KEYWORDS: Dict[str, List[str]] = {
    "policy_precedence": ["priority", "precedence", "hard safety"],
    "quality_gates": ["check", "gate", "transition"],
    "approval_gates": ["human", "approval"],
    "task_routing": ["route", "agent", "owner"],
    "model_classes": ["reasoning", "implementation", "cheap"],
    "runtime_assumptions": ["verified", "tbd", "unsupported", "assumption"],
    "fallback": ["default", "fallback", "absent"],
}

PROFILE_REL = os.path.join(".moon-cell", "docs", "harness", "PLUGIN_PROFILE.md")

ABSENT_RECOMMENDATION = (
    "Moon Cell profile not found.\n"
    "Laplace can run with default local policy.\n"
    "Recommended: use Moon Cell to generate a project-specific harness profile."
)


def _profile_path(target: Optional[str]) -> str:
    return os.path.join(state._harness_root(target), PROFILE_REL)


def _snapshot_path(target: Optional[str]) -> str:
    return os.path.join(state._state_dir(target), "profile-snapshot.json")


def _extract_headings(md_text: str) -> List[str]:
    """Return markdown ATX/SETEX headings as lower-case slugs."""
    out = []
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip().lower().replace(" ", "_")
            if heading:
                out.append(heading)
        elif s and "=" in s:
            # SETEX underline: a line of = or - under text.
            # Best-effort; cheap check.
            stripped = s.replace("=", "").replace("-", "").strip()
            if not stripped and out:
                # already captured by prior text line; skip
                pass
    return out


def _section_text(md_text: str, heading_slug: str) -> Optional[str]:
    """Return the body text under a `## heading` section (best-effort)."""
    lines = md_text.splitlines()
    capture = False
    out: List[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            slug = s.lstrip("#").strip().lower().replace(" ", "_")
            if capture:
                break
            capture = (slug == heading_slug)
            continue
        if capture:
            out.append(line)
    return "\n".join(out).strip() if capture else None


def consume_profile(target: Optional[str] = None) -> Dict[str, Any]:
    """Read + validate the Moon Cell profile. Returns a snapshot dict."""
    path = _profile_path(target)
    if not os.path.isfile(path):
        return {
            "source": None,
            "present": False,
            "hash": None,
            "consumed_at": time.time(),
            "sections_present": [],
            "sections_missing": list(REQUIRED_SECTIONS),
            "valid": False,
            "errors": [],
            "recommendation": ABSENT_RECOMMENDATION,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            md_text = f.read()
    except OSError as exc:
        return {
            "source": path,
            "present": True,
            "hash": None,
            "consumed_at": time.time(),
            "sections_present": [],
            "sections_missing": list(REQUIRED_SECTIONS),
            "valid": False,
            "errors": [f"read error: {exc}"],
            "recommendation": None,
        }

    sha = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    headings = set(_extract_headings(md_text))
    present: List[str] = []
    missing: List[str] = []
    errors: List[str] = []
    for req in REQUIRED_SECTIONS:
        if req in headings:
            present.append(req)
            # Sanity: section body must be non-empty.
            body = _section_text(md_text, req)
            if not body:
                errors.append(f"section '{req}' is empty")
                continue
            # Deeper schema: body must mention at least one expected keyword.
            expected = SECTION_EXPECTED_KEYWORDS.get(req, [])
            if expected:
                body_lower = body.lower()
                if not any(kw in body_lower for kw in expected):
                    errors.append(
                        f"section '{req}' missing expected keyword(s): {expected}"
                    )
        else:
            missing.append(req)
    valid = (len(missing) == 0) and (len(errors) == 0)
    return {
        "source": path,
        "present": True,
        "hash": sha,
        "consumed_at": time.time(),
        "sections_present": present,
        "sections_missing": missing,
        "valid": valid,
        "errors": errors,
        "recommendation": None if valid else (
            "Moon Cell profile present but incomplete/invalid. "
            "Laplace falls back to default local policy; do not silently ignore."
        ),
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = consume_profile(args.target)
    out_path = _snapshot_path(args.target)
    state._atomic_write_json(out_path, snapshot)
    if not snapshot["present"]:
        print(snapshot["recommendation"])
        return 0
    if not snapshot["valid"]:
        sys.stderr.write(
            f"profile invalid: missing={snapshot['sections_missing']} "
            f"errors={snapshot['errors']}\n"
        )
        print("Moon Cell profile present but invalid. Falling back to default policy.")
        return 0
    print(f"profile snapshot valid: hash={snapshot['hash'][:12]} "
          f"sections={len(snapshot['sections_present'])}/{len(REQUIRED_SECTIONS)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    out_path = _snapshot_path(args.target)
    data = state._read_json(out_path, default=None)
    if data is None:
        sys.stderr.write("no snapshot; run `profile.py snapshot` first\n")
        return 1
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Drift detection. Compare current profile hash vs stored snapshot hash.

    Exit codes:
      0 = in sync (or both absent)
      1 = no stored snapshot (run `profile.py snapshot` first)
      2 = drift detected (profile changed since last snapshot)
      3 = profile absent on both sides (informational)
    """
    snap_path = _snapshot_path(args.target)
    stored = state._read_json(snap_path, default=None)
    current = consume_profile(args.target)

    if not current["present"]:
        if stored is None or not stored.get("present"):
            print("profile absent on both sides; nothing to check")
            return 3
        print("DRIFT: profile was present (hash=%s) but is now absent"
              % (stored.get("hash") or "?")[:12])
        return 2

    if stored is None or not stored.get("present"):
        sys.stderr.write(
            "no stored snapshot; run `profile.py snapshot` first "
            "(current hash=%s)\n" % (current["hash"] or "?")[:12]
        )
        return 1

    cur_hash = current.get("hash")
    sto_hash = stored.get("hash")
    if cur_hash == sto_hash:
        print("in sync: hash=%s sections=%d/%d"
              % ((cur_hash or "?")[:12], len(current["sections_present"]),
                 len(REQUIRED_SECTIONS)))
        return 0

    print("DRIFT detected:")
    print("  stored hash: %s  (consumed_at=%s)"
          % ((sto_hash or "?")[:12], stored.get("consumed_at")))
    print("  current hash: %s" % (cur_hash or "?")[:12])
    print("  action: re-run `profile.py snapshot` to consume the new version")
    return 2


def cmd_resnapshot(args: argparse.Namespace) -> int:
    """Force re-snapshot (overwrite stored snapshot with current profile)."""
    snapshot = consume_profile(args.target)
    out_path = _snapshot_path(args.target)
    state._atomic_write_json(out_path, snapshot)
    if not snapshot["present"]:
        print(snapshot["recommendation"])
        return 0
    if not snapshot["valid"]:
        sys.stderr.write(
            "resnapshot: profile invalid (missing=%s errors=%s); "
            "snapshot recorded but Laplace will fall back\n"
            % (snapshot["sections_missing"], snapshot["errors"])
        )
        return 0
    print("resnapshot complete: hash=%s sections=%d/%d"
          % ((snapshot["hash"] or "?")[:12],
             len(snapshot["sections_present"]), len(REQUIRED_SECTIONS)))
    return 0


# --- selftest ----------------------------------------------------------------

def selftest() -> int:
    import shutil
    import tempfile

    failures = []
    tmp = tempfile.mkdtemp(prefix="laplace-profile-")
    saved_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        state.cmd_init(target=tmp)

        # Per-section keyword-valid bodies (each contains an expected keyword).
        VALID_BODIES = {
            "policy_precedence": "Priority precedence: hard safety wins.",
            "quality_gates": "Each gate is a check before a state transition.",
            "approval_gates": "Human approval required for destructive actions.",
            "task_routing": "Route each task to its owner agent.",
            "model_classes": "Classes: reasoning-heavy, implementation, cheap-fast.",
            "runtime_assumptions": "Each assumption is verified, TBD, or unsupported.",
            "fallback": "Fallback to default policy when profile is absent.",
        }

        def _md(sections):
            return "\n\n".join(f"# {s}\n\n{VALID_BODIES[s]}" for s in sections)

        # 1. No .moon-cell/ -> snapshot present=false, recommendation set.
        snap = consume_profile(target=tmp)
        if snap["present"] is not False:
            failures.append(f"absent: present={snap['present']} expected False")
        if not snap.get("recommendation"):
            failures.append("absent: recommendation missing")
        if snap["valid"] is not False:
            failures.append("absent: valid should be False")

        # 2. Valid profile with all sections -> valid=True.
        moon_dir = os.path.join(tmp, ".moon-cell", "docs", "harness")
        os.makedirs(moon_dir, exist_ok=True)
        sections_md = _md(REQUIRED_SECTIONS)
        profile_path = os.path.join(moon_dir, "PLUGIN_PROFILE.md")
        state._atomic_write_text(profile_path, sections_md)
        snap = consume_profile(target=tmp)
        if not snap["present"]:
            failures.append("valid: present should be True")
        if not snap["valid"]:
            failures.append(f"valid: expected valid=True, errors={snap['errors']}")
        if snap["sections_missing"]:
            failures.append(f"valid: unexpected missing={snap['sections_missing']}")
        if not snap["hash"] or len(snap["hash"]) != 64:
            failures.append("valid: hash missing or wrong length")

        # 3. Profile missing some sections -> invalid with errors.
        partial = _md(REQUIRED_SECTIONS[:3])
        state._atomic_write_text(profile_path, partial)
        snap = consume_profile(target=tmp)
        if snap["valid"]:
            failures.append("partial: expected valid=False")
        if len(snap["sections_missing"]) != len(REQUIRED_SECTIONS) - 3:
            failures.append(f"partial: wrong missing count: {snap['sections_missing']}")
        if not snap.get("recommendation"):
            failures.append("partial: fallback recommendation missing")

        # 4. Profile with empty section body -> error recorded.
        empty_section = "# policy_precedence\n\n# quality_gates\n\nreal content\n\n" + \
                        _md(REQUIRED_SECTIONS[2:])
        state._atomic_write_text(profile_path, empty_section)
        snap = consume_profile(target=tmp)
        if snap["valid"]:
            failures.append("empty-section: expected valid=False")
        if not any("policy_precedence" in e for e in snap["errors"]):
            failures.append(f"empty-section: expected error about policy_precedence, got {snap['errors']}")

        # 5. Section present but missing expected keyword -> error recorded.
        # Only policy_precedence lacks its keyword; other sections stay valid.
        bad_keyword = "# policy_precedence\n\nnothing relevant here at all\n\n" + \
                      _md(REQUIRED_SECTIONS[1:])
        state._atomic_write_text(profile_path, bad_keyword)
        snap = consume_profile(target=tmp)
        if snap["valid"]:
            failures.append("keyword-missing: expected valid=False")
        if not any("policy_precedence" in e and "keyword" in e for e in snap["errors"]):
            failures.append(
                f"keyword-missing: expected keyword error, got {snap['errors']}"
            )

        # 6. Drift detection: snapshot valid, then modify profile, check fires.
        state._atomic_write_text(profile_path, sections_md)  # restore valid
        snap1 = consume_profile(target=tmp)
        state._atomic_write_json(_snapshot_path(tmp), snap1)
        # Re-run check on unchanged profile: in sync.
        rc_sync = cmd_check(argparse.Namespace(target=tmp))
        if rc_sync != 0:
            failures.append(f"drift: in-sync check returned {rc_sync}, expected 0")
        # Modify profile -> hash changes.
        state._atomic_write_text(profile_path, sections_md + "\n\n# extra note\n")
        rc_drift = cmd_check(argparse.Namespace(target=tmp))
        if rc_drift != 2:
            failures.append(f"drift: changed-profile check returned {rc_drift}, expected 2")
        # Resnapshot brings it back in sync.
        rc_resnap = cmd_resnapshot(argparse.Namespace(target=tmp))
        if rc_resnap != 0:
            failures.append(f"drift: resnapshot returned {rc_resnap}")
        rc_sync2 = cmd_check(argparse.Namespace(target=tmp))
        if rc_sync2 != 0:
            failures.append(f"drift: post-resnapshot check returned {rc_sync2}, expected 0")

    finally:
        sys.stdout.close()
        sys.stdout = saved_stdout
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("profile selftest: PASS")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="profile.py",
                                     description="Moon Cell profile consumer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="Read + snapshot Moon Cell profile")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("show", help="Print the current snapshot")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check", help="Drift check: current vs stored snapshot")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("resnapshot", help="Force re-snapshot (overwrite stored)")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_resnapshot)

    p = sub.add_parser("selftest", help="Internal sanity checks")
    p.set_defaults(func=lambda a: selftest())

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
