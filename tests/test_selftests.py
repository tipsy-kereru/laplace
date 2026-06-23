"""Parametrized runner for every script/hook embedded selftest.

Each script ships a `selftest` subcommand that exercises its own invariants
using stdlib only. This module invokes each via subprocess and asserts exit 0,
giving pytest collection, reporting, and coverage hooks without duplicating
the (extensive) selftest logic.

To run: `pytest tests/ -v`
"""
import os
import subprocess
import sys

import pytest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (relative_path, label) for every script/hook that exposes a selftest.
SELFTEST_TARGETS = [
    ("scripts/redaction.py", "redaction"),
    ("scripts/policy.py", "policy"),
    ("scripts/state.py", "state"),
    ("scripts/intake.py", "intake"),
    ("scripts/runner.py", "runner"),
    ("scripts/queue_runner.py", "queue-runner"),
    ("scripts/report.py", "report"),
    ("scripts/profile.py", "profile"),
    ("scripts/validate.py", "validate"),
    ("scripts/pipeline.py", "pipeline"),
    ("hooks/pretooluse.py", "pretooluse"),
    ("hooks/posttooluse.py", "posttooluse"),
    ("hooks/stop-loop.py", "stop-loop"),
]


@pytest.mark.parametrize("rel,label", SELFTEST_TARGETS)
def test_selftest_passes(rel: str, label: str) -> None:
    path = os.path.join(PLUGIN_ROOT, rel)
    assert os.path.isfile(path), f"missing: {path}"
    result = subprocess.run(
        [sys.executable, path, "selftest"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=PLUGIN_ROOT,
    )
    assert result.returncode == 0, (
        f"{label} selftest failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
