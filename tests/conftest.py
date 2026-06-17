"""Pytest configuration for the Laplace plugin test suite.

Adds the plugin's `scripts/` and `hooks/` directories to sys.path so test
modules can import script modules directly. Keeps the runtime scripts
stdlib-only and runnable as `python3 scripts/<name>.py selftest`.

The runtime plugin itself has NO third-party dependencies. pytest is a
dev-only dependency for this test suite. If pytest is not installed, the
embedded `selftest` subcommand on each script remains the canonical
runtime check.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
HOOKS = os.path.join(PLUGIN_ROOT, "hooks")
for p in (SCRIPTS, HOOKS, PLUGIN_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
