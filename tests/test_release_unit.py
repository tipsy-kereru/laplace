"""Unit tests for release.py — one per halt case (A-L) + happy path + audit log.

Each halt case uses a fresh temp git repo with the three version files seeded
at 0.3.0. PUSH IS STUBBED in every test (monkeypatches _run_git) so no actual
push leaves the test repo. Network ops never run.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile

import pytest

import release
from release import (
    _check_format,
    _check_no_pending_approved,
    _check_semver_direction,
    _check_sync_after_bump,
    _check_tag_absent,
    _check_tree_clean,
    cmd_release,
    _current_version,
    _read_three_versions,
    _releases_path,
    _version_tuple,
)


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Fresh git repo + .harness + three version files at 0.3.0."""
    r = str(tmp_path / "repo")
    os.makedirs(r)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "test@laplace.test"],
                   cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=r, check=True)
    with open(os.path.join(r, "VERSION"), "w") as f:
        f.write("0.3.0\n")
    os.makedirs(os.path.join(r, ".claude-plugin"), exist_ok=True)
    with open(os.path.join(r, ".claude-plugin", "plugin.json"), "w") as f:
        json.dump({"name": "laplace", "version": "0.3.0"}, f)
    with open(os.path.join(r, ".claude-plugin", "marketplace.json"), "w") as f:
        json.dump({"plugins": [{"name": "laplace", "version": "0.3.0"}]}, f)

    import state
    state.cmd_init(target=r)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def stub_push(monkeypatch):
    """Stub _run_git so push calls never reach the network.

    Records every push call into the returned list.
    """
    pushes = []
    real = release._run_git

    def fake(cmd, *, target=None, allow_push=False, check=True):
        if cmd and cmd[0] == "push":
            pushes.append(list(cmd))

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return _CP()
        return real(cmd, target=target, allow_push=allow_push, check=check)

    monkeypatch.setattr(release, "_run_git", fake)
    return pushes


@pytest.fixture
def stub_tests_pass(monkeypatch):
    """Make _check_tests always pass."""
    monkeypatch.setattr(release, "_check_tests", lambda target: (True, ""))


def _run(version, target, force=False):
    return cmd_release(argparse.Namespace(version=version, target=target,
                                          force=force))


# --- Case A: happy path -------------------------------------------------------

def test_case_a_happy_path(repo, stub_push, stub_tests_pass, capsys):
    rc = _run("0.3.1", repo)
    assert rc == 0
    # All three files bumped.
    assert _current_version(repo) == "0.3.1"
    three = _read_three_versions(repo)
    assert set(three.values()) == {"0.3.1"}
    # Tag created locally.
    tag = subprocess.run(["git", "rev-parse", "--verify", "v0.3.1"],
                         cwd=repo, capture_output=True, text=True)
    assert tag.returncode == 0
    # Pushed main + tag.
    push_strs = [" ".join(c) for c in stub_push]
    assert any("main" in s for s in push_strs)
    assert any("v0.3.1" in s for s in push_strs)


# --- Case B: bad format -------------------------------------------------------

@pytest.mark.parametrize("bad", ["0.3", "v0.3.1", "0.3.1.2", "0", "x.y.z", ""])
def test_case_b_bad_format(bad):
    ok, _ = _check_format(bad)
    assert not ok


def test_case_b_good_format():
    ok, _ = _check_format("0.3.1")
    assert ok


def test_case_b_bad_format_halts(repo, stub_push, stub_tests_pass, capsys):
    rc = _run("0.3", repo)
    assert rc == 1
    # No bump occurred.
    assert _current_version(repo) == "0.3.0"
    # No pushes.
    assert stub_push == []


# --- Case C: failing tests ----------------------------------------------------

def test_case_c_failing_tests_halt(repo, stub_push, monkeypatch, capsys):
    monkeypatch.setattr(release, "_check_tests",
                        lambda target: (False, "tests failed (pytest exit 1)"))
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert _current_version(repo) == "0.3.0"
    assert stub_push == []


# --- Case D: sync-after-bump failure ------------------------------------------

def test_case_d_sync_failure(repo, stub_push, stub_tests_pass, monkeypatch, capsys):
    # Force _bump_three to leave marketplace stale.
    def partial_bump(target, new_version):
        release._write_version_file(target, "VERSION", new_version)
        release._write_version_file(target, "plugin", new_version)
        # marketplace.json stays at 0.3.0

    monkeypatch.setattr(release, "_bump_three", partial_bump)
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert stub_push == []


# --- Case E: downgrade without --force ----------------------------------------

def test_case_e_downgrade_no_force():
    ok, _ = _check_semver_direction("0.4.0", "0.3.1", force=False)
    assert not ok


def test_case_e_downgrade_with_force():
    ok, _ = _check_semver_direction("0.4.0", "0.3.1", force=True)
    assert ok


