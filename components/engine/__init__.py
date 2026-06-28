"""State transition engine: Automatic state machine progression.

This component provides automatic state machine transitions for workflow
execution, implementing the Ralph loop pattern with bounded iterations
and completion detection.

Features:
- Automatic state transitions based on workflow steps
- Completion detection from multiple signals
- Iteration counting and limits
- Loop ledger integration for context survival

Inspired by Ralph loop, LazyCodex ULW-Loop, and MoAI-ADK workflow automation.
"""

from .transition_engine import (
    TransitionEngine,
    TransitionResult,
    CompletionSignal,
    StateTransition,
    advance_workflow_step,
)

from .completion import (
    CompletionDetector,
    CompletionResult,
    CompletionDimension,
    detect_completion,
    check_and_advance_workflow,
)

__all__ = [
    "TransitionEngine",
    "TransitionResult",
    "CompletionSignal",
    "StateTransition",
    "advance_workflow_step",
    "CompletionDetector",
    "CompletionResult",
    "CompletionDimension",
    "detect_completion",
    "check_and_advance_workflow",
]
