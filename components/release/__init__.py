"""Release component (Phase 4): Release automation with quality gates.

This component provides release automation with:
    - Quality gate enforcement
    - Pre-release validation
    - Release artifact generation
"""

from .gates import ReleaseComponent

__all__ = ["ReleaseComponent"]