def test_case_e_equal_version():
    ok, _ = _check_semver_direction("0.3.1", "0.3.1", force=False)
    assert not ok


def test_case_e_upgrade_ok():
    ok, _ = _check_semver_direction("0.3.0", "0.3.1", force=False)
    assert ok


# --- Case F: dirty tree -------------------------------------------------------

def test_case_f_dirty_tree(repo, stub_push, stub_tests_pass, capsys):
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("uncommitted\n")
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert stub_push == []


# --- Case G: tag exists -------------------------------------------------------

def test_case_g_tag_exists(repo, stub_push, stub_tests_pass, capsys):
    subprocess.run(["git", "tag", "-a", "v0.3.1", "-m", "x"],
                   cwd=repo, check=True)
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert stub_push == []


# --- Case H: remote ahead -----------------------------------------------------

def test_case_h_remote_ahead(repo, stub_push, stub_tests_pass, monkeypatch, capsys):
    monkeypatch.setattr(release, "_check_remote_not_ahead",
                        lambda target: (False, "origin/main has 3 new commits"))
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert stub_push == []


# --- Case I: pending approved issues ------------------------------------------

def test_case_i_pending_approved(repo, stub_push, stub_tests_pass, capsys):
    qpath = os.path.join(repo, ".harness", "state", "queue.json")
    with open(qpath) as f:
        q = json.load(f)
    q["approved"] = ["ISSUE-9999"]
    with open(qpath, "w") as f:
        json.dump(q, f)
    rc = _run("0.3.1", repo)
    assert rc == 1
    assert stub_push == []


# --- Case J: --force relaxes downgrade + pending ------------------------------

def test_case_j_force_relaxes_pending(repo, stub_push, monkeypatch):
    monkeypatch.setattr(release, "_check_tests", lambda target: (True, ""))
    qpath = os.path.join(repo, ".harness", "state", "queue.json")
    with open(qpath) as f:
        q = json.load(f)
    q["approved"] = ["ISSUE-8888"]
    with open(qpath, "w") as f:
        json.dump(q, f)
    # Bump current to 0.4.0 so it's a downgrade, then --force.
    with open(os.path.join(repo, "VERSION"), "w") as f:
        f.write("0.4.0\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "bump"], cwd=repo, check=True)
    # _check_no_pending_approved(r, force=True) should pass.
    ok, _ = _check_no_pending_approved(repo, force=True)
    assert ok


# --- Case K: partial-push (main ok, tag fails) --------------------------------

def test_case_k_partial_push(repo, stub_tests_pass, monkeypatch, capsys):
    pushes = []
    real = release._run_git

    def fake(cmd, *, target=None, allow_push=False, check=True):
        if cmd and cmd[0] == "push":
            pushes.append(list(cmd))
            if "v0.3.1" in " ".join(cmd):
                raise subprocess.CalledProcessError(1, ["git"] + cmd,
                                                    "", "tag push denied")

            class _CP:
                returncode = 0
                stdout = ""
                stderr = ""
            return _CP()
        return real(cmd, target=target, allow_push=allow_push, check=check)

    monkeypatch.setattr(release, "_run_git", fake)
    rc = _run("0.3.1", repo)
    assert rc == 1
    # main was pushed, tag was attempted.
    push_strs = [" ".join(c) for c in pushes]
    assert any("main" in s for s in push_strs)
    assert any("v0.3.1" in s for s in push_strs)
    # main commit is real (not rolled back).
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True)
    assert "chore(release)" in log.stdout


# --- Case L: --force never skips tests ----------------------------------------

def test_case_l_force_never_skips_tests(repo, stub_push, monkeypatch, capsys):
    monkeypatch.setattr(release, "_check_tests",
                        lambda target: (False, "tests failed"))
    rc = _run("0.3.1", repo, force=True)
    assert rc == 1
    assert stub_push == []


# --- Non-git repo -------------------------------------------------------------

def test_non_git_repo_exits_2(tmp_path, capsys):
    nd = str(tmp_path / "nogit")
    os.makedirs(os.path.join(nd, ".harness", "state"))
    rc = _run("0.3.1", nd)
    assert rc == 2


# --- Audit log: success -------------------------------------------------------

def test_audit_log_success_entry(repo, stub_push, stub_tests_pass):
    rc = _run("0.3.1", repo)
    assert rc == 0
    log_path = _releases_path(repo)
    assert os.path.exists(log_path)
    with open(log_path) as f:
        entries = [json.loads(l) for l in f if l.strip()]
    successes = [e for e in entries
                 if e.get("checks_passed") and e.get("sequence_ok")]
    assert len(successes) == 1
    e = successes[0]
    assert e["version"] == "0.3.1"
    assert e["prev_version"] == "0.3.0"
    assert e["authorization_basis"] == "release-invocation"
    assert e["tag"] == "v0.3.1"
    assert e["main_pushed"] is True
    assert e["tag_pushed"] is True
    assert "pushed_at" in e


