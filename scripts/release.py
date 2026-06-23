#!/usr/bin/env python3
"""Laplace release: 8-check gate + atomic bump/commit/tag/push.

Responsibilities (ISSUE-0003, PRD docs/prd-release-command.md):
  - Run an 8-check pre-release gate (branch, format, tests, sync, semver,
    tree-clean, tag-absent, remote-not-ahead, no-pending-approved).
  - On all-pass, atomically bump VERSION + plugin.json + marketplace.json,
    commit, tag, push main, push tag.
  - Every git op is routed through `policy.check_command`. The push is the
    one invocation-authorized side effect (Option A): when policy denies
    `git push`, release.py proceeds anyway because the human authorized by
    invoking `/laplace:release`. The authorization basis is recorded.
  - Append a release audit entry to `.harness/state/releases.jsonl` on
    every attempt (pass or halt).

stdlib-only. Mirrors verify.py structure (HERE / sys.path / import state).
"""

import argparse
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

import state  # noqa: E402
import policy  # noqa: E402

# --- Constants -----------------------------------------------------------------

# Three files whose `version` fields must stay in sync. Each maps to a reader
# + writer pair (the writer rewrites the file with the new version embedded).
_VERSION_FILES: Dict[str, str] = {
    "VERSION": "VERSION",
    "plugin": os.path.join(".claude-plugin", "plugin.json"),
    "marketplace": os.path.join(".claude-plugin", "marketplace.json"),
}

# Strict X.Y.Z format (PRD check 1). No leading `v`, no pre-release suffix.
_FORMAT_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Authorization basis recorded in the release log for the push step. The
# invocation of `/laplace:release <X.Y.Z>` IS the human approval (Option A,
# mirrors /laplace:create-pr).
_AUTHORIZATION_BASIS = "release-invocation"


class GitPolicyError(Exception):
    """Raised when policy denies a git op that this command cannot self-authorize."""


# --- Version helpers -----------------------------------------------------------

def _read_version_file(target: str, key: str) -> str:
    """Read the current version string for one of the three sync files."""
    rel = _VERSION_FILES[key]
    path = os.path.join(target, rel)
    if key == "VERSION":
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if key == "plugin":
        return str(data.get("version", "")).strip()
    # marketplace.json -> plugins[0].version
    plugins = data.get("plugins") or []
    if not plugins:
        return ""
    return str(plugins[0].get("version", "")).strip()


def _read_three_versions(target: str) -> Dict[str, str]:
    return {key: _read_version_file(target, key) for key in _VERSION_FILES}


def _current_version(target: str) -> str:
    """Return the canonical current version (sourced from VERSION)."""
    return _read_version_file(target, "VERSION")


def _write_version_file(target: str, key: str, new_version: str) -> None:
    rel = _VERSION_FILES[key]
    path = os.path.join(target, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if key == "VERSION":
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_version + "\n")
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if key == "plugin":
        data["version"] = new_version
    else:  # marketplace
        plugins = data.get("plugins") or []
        if not plugins:
            data["plugins"] = [{"version": new_version}]
        else:
            plugins[0]["version"] = new_version
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _bump_three(target: str, new_version: str) -> None:
    for key in _VERSION_FILES:
        _write_version_file(target, key, new_version)


def _version_tuple(v: str) -> Tuple[int, int, int]:
    parts = v.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


# --- Git wrapper (policy-routed; Option A for push) ---------------------------

def _cmd_to_str(cmd: List[str]) -> str:
    return " ".join(cmd)


