"""Metrics component (Phase 4 extension): Performance and quality metrics capture.

This component provides:
- Metric collection helpers
- Metric analysis and comparison
- Metric-based evidence capture
- Performance threshold validation

Inspired by performance engineering best practices and continuous monitoring.
"""

from . import collector, analyzer

__all__ = ["collector", "analyzer"]
