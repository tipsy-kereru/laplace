"""State transition engine implementation.

Provides automatic state machine progression with completion detection
and loop control.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum


class CompletionSignal(Enum):
    """Types of completion signals."""

    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    MANUAL = "manual"


class TransitionStatus(Enum):
    """Status of state transition."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class StateTransition:
    """Represents a state transition."""

    def __init__(
        self,
        from_state: str,
        to_state: str,
        triggered_by: str,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.from_state = from_state
        self.to_state = to_state
        self.triggered_by = triggered_by
        self.reason = reason
        self.metadata = metadata or {}
        self.timestamp = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "triggered_by": self.triggered_by,
            "reason": self.reason,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


class TransitionResult:
    """Result of a transition attempt."""

    def __init__(
        self,
        success: bool,
        transition: Optional[StateTransition] = None,
        error: Optional[str] = None,
    ):
        self.success = success
        self.transition = transition
        self.error = error
        self.timestamp = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "transition": self.transition.to_dict() if self.transition else None,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class TransitionEngine:
    """Automatic state transition engine.

    Implements Ralph loop pattern with bounded iterations and completion
    detection from multiple signals.
    """

    def __init__(
        self,
        harness_root: str,
        max_iterations: int = 12,
        completion_signals: Optional[List[str]] = None,
    ):
        self.harness_root = harness_root
        self.max_iterations = max_iterations
        self.completion_signals = completion_signals or ["COMPLETE", "DONE", "FINISHED"]
        self.state_file = os.path.join(
            harness_root, ".harness", "state", "active-loop.local.json"
        )
        self.transition_log = os.path.join(
            harness_root, ".harness", "state", "transitions.jsonl"
        )

    def execute_transition(
        self,
        from_state: str,
        to_state: str,
        triggered_by: str,
        reason: str = "",
    ) -> TransitionResult:
        """Execute a state transition.

        Args:
            from_state: Current state
            to_state: Target state
            triggered_by: What triggered the transition
            reason: Reason for transition

        Returns:
            TransitionResult
        """
        # Check iteration limit
        iteration = self._get_iteration()
        if iteration >= self.max_iterations:
            return TransitionResult(
                success=False,
                error=f"Max iterations ({self.max_iterations}) exceeded",
            )

        # Create transition
        transition = StateTransition(
            from_state=from_state,
            to_state=to_state,
            triggered_by=triggered_by,
            reason=reason,
        )

        # Log transition
        self._log_transition(transition)

        # Update state file
        self._update_state_file(transition)

        return TransitionResult(success=True, transition=transition)

    def check_completion(
        self,
        transcript: Optional[str] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, CompletionSignal, str]:
        """Check if workflow is complete.

        Args:
            transcript: Agent transcript to check for completion signals
            evidence: Captured evidence to validate

        Returns:
            (is_complete, signal_type, reason) tuple
        """
        # Check transcript for completion signals
        if transcript:
            for signal in self.completion_signals:
                if signal.lower() in transcript.lower():
                    return True, CompletionSignal.SUCCESS, f"Found completion signal: {signal}"

        # Check evidence requirements
        if evidence:
            # Validate that required evidence is present
            required_kinds = self._get_required_evidence()
            captured_kinds = {e.get("kind") for e in evidence}

            missing = required_kinds - captured_kinds
            if not missing:
                return True, CompletionSignal.SUCCESS, "All required evidence captured"

        # Not complete
        return False, CompletionSignal.MANUAL, "Workflow not complete"

    def advance_workflow(
        self,
        current_step: Dict[str, Any],
        next_step: Optional[Dict[str, Any]],
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> TransitionResult:
        """Advance workflow to next step.

        Args:
            current_step: Current workflow step
            next_step: Next workflow step (None if complete)
            evidence: Evidence captured from current step

        Returns:
            TransitionResult
        """
        if next_step is None:
            # Workflow complete
            return self.execute_transition(
                from_state=current_step.get("phase", "running"),
                to_state="complete",
                triggered_by="workflow-engine",
                reason="All steps completed",
            )

        # Check evidence gates
        gate_result = self._check_evidence_gate(current_step, evidence)
        if not gate_result[0]:
            return TransitionResult(
                success=False,
                error=f"Evidence gate failed: {gate_result[1]}",
            )

        # Advance to next step
        return self.execute_transition(
            from_state=current_step.get("phase", "running"),
            to_state=next_step.get("phase", "running"),
            triggered_by="workflow-engine",
            reason=f"Step {current_step.get('step_id')} completed",
        )

    def _get_iteration(self) -> int:
        """Get current iteration count."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                    return state.get("iteration", 0)
        except (IOError, json.JSONDecodeError):
            pass
        return 0

    def _update_state_file(self, transition: StateTransition) -> None:
        """Update the state file with transition."""
        try:
            # Read current state
            state = {}
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)

            # Update iteration
            state["iteration"] = state.get("iteration", 0) + 1
            state["last_transition"] = transition.to_dict()
            state["current_state"] = transition.to_state

            # Atomic write
            tmp_file = self.state_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, self.state_file)

        except (IOError, json.JSONDecodeError):
            pass  # Don't fail on state file errors

    def _log_transition(self, transition: StateTransition) -> None:
        """Log transition to JSONL file."""
        try:
            os.makedirs(os.path.dirname(self.transition_log), exist_ok=True)
            with open(self.transition_log, "a") as f:
                f.write(json.dumps(transition.to_dict()) + "\n")
        except IOError:
            pass

    def _check_evidence_gate(
        self,
        step: Dict[str, Any],
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, str]:
        """Check if evidence requirements are met for a step.

        Args:
            step: Workflow step
            evidence: Captured evidence

        Returns:
            (passed, reason) tuple
        """
        required = step.get("evidence_required", [])
        if not required:
            return True, "No evidence required"

        if not evidence:
            return False, f"Missing required evidence: {required}"

        captured_kinds = {e.get("kind") for e in evidence}
        missing = set(required) - captured_kinds

        if missing:
            return False, f"Missing evidence kinds: {missing}"

        return True, "All required evidence present"

    def _get_required_evidence(self) -> set:
        """Get required evidence kinds for completion."""
        # This would be loaded from workflow or config
        return {"test", "review", "audit-report"}

    def get_state(self) -> Dict[str, Any]:
        """Get current engine state.

        Returns:
            Current state dictionary
        """
        state = {
            "harness_root": self.harness_root,
            "max_iterations": self.max_iterations,
            "completion_signals": self.completion_signals,
            "iteration": self._get_iteration(),
        }

        # Load last transition if exists
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    file_state = json.load(f)
                    state["last_transition"] = file_state.get("last_transition")
                    state["current_state"] = file_state.get("current_state")
            except (IOError, json.JSONDecodeError):
                pass

        return state

    def reset(self) -> None:
        """Reset the engine state."""
        try:
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        except OSError:
            pass


def create_transition_engine(
    harness_root: str,
    max_iterations: int = 12,
    completion_signals: Optional[List[str]] = None,
) -> TransitionEngine:
    """Create a transition engine.

    Args:
        harness_root: Path to harness root
        max_iterations: Maximum loop iterations
        completion_signals: List of completion signal strings

    Returns:
        TransitionEngine instance
    """
    return TransitionEngine(
        harness_root=harness_root,
        max_iterations=max_iterations,
        completion_signals=completion_signals,
    )


# Convenience function for workflow advancement

def advance_workflow_step(
    current_step: Dict[str, Any],
    next_step: Optional[Dict[str, Any]],
    harness_root: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> TransitionResult:
    """Advance workflow to next step.

    Convenience function for workflow execution.

    Args:
        current_step: Current workflow step
        next_step: Next workflow step (None if complete)
        harness_root: Path to harness root
        evidence: Evidence captured from current step

    Returns:
        TransitionResult
    """
    engine = create_transition_engine(harness_root)
    return engine.advance_workflow(current_step, next_step, evidence)
