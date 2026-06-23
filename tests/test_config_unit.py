"""Unit tests for state.load_config and the new queue-runner config keys.

Covers AC-QR-001/002/003 for ISSUE-0001:
  - load_config applies defaults when new keys are absent (old-format config).
  - load_config rejects invalid merge_policy with exit code 2.
  - load_config rejects non-positive max_queue_run with exit code 2.
  - `state.py init` writes both new keys into generated config.yml.
"""
import os
import subprocess
import sys
import tempfile

import pytest

import state

# Old-format config.yml (pre-ISSUE-0001): no max_queue_run, no merge_policy.
OLD_CONFIG_YML = """\
# Laplace runtime configuration (legacy format, missing new keys).
limits:
  max_fix_attempts: 3
  max_pm_clarification_attempts: 2
  max_security_fix_attempts: 2
  max_runtime_minutes_per_issue: 60
  max_files_changed_without_approval: 20
  max_diff_lines_without_approval: 1000
  max_stop_hook_iterations: 12
policy:
  require_approval_for:
    - git_push
    - pr_creation
redaction:
  enabled: true
  store_raw_command_output: false
"""


def _write_config(target: str, text: str) -> str:
    """Write text into <target>/.harness/config.yml and return its path."""
    harness = os.path.join(target, ".harness")
    os.makedirs(harness, exist_ok=True)
    path = os.path.join(harness, "config.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_load_config_applies_defaults_when_keys_absent() -> None:
    """AC-QR-003: old-format config.yml without new keys still loads."""
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, OLD_CONFIG_YML)
        cfg = state.load_config(target=tmp)
    assert cfg["max_queue_run"] == state.MAX_QUEUE_RUN == 5
    assert cfg["merge_policy"] == state.DEFAULT_MERGE_POLICY
    assert cfg["merge_policy"] == "wait-for-human-merge"


def test_load_config_rejects_invalid_merge_policy() -> None:
    """AC-QR-002: invalid merge_policy rejected at load, exit code 2."""
    bad = OLD_CONFIG_YML.replace(
        "policy:\n",
        "policy:\n  merge_policy: stack-branches\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, bad)
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_load_config_rejects_non_positive_max_queue_run() -> None:
    """max_queue_run must be a positive int; zero is rejected, exit code 2."""
    bad = OLD_CONFIG_YML.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_queue_run: 0\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, bad)
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_load_config_rejects_non_int_max_queue_run() -> None:
    """Non-integer max_queue_run is rejected with exit code 2."""
    bad = OLD_CONFIG_YML.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_queue_run: plenty\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, bad)
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_load_config_accepts_valid_auto_merge_policy() -> None:
    """The other valid policy value loads cleanly with explicit value."""
    good = OLD_CONFIG_YML.replace(
        "policy:\n",
        "policy:\n  merge_policy: auto-merge-branch\n",
    )
    good = good.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_queue_run: 8\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, good)
        cfg = state.load_config(target=tmp)
    assert cfg["merge_policy"] == "auto-merge-branch"
    assert cfg["max_queue_run"] == 8


def test_load_config_default_max_parallel_when_key_absent() -> None:
    """ISSUE-0004: old-format config.yml without max_parallel defaults to 2."""
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, OLD_CONFIG_YML)
        cfg = state.load_config(target=tmp)
    assert cfg["max_parallel"] == state.MAX_PARALLEL == 2


def test_load_config_accepts_explicit_max_parallel() -> None:
    """An explicit positive max_parallel is parsed and returned."""
    good = OLD_CONFIG_YML.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_parallel: 4\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, good)
        cfg = state.load_config(target=tmp)
    assert cfg["max_parallel"] == 4


def test_load_config_rejects_non_positive_max_parallel() -> None:
    """Zero max_parallel is rejected with exit code 2."""
    bad = OLD_CONFIG_YML.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_parallel: 0\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, bad)
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_load_config_rejects_non_int_max_parallel() -> None:
    """Non-integer max_parallel is rejected with exit code 2."""
    bad = OLD_CONFIG_YML.replace(
        "  max_stop_hook_iterations: 12\n",
        "  max_stop_hook_iterations: 12\n  max_parallel: plenty\n",
    )
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        _write_config(tmp, bad)
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_init_writes_both_new_keys() -> None:
    """AC-QR-001: `state.py init` writes both new keys into config.yml."""
    with tempfile.TemporaryDirectory(prefix="laplace-init-") as tmp:
        rc = state.cmd_init(target=tmp)
        assert rc == 0
        path = os.path.join(tmp, ".harness", "config.yml")
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        assert "max_queue_run:" in text
        assert "max_parallel:" in text
        assert "merge_policy:" in text
        # And the generated config must itself load without error.
        cfg = state.load_config(target=tmp)
    assert cfg["max_queue_run"] == 5
    assert cfg["max_parallel"] == 2
    assert cfg["merge_policy"] == "wait-for-human-merge"


def test_load_config_missing_file_exits_two() -> None:
    """Missing config.yml is a hard error, not silent defaults."""
    with tempfile.TemporaryDirectory(prefix="laplace-cfg-") as tmp:
        with pytest.raises(SystemExit) as exc:
            state.load_config(target=tmp)
    assert exc.value.code == 2


def test_state_py_selftest_still_passes() -> None:
    """Regression: state.py selftest (which now asserts the new keys) passes."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "state.py"), "selftest"],
        capture_output=True, text=True, timeout=120, cwd=root,
    )
    assert result.returncode == 0, (
        f"state.py selftest failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
