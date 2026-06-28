"""Workflow component (Phase 1): Workflow planning from intent.

This component generates step-by-step implementation plans from
clarified intent, defining evidence collection points and quality gates.

Inspired by MoAI-ADK's Plan phase and LazyCodex's workflow generation.
"""

from .planner import WorkflowComponent
from .auto_generator import (
    WorkflowAutoGenerator,
    WorkflowPlan,
    WorkflowStep,
    QualityGate,
    generate_workflow_from_spec,
)

__all__ = [
    "WorkflowComponent",
    "WorkflowAutoGenerator",
    "WorkflowPlan",
    "WorkflowStep",
    "QualityGate",
    "generate_workflow_from_spec",
]
