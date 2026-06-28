"""Execute component (Phase 2): Implementation execution with evidence capture.

This component extends the existing runner.py with workflow-aware execution,
executing steps in dependency order and enforcing gates between steps.
"""

from .executor import ExecuteComponent

__all__ = ["ExecuteComponent"]
