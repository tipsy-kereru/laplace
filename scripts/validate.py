#!/usr/bin/env python3
"""Laplace plugin discovery validator (FU-1, VP-LP-001 prep).

Verifies the plugin is discoverable and well-formed for Claude Code:
  - plugin.json schema (name, version, description)
  - every skills/*/SKILL.md has parseable frontmatter with name + description
  - every agents/*.md has parseable frontmatter with name + description (+ tools)
  - hooks.json is valid JSON with recognized event names
  - script paths referenced in hooks.json resolve under the plugin root
  - no orphan script directories

stdlib only. Read-only. Exit 0 iff all hard checks pass; non-zero on failure.
Warnings (e.g., missing optional `tools` field on agents) do not fail validation
but are reported.

Usage:
  python3 scripts/validate.py [--root <plugin-root>] [--strict]
  python3 scripts/validate.py selftest
"""

import argparse
import json
import os
import re
import sys
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(HERE)  # plugin root = parent of scripts/

KNOWN_HOOK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Stop", "SubagentStart", "SubagentStop",
    "WorktreeCreate", "WorktreeRemove",
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Frontmatter parsing (minimal YAML subset: key: value lines)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> Optional[dict]:
    """Return {key: value} from leading --- block, or None if absent/malformed."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    body = m.group(1)
    out: dict = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            return None  # malformed line
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.passes: List[str] = []
        self.warns: List[str] = []
        self.fails: List[str] = []

    def ok(self, msg: str) -> None:
        self.passes.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    @property
    def ok_all(self) -> bool:
        return not self.fails


def _read(path: str) -> Tuple[bool, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return True, f.read()
    except OSError as exc:
        return False, str(exc)


def check_plugin_json(root: str, rep: Report) -> None:
    path = os.path.join(root, ".claude-plugin", "plugin.json")
    ok, raw = _read(path)
    if not ok:
        rep.fail(f"plugin.json unreadable: {raw}")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        rep.fail(f"plugin.json invalid JSON: {exc}")
        return
    if not isinstance(data, dict):
        rep.fail("plugin.json top-level is not an object")
        return
    for field in ("name", "version", "description"):
        v = data.get(field)
        if not v or not isinstance(v, str):
            rep.fail(f"plugin.json missing/empty field: {field}")
        else:
            rep.ok(f"plugin.json field {field}={v!r}")
    # Optional marketplace marker check
    if "category" in data:
        rep.ok(f"plugin.json category={data['category']!r}")


def check_skills(root: str, rep: Report) -> List[str]:
    skills_dir = os.path.join(root, "skills")
    if not os.path.isdir(skills_dir):
        rep.fail("skills/ directory missing")
        return []
    found: List[str] = []
    for name in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_path):
            rep.warn(f"skills/{name}/ has no SKILL.md (orphan dir)")
            continue
        ok, raw = _read(skill_path)
        if not ok:
            rep.fail(f"skills/{name}/SKILL.md unreadable: {raw}")
            continue
        fm = parse_frontmatter(raw)
        if fm is None:
            rep.fail(f"skills/{name}/SKILL.md missing frontmatter")
            continue
        missing = [f for f in ("name", "description") if not fm.get(f)]
        if missing:
            rep.fail(f"skills/{name}/SKILL.md missing frontmatter fields: {missing}")
            continue
        if fm.get("name") != name:
            rep.warn(
                f"skills/{name}/SKILL.md name={fm.get('name')!r} != dir name {name!r}"
            )
        rep.ok(f"skills/{name}/SKILL.md frontmatter OK (name={fm.get('name')!r})")
        found.append(name)
    return found


def check_agents(root: str, rep: Report) -> List[str]:
    agents_dir = os.path.join(root, "agents")
    if not os.path.isdir(agents_dir):
        rep.warn("agents/ directory missing (optional but expected)")
        return []
    found: List[str] = []
    for name in sorted(os.listdir(agents_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(agents_dir, name)
        ok, raw = _read(path)
        if not ok:
            rep.fail(f"agents/{name} unreadable: {raw}")
            continue
        fm = parse_frontmatter(raw)
        if fm is None:
            rep.fail(f"agents/{name} missing frontmatter")
            continue
        missing = [f for f in ("name", "description") if not fm.get(f)]
        if missing:
            rep.fail(f"agents/{name} missing frontmatter fields: {missing}")
            continue
        if not fm.get("tools"):
            rep.warn(f"agents/{name} has no 'tools' field (recommended)")
        if not fm.get("model"):
            rep.warn(f"agents/{name} has no 'model' field (recommended)")
        rep.ok(f"agents/{name} frontmatter OK (name={fm.get('name')!r})")
        found.append(name[:-3])
    return found


def check_hooks_json(root: str, rep: Report) -> None:
    path = os.path.join(root, "hooks", "hooks.json")
    ok, raw = _read(path)
    if not ok:
        rep.warn(f"hooks/hooks.json unreadable: {raw} (optional)")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        rep.fail(f"hooks/hooks.json invalid JSON: {exc}")
        return
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        rep.fail("hooks/hooks.json top-level 'hooks' is not an object")
        return
    unknown = [e for e in hooks if e not in KNOWN_HOOK_EVENTS]
    if unknown:
        rep.fail(f"hooks/hooks.json unknown event names: {unknown}")
    bound = [e for e, v in hooks.items()
             if isinstance(v, list) and len(v) > 0]
    rep.ok(f"hooks/hooks.json valid; {len(bound)} event(s) bound: {bound}")


def check_hook_script_paths(root: str, rep: Report) -> None:
    """Resolve ${CLAUDE_PLUGIN_ROOT}/... paths in hooks.json; warn if missing."""
    path = os.path.join(root, "hooks", "hooks.json")
    ok, raw = _read(path)
    if not ok:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    for event, entries in data.get("hooks", {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                # Extract first path token after ${CLAUDE_PLUGIN_ROOT}
                m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+)", cmd)
                if not m:
                    continue
                rel = m.group(1)
                # Strip any args after the path (split on whitespace already done by \S+,
                # but the path may itself have no spaces; verify file exists)
                candidate = os.path.join(root, rel)
                if os.path.exists(candidate):
                    rep.ok(f"hook script resolves: {rel}")
                    # Executable bit for .sh
                    if rel.endswith(".sh") and not os.access(candidate, os.X_OK):
                        rep.fail(f"hook script not executable: {rel}")
                else:
                    rep.fail(f"hook script missing: {rel}")


def check_scripts_dir(root: str, rep: Report) -> None:
    scripts_dir = os.path.join(root, "scripts")
    if not os.path.isdir(scripts_dir):
        rep.warn("scripts/ directory missing (optional)")
        return
    py_files = [f for f in os.listdir(scripts_dir) if f.endswith(".py")]
    if not py_files:
        rep.warn("scripts/ has no .py files")
        return
    rep.ok(f"scripts/ contains {len(py_files)} .py file(s)")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_validation(root: str) -> Report:
    rep = Report()
    if not os.path.isdir(root):
        rep.fail(f"plugin root not a directory: {root}")
        return rep
    if not os.path.isdir(os.path.join(root, ".claude-plugin")):
        rep.fail(".claude-plugin/ missing — not a plugin root")
        return rep
    check_plugin_json(root, rep)
    check_skills(root, rep)
    check_agents(root, rep)
    check_hooks_json(root, rep)
    check_hook_script_paths(root, rep)
    check_scripts_dir(root, rep)
    return rep


def print_report(rep: Report, strict: bool = False) -> int:
    for msg in rep.passes:
        print(f"  PASS  {msg}")
    for msg in rep.warns:
        print(f"  WARN  {msg}")
    for msg in rep.fails:
        print(f"  FAIL  {msg}")
    total = len(rep.passes) + len(rep.warns) + len(rep.fails)
    print(f"\n{len(rep.passes)} pass, {len(rep.warns)} warn, {len(rep.fails)} fail "
          f"({total} checks)")
    if rep.fails:
        return 1
    if strict and rep.warns:
        return 2
    return 0


def selftest() -> int:
    """Validate the plugin itself; exit 0 iff clean (warnings allowed)."""
    rep = run_validation(DEFAULT_ROOT)
    rc = print_report(rep, strict=False)
    if rc != 0:
        print("validate selftest: FAIL", file=sys.stderr)
        return 1
    # Also validate against a synthetic broken fixture to confirm checks fire.
    import tempfile
    tmp = tempfile.mkdtemp(prefix="laplace-validate-selftest-")
    try:
        # Broken: plugin.json with missing fields
        os.makedirs(os.path.join(tmp, ".claude-plugin"))
        with open(os.path.join(tmp, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": ""}, f)  # empty name, no version/description
        # Skill with no frontmatter
        os.makedirs(os.path.join(tmp, "skills", "broken"))
        with open(os.path.join(tmp, "skills", "broken", "SKILL.md"), "w") as f:
            f.write("# no frontmatter\n")
        rep2 = run_validation(tmp)
        if rep2.ok_all:
            print("validate selftest: FAIL (broken fixture not detected)",
                  file=sys.stderr)
            return 1
        if not any("plugin.json" in m and "name" in m for m in rep2.fails):
            print("validate selftest: FAIL (missing-name not flagged)",
                  file=sys.stderr)
            return 1
        if not any("frontmatter" in m for m in rep2.fails):
            print("validate selftest: FAIL (skill frontmatter not flagged)",
                  file=sys.stderr)
            return 1
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("validate selftest: PASS")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="validate.py",
                                     description="Laplace plugin discovery validator")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="Plugin root (default: parent of scripts/)")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as failures (exit 2)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="Internal sanity checks")
    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    rep = run_validation(args.root)
    return print_report(rep, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