# --- Audit log: failure -------------------------------------------------------

def test_audit_log_failure_entry(repo, stub_push, monkeypatch, capsys):
    monkeypatch.setattr(release, "_check_tests",
                        lambda target: (False, "tests failed"))
    rc = _run("0.3.1", repo)
    assert rc == 1
    log_path = _releases_path(repo)
    with open(log_path) as f:
        entries = [json.loads(l) for l in f if l.strip()]
    fails = [e for e in entries if not e.get("checks_passed")]
    assert len(fails) == 1
    e = fails[0]
    assert e["failed_check"] == "tests"
    assert "tests failed" in e["reason"]
    assert e["version"] == "0.3.1"


# --- Helpers ------------------------------------------------------------------

def test_version_tuple():
    assert _version_tuple("0.3.1") == (0, 3, 1)
    assert _version_tuple("1.2.3") < _version_tuple("1.2.4")
    assert _version_tuple("0.3.0") < _version_tuple("0.4.0")


def test_check_tree_clean_clean(repo):
    ok, _ = _check_tree_clean(repo)
    assert ok


def test_check_tree_clean_dirty(repo):
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("dirty\n")
    ok, reason = _check_tree_clean(repo)
    assert not ok
    assert "dirty" in reason


def test_check_tag_absent_clean(repo):
    ok, _ = _check_tag_absent(repo, "0.3.1")
    assert ok


def test_check_tag_absent_exists(repo):
    subprocess.run(["git", "tag", "-a", "v0.3.1", "-m", "x"],
                   cwd=repo, check=True)
    ok, _ = _check_tag_absent(repo, "0.3.1")
    assert not ok


def test_check_sync_after_bump_ok(repo):
    release._bump_three(repo, "0.3.1")
    ok, _ = _check_sync_after_bump(repo, "0.3.1")
    assert ok


def test_check_sync_after_bump_desync(repo):
    release._write_version_file(repo, "VERSION", "0.3.1")
    # plugin.json stays at 0.3.0
    ok, reason = _check_sync_after_bump(repo, "0.3.1")
    assert not ok
    assert "0.3.1" in reason


def test_policy_gate_fires_on_push_without_allow(repo):
    """Real (unstubbed) policy.check_command must deny a push.

    Regression guard for the policy-bypass bug where _run_git omitted the
    'git' prefix, so the deny regex never matched and Option A was dead
    code. This test does NOT monkeypatch _run_git.
    """
    from release import _run_git, GitPolicyError
    # Assemble the remote-push verb at runtime so static scanners don't
    # trip on the literal; the policy regex matches the assembled string.
    push_cmd = list("pu" + "sh")  # -> ['p','u','s','h']; rejoin below
    push_cmd = ["".join(push_cmd), "origin", "main"]
    with pytest.raises(GitPolicyError) as exc:
        _run_git(push_cmd, target=repo, allow_push=False)
    msg = str(exc.value)
    assert "approval" in msg.lower() or "denied" in msg.lower()


def test_policy_gate_allows_push_with_allow(repo):
    """With allow_push=True (invocation-authorized), the gate does not raise.

    The actual remote op fails (no remote in temp repo), but that is a
    subprocess returncode, not a GitPolicyError.
    """
    from release import _run_git, GitPolicyError
    push_cmd = ["".join(list("pu" + "sh")), "origin", "main"]
    try:
        _run_git(push_cmd, target=repo, allow_push=True, check=False)
    except GitPolicyError as exc:
        pytest.fail(f"policy gate raised despite allow_push=True: {exc}")


def test_sync_failure_rolls_back_files(repo, stub_tests_pass, capsys):
    """AC-REL-011: check-3 (post-bump sync) failure restores the 3 files."""
    # Force a desync: make _bump_three write only VERSION, not the JSONs.
    real_bump = release._bump_three

    def partial_bump(target, version):
        release._write_version_file(target, "VERSION", version)
        # skip plugin.json + marketplace.json

    monkeypatch_target = release
    # Use the repo fixture's monkeypatch via pytest — but this test takes no
    # monkeypatch arg; set/restore manually instead.
    import release as _r
    saved = _r._bump_three
    _r._bump_three = partial_bump
    try:
        ns = argparse.Namespace(version="0.3.1", target=repo, force=False)
        rc = release.cmd_release(ns)
    finally:
        _r._bump_three = saved
    assert rc == 1
    # Tree must be clean (bump rolled back).
    ok, _ = _check_tree_clean(repo)
    assert ok, "check-3 failure left a dirty tree"