def _run_git(cmd: List[str], *, target: Optional[str] = None,
             allow_push: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, routing through policy.check_command first.

    Option A: if the denied command is `git push` and the caller passed
    `allow_push=True`, the invocation of `/laplace:release` IS the
    authorization — proceed anyway. All other denials raise GitPolicyError.
    """
    cmd_str = _cmd_to_str(cmd)
    # policy.check_command matches deny regexes against the full command
    # including the leading "git" token (e.g. `(^|\s)git\s+push\b`). The
    # subprocess call prepends "git", so we must do the same here or the
    # push deny rule never fires and Option A becomes dead code.
    policy_str = "git " + cmd_str
    allowed, reason = policy.check_command(policy_str)
    if not allowed:
        is_push = "git push" in policy_str
        if allow_push and is_push:
            # Invocation-authorized push (Option A). Proceed.
            pass
        else:
            raise GitPolicyError(f"{policy_str}: {reason}")
    cwd = target or os.getcwd()
    result = subprocess.run(
        ["git"] + cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git"] + cmd, result.stdout, result.stderr
        )
    return result


# --- Release log (audit trail) -------------------------------------------------

def _releases_path(target: Optional[str] = None) -> str:
    return os.path.join(state._state_dir(target), "releases.jsonl")


def _append_release_log(target: Optional[str], entry: Dict[str, Any]) -> None:
    """Append one JSON-line release record to releases.jsonl."""
    path = _releases_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(entry)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --- The 8-check gate ----------------------------------------------------------

# Each check returns (passed: bool, reason: str). On failure, reason is the
# human-readable resolution message. Checks are ordered so the cheapest /
# most-local failures surface first; bumping happens AFTER checks 0,1,2,4,5,6,7,8
# pass, and check 3 (post-write sync) runs inside the atomic sequence.

def _check_branch(target: str) -> Tuple[bool, str]:
    res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], target=target)
    branch = res.stdout.strip()
    if branch != "main":
        return False, (f"not on main (on {branch!r}); "
                       f"run /laplace:release from main only")
    return True, ""


def _check_format(version: str) -> Tuple[bool, str]:
    if not _FORMAT_RE.match(version):
        return False, f"version {version!r} has bad format; expected X.Y.Z (e.g. 0.3.1)"
    return True, ""


def _check_tests(target: str) -> Tuple[bool, str]:
    """Run pytest. Local test run — not routed through policy (no network)."""
    res = subprocess.run(
        ["python3", "-m", "pytest", "-q"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        # Surface a short reason; full output already on stderr in CI.
        tail = (res.stderr or res.stdout or "").strip().splitlines()[-5:]
        snippet = "\n".join(tail)
        return False, f"tests failed (pytest exit {res.returncode}):\n{snippet}"
    return True, ""


def _check_sync_after_bump(target: str, version: str) -> Tuple[bool, str]:
    """Check 3: post-write integrity — all three files now == version."""
    three = _read_three_versions(target)
    bad = {k: v for k, v in three.items() if v != version}
    if bad:
        return False, (f"version sync failed after bump; current values: "
                       f"{three}; expected all == {version!r}")
    return True, ""


def _check_semver_direction(current: str, version: str, force: bool) -> Tuple[bool, str]:
    if _version_tuple(version) > _version_tuple(current):
        return True, ""
    if force:
        return True, ""  # --force relaxes downgrade
    return False, (f"downgrade {current} -> {version}; "
                   f"pass --force to confirm")


def _check_tree_clean(target: str) -> Tuple[bool, str]:
    res = _run_git(["status", "--porcelain"], target=target)
    out = res.stdout.strip()
    if out:
        return False, f"working tree dirty; commit or stash first:\n{out}"
    return True, ""


def _check_tag_absent(target: str, version: str) -> Tuple[bool, str]:
    res = _run_git(["rev-parse", "--verify", f"v{version}"],
                   target=target, check=False)
    if res.returncode == 0:
        return False, (f"tag v{version} exists; bump to next version or "
                       f"delete the tag")
    return True, ""


def _check_remote_not_ahead(target: str) -> Tuple[bool, str]:
    """Fetch origin main, then ensure local main is not behind origin/main.

    Tolerant of no-remote / offline: if the fetch or the rev-list fails
    (no `origin` configured, no `origin/main` ref, network unreachable),
    the check passes — there is no remote to be ahead of. Only halts when
    we can positively confirm origin/main is ahead of local main.
    """
    # git fetch is not in DENY_COMMAND_PATTERNS — runs clean through policy.
    fetch = _run_git(["fetch", "origin", "main"], target=target, check=False)
    if fetch.returncode != 0:
        # No remote / offline — cannot be ahead. Pass.
        return True, ""
    res = _run_git(["rev-list", "--count", "main..origin/main"],
                   target=target, check=False)
    if res.returncode != 0:
        # No origin/main ref — treat as not-ahead (offline / no remote).
        return True, ""
    n = int(res.stdout.strip() or "0")
    if n > 0:
        return False, (f"origin/main has {n} new commits; "
                       f"pull/rebase first")
    return True, ""


def _check_no_pending_approved(target: str, force: bool) -> Tuple[bool, str]:
    queue = state._load_queue(target)
    approved = queue.get("approved", []) or []
    if approved:
        if force:
            return True, ""
        return False, (f"{len(approved)} issues approved but not run "
                       f"({', '.join(approved[:5])}); release them first or "
                       f"/laplace:discard, or pass --force")
    return True, ""


# --- Atomic sequence (runs only after ALL checks pass) ------------------------

def _atomic_sequence(target: str, version: str, prev: str
                     ) -> Tuple[bool, Dict[str, Any]]:
    """Bump 3 files, commit, tag, push main, push tag.

    Returns (ok, info). On main-push success + tag-push failure, returns
    (False, {partial: True, ...}) — main is already public, do NOT roll back.
    """
    info: Dict[str, Any] = {}

    # 1. Bump 3 files.
    _bump_three(target, version)

    # 2. Post-bump sync self-check (check 3).
    ok, reason = _check_sync_after_bump(target, version)
    if not ok:
        # Roll back the bumped files so a sync-selfcheck failure leaves the
        # working tree clean (AC-REL-011 halt-safety).
        _run_git(["checkout", "--"] + [_VERSION_FILES[k] for k in _VERSION_FILES],
                 target=target, check=False)
        info["failed_step"] = "sync_after_bump"
        info["reason"] = reason
        return False, info
    info["sync_ok"] = True

    # 3. git add the three files.
    add_paths = [_VERSION_FILES[k] for k in _VERSION_FILES]
    add_cmd = ["add"] + add_paths
    _run_git(add_cmd, target=target)

    # 4. Commit.
    commit_msg = f"chore(release): bump {prev} -> {version}"
    _run_git(["commit", "-m", commit_msg], target=target)
    info["commit"] = _run_git(["rev-parse", "HEAD"], target=target).stdout.strip()

    # 5. Tag.
    tag = f"v{version}"
    _run_git(["tag", "-a", tag, "-m", f"{tag}: release"], target=target)
    info["tag"] = tag

    # 6. Push main (invocation-authorized — Option A).
    _run_git(["push", "origin", "main"], target=target, allow_push=True)
    info["main_pushed"] = True

    # 7. Push tag. On failure: halt, do NOT roll back main.
    try:
        _run_git(["push", "origin", tag], target=target, allow_push=True)
    except (subprocess.CalledProcessError, GitPolicyError) as exc:
        info["partial"] = True
        info["main_pushed"] = True
        info["tag"] = tag
        info["reason"] = (f"main pushed but tag push failed: {exc}; "
                          f"recover with: git push origin {tag}")
        return False, info

    info["tag_pushed"] = True
    return True, info


# --- cmd_release ---------------------------------------------------------------

def cmd_release(args: Any) -> int:
    """Run the 8-check gate + atomic sequence. Exit 0 released / 1 halted."""
    version: str = args.version
    target = getattr(args, "target", None) or os.getcwd()
    force: bool = bool(getattr(args, "force", False))

    if not os.path.isdir(os.path.join(target, ".harness")):
        print(f"Laplace is not initialized at {target}. Run /laplace:init first.",
              file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(target, ".git")):
        print(f"Not a git repo: {target}. /laplace:release requires git.",
              file=sys.stderr)
        return 2

    prev = _current_version(target)

    # Checks 0,1,2,4,5,6,7,8 run BEFORE any write. Check 3 (post-bump sync)
    # runs inside the atomic sequence after the bump.
    checks: List[Tuple[str, Tuple[bool, str]]] = []
    checks.append(("branch", _check_branch(target)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("format", _check_format(version)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("tests", _check_tests(target)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("semver", _check_semver_direction(prev, version, force)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("tree_clean", _check_tree_clean(target)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("tag_absent", _check_tag_absent(target, version)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("remote", _check_remote_not_ahead(target)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)
    checks.append(("approved_queue", _check_no_pending_approved(target, force)))
    if not checks[-1][1][0]:
        return _halt(target, version, prev, checks, args)

    # All pre-bump checks pass. Run the atomic sequence.
    ok, info = _atomic_sequence(target, version, prev)
    if not ok:
        # Either post-bump sync failed (no commit/tag/push) or tag-push
        # failed after main-push (partial — main is public, do NOT roll back).
        reason = info.get("reason", "atomic sequence failed")
        entry = {
            "ts": time.time(),
            "version": version,
            "prev_version": prev,
            "checks_passed": True,
            "sequence_ok": False,
            "failed_step": info.get("failed_step", "sequence"),
            "partial": bool(info.get("partial")),
            "main_pushed": bool(info.get("main_pushed")),
            "tag": info.get("tag"),
            "reason": reason,
            "authorization_basis": _AUTHORIZATION_BASIS,
        }
        _append_release_log(target, entry)
        if info.get("partial"):
            print(f"PARTIAL RELEASE: main pushed, tag push failed.\n  {reason}",
                  file=sys.stderr)
        else:
            print(f"HALT: atomic sequence failed at '{info.get('failed_step')}':\n  {reason}",
                  file=sys.stderr)
        return 1

    # Success.
    entry = {
        "ts": time.time(),
        "version": version,
        "prev_version": prev,
        "checks_passed": True,
        "sequence_ok": True,
        "pushed_at": time.time(),
        "commit": info.get("commit"),
        "tag": info.get("tag"),
        "main_pushed": True,
        "tag_pushed": True,
        "authorization_basis": _AUTHORIZATION_BASIS,
    }
    _append_release_log(target, entry)
    print(f"Released {prev} -> {version}: commit {entry['commit'][:8]}, "
          f"tag {entry['tag']}, pushed main + tag.")
    return 0


def _halt(target: str, version: str, prev: str,
          checks: List[Tuple[str, Tuple[bool, str]]], args: Any) -> int:
    """Record the halt in the release log, print resolution, exit 1."""
    name, (ok, reason) = checks[-1]
    entry = {
        "ts": time.time(),
        "version": version,
        "prev_version": prev,
        "checks_passed": False,
        "failed_check": name,
        "reason": reason,
    }
    _append_release_log(target, entry)
    print(f"HALT: check '{name}' failed:\n  {reason}", file=sys.stderr)
    return 1


# --- selftest ------------------------------------------------------------------

def _setup_selftest_repo(repo: str) -> None:
    """Create a git repo + .harness + the three version files at 0.3.0."""
    import shutil
    if os.path.exists(repo):
        shutil.rmtree(repo, ignore_errors=True)
    os.makedirs(repo)

    # git init + main branch + initial commit.
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "self@laplace.test"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Self Test"], cwd=repo, check=True)

    # version files
    with open(os.path.join(repo, "VERSION"), "w") as f:
        f.write("0.3.0\n")
    os.makedirs(os.path.join(repo, ".claude-plugin"), exist_ok=True)
    with open(os.path.join(repo, ".claude-plugin", "plugin.json"), "w") as f:
        json.dump({"name": "laplace", "version": "0.3.0"}, f)
    with open(os.path.join(repo, ".claude-plugin", "marketplace.json"), "w") as f:
        json.dump({"plugins": [{"name": "laplace", "version": "0.3.0"}]}, f)

    # Initialize .harness.
    if state.cmd_init(target=repo) != 0:
        raise RuntimeError("selftest: cmd_init failed")

    # Initial commit on main.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _running_module():
    """Return the module release.py is currently executing as.

    When run as `python3 scripts/release.py`, this is `__main__`. When
    imported, it is `release`. Monkeypatches must target whichever module
    owns the live `cmd_release` so the runtime call sites pick them up.
    """
    return sys.modules[__name__]


def _stub_push(monkey: Dict[str, Any]) -> None:
    """Install a push-stubbing monkeypatch on the module's _run_git.

    Records push calls into `monkey['pushes']` (list of cmd lists) and returns
    a synthetic CompletedProcess for push commands without actually pushing.
    """
    rel = _running_module()
    real = rel._run_git
    monkey["pushes"] = []
    monkey["real"] = real

    def fake(cmd, *, target=None, allow_push=False, check=True):
        cmd_str = rel._cmd_to_str(cmd)
        # cmd is the arg list WITHOUT the leading "git" (subprocess.run gets
        # ["git"] + cmd), so match on "push ..." rather than "git push ...".
        if cmd and cmd[0] == "push":
            monkey["pushes"].append(list(cmd))
            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return _CP()
        return real(cmd, target=target, allow_push=allow_push, check=check)

    rel._run_git = fake


def _unstub_push(monkey: Dict[str, Any]) -> None:
    rel = _running_module()
    rel._run_git = monkey["real"]


def _stub_tests_pass() -> Any:
    """Make _check_tests always pass. Returns the original for restore."""
    rel = _running_module()
    real = rel._check_tests

    def fake(target):
        return True, ""
    rel._check_tests = fake
    return real


def selftest() -> int:
    """Exercise the 8 halt cases + happy path + partial-push + audit log.

    PUSH IS STUBBED: every test monkeypatches _run_git so no actual push
    leaves the selftest repo. Network ops never run.
    """
    import shutil
    import tempfile

    failures: List[str] = []
    rel = _running_module()

    # --- Happy path (Case A) ---------------------------------------------------
    repo = os.path.join(tempfile.mkdtemp(prefix="laplace-release-selftest-"),
                        "repo")
    _setup_selftest_repo(repo)
    monkey: Dict[str, Any] = {}
    _stub_push(monkey)
    real_tests = _stub_tests_pass()
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        rc = cmd_release(argparse.Namespace(version="0.3.1", target=repo, force=False))
        if rc != 0:
            failures.append(f"Case A happy path should exit 0, got {rc}")
        # versions bumped
        if _current_version(repo) != "0.3.1":
            failures.append("Case A: VERSION not bumped to 0.3.1")
        three = _read_three_versions(repo)
        if not all(v == "0.3.1" for v in three.values()):
            failures.append(f"Case A: three-file sync broken: {three}")
        # main + tag pushed
        pushes = monkey["pushes"]
        push_strs = [_cmd_to_str(c) for c in pushes]
        if not any("main" in s for s in push_strs):
            failures.append(f"Case A: main not pushed: {pushes}")
        if not any("v0.3.1" in s for s in push_strs):
            failures.append(f"Case A: tag not pushed: {pushes}")
        # tag exists in the repo
        tag_check = subprocess.run(["git", "rev-parse", "--verify", "v0.3.1"],
                                   cwd=repo, capture_output=True, text=True)
        if tag_check.returncode != 0:
            failures.append("Case A: tag v0.3.1 not created")
        # audit log has a success entry
        log_path = _releases_path(repo)
        if not os.path.exists(log_path):
            failures.append("Case A: releases.jsonl not created")
        else:
            with open(log_path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            if not any(e.get("checks_passed") and e.get("sequence_ok")
                       for e in lines):
                failures.append(f"Case A: no success entry in log: {lines}")
            if not any(e.get("authorization_basis") == "release-invocation"
                       for e in lines):
                failures.append("Case A: authorization_basis missing from log")
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = saved_out
        sys.stderr = saved_err
        _unstub_push(monkey)
        rel._check_tests = real_tests
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)

    # Helper to run a fresh repo for a halt-case test.
    def run_case(case_label: str, setup_fn, stub_tests: bool = True) -> int:
        r = os.path.join(tempfile.mkdtemp(prefix=f"laplace-rel-{case_label}-"),
                         "repo")
        _setup_selftest_repo(r)
        setup_fn(r)
        m: Dict[str, Any] = {}
        _stub_push(m)
        real_tests = _stub_tests_pass() if stub_tests else None
        so, se = sys.stdout, sys.stderr
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
        try:
            rc = cmd_release(argparse.Namespace(version="0.3.1", target=r, force=False))
        finally:
            try:
                sys.stdout.close()
            except Exception:
                pass
            try:
                sys.stderr.close()
            except Exception:
                pass
            sys.stdout = so
            sys.stderr = se
            _unstub_push(m)
            if real_tests is not None:
                rel._check_tests = real_tests
            shutil.rmtree(os.path.dirname(r), ignore_errors=True)
        return rc

    # --- Case B: bad format ----------------------------------------------------
    def setup_b(r): pass
    # Need to test bad format directly via the check + via cmd_release.
    ok, reason = _check_format("0.3")
    if ok:
        failures.append("Case B: _check_format('0.3') should fail")
    ok, reason = _check_format("v0.3.1")
    if ok:
        failures.append("Case B: _check_format('v0.3.1') should fail")
    ok, reason = _check_format("0.3.1.2")
    if ok:
        failures.append("Case B: _check_format('0.3.1.2') should fail")
    ok, reason = _check_format("0.3.1")
    if not ok:
        failures.append("Case B: _check_format('0.3.1') should pass")

    # --- Case C: failing test (pytest missing → subprocess error) --------------
    # Use a repo with no pytest available in subprocess by overriding _check_tests
    # via direct invocation: simulate non-zero return.
    real_check_tests = rel._check_tests
    def fake_check_tests_fail(target):
        return False, "tests failed (pytest exit 1):\nfake_failing_test"
    rel._check_tests = fake_check_tests_fail
    try:
        rc = run_case("C", lambda r: None, stub_tests=False)
    finally:
        rel._check_tests = real_check_tests
    if rc != 1:
        failures.append(f"Case C failing-tests should exit 1, got {rc}")

    # --- Case D: version sync forced-desync (post-bump) ------------------------
    # Monkeypatch _bump_three to write a wrong version into one file.
    real_bump = rel._bump_three
    def fake_bump_desync(target, new_version):
        # Bump VERSION + plugin only; leave marketplace stale.
        rel._write_version_file(target, "VERSION", new_version)
        rel._write_version_file(target, "plugin", new_version)
        # marketplace stays at 0.3.0
    rel._bump_three = fake_bump_desync
    try:
        rc = run_case("D", lambda r: None)
    finally:
        rel._bump_three = real_bump
    if rc != 1:
        failures.append(f"Case D sync-desync should exit 1, got {rc}")

    # --- Case E: downgrade without --force -------------------------------------
    def setup_e(r):
        # Bump current to 0.4.0 manually, then try to release 0.3.1.
        with open(os.path.join(r, "VERSION"), "w") as f:
            f.write("0.4.0\n")
        with open(os.path.join(r, ".claude-plugin", "plugin.json")) as f:
            data = json.load(f)
        data["version"] = "0.4.0"
        with open(os.path.join(r, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump(data, f)
        with open(os.path.join(r, ".claude-plugin", "marketplace.json")) as f:
            data = json.load(f)
        data["plugins"][0]["version"] = "0.4.0"
        with open(os.path.join(r, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump(data, f)
        subprocess.run(["git", "add", "-A"], cwd=r, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "bump to 0.4.0"], cwd=r, check=True)

    ok, reason = _check_semver_direction("0.4.0", "0.3.1", False)
    if ok:
        failures.append("Case E: downgrade without --force should fail check")
    ok, reason = _check_semver_direction("0.4.0", "0.3.1", True)
    if not ok:
        failures.append("Case E: downgrade with --force should pass check")

    # --- Case F: dirty tree ----------------------------------------------------
    def setup_f(r):
        # Dirty an unrelated file (not VERSION — _current_version must still parse).
        with open(os.path.join(r, "README.md"), "w") as f:
            f.write("# uncommitted change\n")
    rc = run_case("F", setup_f)
    if rc != 1:
        failures.append(f"Case F dirty-tree should exit 1, got {rc}")

    # --- Case G: tag already exists --------------------------------------------
    def setup_g(r):
        subprocess.run(["git", "tag", "-a", "v0.3.1", "-m", "x"], cwd=r, check=True)
    rc = run_case("G", setup_g)
    if rc != 1:
        failures.append(f"Case G tag-exists should exit 1, got {rc}")

    # --- Case H: remote ahead --------------------------------------------------
    real_remote = rel._check_remote_not_ahead
    def fake_remote_ahead(target):
        return False, "origin/main has 3 new commits; pull/rebase first"
    rel._check_remote_not_ahead = fake_remote_ahead
    try:
        rc = run_case("H", lambda r: None)
    finally:
        rel._check_remote_not_ahead = real_remote
    if rc != 1:
        failures.append(f"Case H remote-ahead should exit 1, got {rc}")

    # --- Case I: pending approved issues ---------------------------------------
    def setup_i(r):
        qpath = os.path.join(r, ".harness", "state", "queue.json")
        with open(qpath) as f:
            q = json.load(f)
        q["approved"] = ["ISSUE-9999"]
        with open(qpath, "w") as f:
            json.dump(q, f)
    rc = run_case("I", setup_i)
    if rc != 1:
        failures.append(f"Case I pending-approved should exit 1, got {rc}")

    # --- Case J: --force relaxes downgrade (passes semver check) ---------------
    # Direct check-level test (full-sequence happy-path with --force is covered
    # by the fact that --force only relaxes 4 + 8, never others).
    # Verify --force relaxes pending-approved check.
    r2 = os.path.join(tempfile.mkdtemp(prefix="laplace-rel-J-"), "repo")
    _setup_selftest_repo(r2)
    qpath = os.path.join(r2, ".harness", "state", "queue.json")
    with open(qpath) as f:
        q = json.load(f)
    q["approved"] = ["ISSUE-8888"]
    with open(qpath, "w") as f:
        json.dump(q, f)
    ok, reason = _check_no_pending_approved(r2, force=True)
    if not ok:
        failures.append(f"Case J: --force should relax pending-approved: {reason}")
    shutil.rmtree(os.path.dirname(r2), ignore_errors=True)

    # --- Case K: partial-push (main ok, tag fails) -----------------------------
    # Monkeypatch _run_git so the tag push raises.
    r3 = os.path.join(tempfile.mkdtemp(prefix="laplace-rel-K-"), "repo")
    _setup_selftest_repo(r3)
    m2: Dict[str, Any] = {}
    real_run_git = rel._run_git
    m2["pushes"] = []

    def fake_partial(cmd, *, target=None, allow_push=False, check=True):
        cmd_str = rel._cmd_to_str(cmd)
        if cmd and cmd[0] == "push":
            m2["pushes"].append(list(cmd))
            if "v0.3.1" in cmd_str:
                raise subprocess.CalledProcessError(1, ["git"] + cmd,
                                                    "", "tag push denied")
            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return _CP()
        return real_run_git(cmd, target=target, allow_push=allow_push, check=check)

    rel._run_git = fake_partial
    real_tests_k = _stub_tests_pass()
    so, se = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        rc = cmd_release(argparse.Namespace(version="0.3.1", target=r3, force=False))
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = so
        sys.stderr = se
        rel._run_git = real_run_git
        rel._check_tests = real_tests_k
        shutil.rmtree(os.path.dirname(r3), ignore_errors=True)
    if rc != 1:
        failures.append(f"Case K partial-push should exit 1, got {rc}")
    # main was pushed but tag was not
    if not any("main" in rel._cmd_to_str(c) for c in m2["pushes"]):
        failures.append("Case K: main should have been pushed (partial)")

    # --- Non-git repo sanity ---------------------------------------------------
    nd = tempfile.mkdtemp(prefix="laplace-rel-nongit-")
    so, se = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        # .harness dir present but no .git
        os.makedirs(os.path.join(nd, ".harness", "state"))
        rc = cmd_release(argparse.Namespace(version="0.3.1", target=nd, force=False))
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = so
        sys.stderr = se
        shutil.rmtree(nd, ignore_errors=True)
    if rc != 2:
        failures.append(f"non-git repo should exit 2, got {rc}")

    # --- --force never skips tests (Case L) ------------------------------------
    # If tests fail, --force must NOT let it through. Replace _check_tests
    # with a failing stub and run cmd_release with force=True.
    def fake_tests_fail(target):
        return False, "tests failed (pytest exit 1):\nfake"
    rL = os.path.join(tempfile.mkdtemp(prefix="laplace-rel-L-"), "repo")
    _setup_selftest_repo(rL)
    rel._check_tests = fake_tests_fail
    so, se = sys.stdout, sys.stderr
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        rc = cmd_release(argparse.Namespace(version="0.3.1", target=rL, force=True))
    finally:
        try:
            sys.stdout.close()
            sys.stderr.close()
        except Exception:
            pass
        sys.stdout = so
        sys.stderr = se
        rel._check_tests = real_check_tests
        shutil.rmtree(os.path.dirname(rL), ignore_errors=True)
    if rc != 1:
        failures.append(f"--force must NOT skip tests; expected exit 1, got {rc}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("release selftest: PASS")
    return 0


# --- CLI -----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "selftest":
        return selftest()
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Release a Laplace version: 8-check gate + atomic bump/commit/tag/push.",
    )
    parser.add_argument("version", help="Target version, X.Y.Z (e.g. 0.3.1)")
    parser.add_argument("--target", default=None,
                        help="Repository root containing .harness/ (default: CWD)")
    parser.add_argument("--force", action="store_true",
                        help="Relax downgrade + pending-approved checks only. "
                             "Never skips format/tests/sync/tree/tag/remote.")
    args = parser.parse_args(argv)
    return cmd_release(args)


if __name__ == "__main__":
    sys.exit(main())
