"""Multi-dimensional completion detection system.

Provides comprehensive completion detection using multiple signals:
- Transcript signals (explicit completion messages)
- Evidence requirements (all required evidence captured)
- Convergence detection (agent results stabilize)
- State machine validation (all phases complete)

Inspired by Claude Code's evaluation agent pattern and LazyCodex's
evidence-driven completion.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum


class CompletionDimension(Enum):
    """Dimensions for completion detection."""

    SIGNAL = "signal"  # Explicit completion signal in transcript
    EVIDENCE = "evidence"  # Required evidence captured
    CONVERGENCE = "convergence"  # Agent results converged
    STATE_MACHINE = "state_machine"  # All phases complete
    MANUAL = "manual"  # Manual completion confirmation


class CompletionResult:
    """Result of completion detection."""

    def __init__(
        self,
        is_complete: bool,
        confidence: float,  # 0.0 to 1.0
        dimensions: Dict[CompletionDimension, bool],
        reasoning: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.is_complete = is_complete
        self.confidence = confidence
        self.dimensions = dimensions
        self.reasoning = reasoning
        self.metadata = metadata or {}
        self.detected_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "is_complete": self.is_complete,
            "confidence": self.confidence,
            "dimensions": {d.value: v for d, v in self.dimensions.items()},
            "reasoning": self.reasoning,
            "metadata": self.metadata,
            "detected_at": self.detected_at,
        }


class CompletionDetector:
    """Multi-dimensional completion detection system."""

    def __init__(
        self,
        harness_root: str,
        completion_signals: Optional[List[str]] = None,
        required_evidence: Optional[List[str]] = None,
        convergence_threshold: int = 3,  # Same result N times
    ):
        self.harness_root = harness_root
        self.completion_signals = completion_signals or [
            "COMPLETE", "DONE", "FINISHED", "SUCCESS", "LAPLACE-P0P6-COMPLETE"
        ]
        self.required_evidence = required_evidence or ["test", "review", "audit-report"]
        self.convergence_threshold = convergence_threshold
        self.convergence_history: List[str] = []

    def detect_completion(
        self,
        transcript: Optional[str] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        agent_results: Optional[List[Any]] = None,
        current_phase: Optional[str] = None,
        total_phases: Optional[List[str]] = None,
    ) -> CompletionResult:
        """Detect completion across multiple dimensions.

        Args:
            transcript: Agent transcript to check for signals
            evidence: Captured evidence
            agent_results: Results from agents (for convergence)
            current_phase: Current workflow phase
            total_phases: List of all phases (for state machine check)

        Returns:
            CompletionResult
        """
        dimensions = {}
        confidence_sum = 0.0
        confidence_count = 0

        # Dimension 1: Signal detection
        signal_complete = self._check_signal(transcript)
        dimensions[CompletionDimension.SIGNAL] = signal_complete
        if signal_complete:
            confidence_sum += 1.0
        else:
            confidence_sum += 0.0
        confidence_count += 1

        # Dimension 2: Evidence requirements
        evidence_complete = self._check_evidence(evidence)
        dimensions[CompletionDimension.EVIDENCE] = evidence_complete
        if evidence_complete:
            confidence_sum += 1.0
        else:
            confidence_sum += 0.5  # Partial credit
        confidence_count += 1

        # Dimension 3: Convergence detection
        convergence_complete = self._check_convergence(agent_results)
        dimensions[CompletionDimension.CONVERGENCE] = convergence_complete
        if convergence_complete:
            confidence_sum += 1.0
        else:
            confidence_sum += 0.3  # Low credit for no convergence
        confidence_count += 1

        # Dimension 4: State machine validation
        state_complete = self._check_state_machine(current_phase, total_phases)
        dimensions[CompletionDimension.STATE_MACHINE] = state_complete
        if state_complete:
            confidence_sum += 1.0
        else:
            confidence_sum += 0.0
        confidence_count += 1

        # Calculate overall confidence
        confidence = confidence_sum / confidence_count if confidence_count > 0 else 0.0

        # Determine completion
        # Complete if: signal AND (evidence OR state_machine)
        is_complete = (
            signal_complete and (evidence_complete or state_complete)
        ) or (
            confidence >= 0.75  # High confidence threshold
        )

        # Generate reasoning
        reasoning = self._generate_reasoning(dimensions, confidence)

        return CompletionResult(
            is_complete=is_complete,
            confidence=confidence,
            dimensions=dimensions,
            reasoning=reasoning,
            metadata={
                "convergence_history_length": len(self.convergence_history),
                "evidence_count": len(evidence) if evidence else 0,
            },
        )

    def _check_signal(self, transcript: Optional[str]) -> bool:
        """Check for completion signal in transcript."""
        if not transcript:
            return False

        transcript_lower = transcript.lower()
        for signal in self.completion_signals:
            if signal.lower() in transcript_lower:
                return True

        return False

    def _check_evidence(self, evidence: Optional[List[Dict[str, Any]]]) -> bool:
        """Check if all required evidence is captured."""
        if not evidence:
            return False

        captured_kinds = {e.get("kind") for e in evidence}
        required = set(self.required_evidence)

        return required.issubset(captured_kinds)

    def _check_convergence(self, agent_results: Optional[List[Any]]) -> bool:
        """Check if agent results have converged."""
        if not agent_results:
            return False  # No results to check

        # Simple convergence: last N results are the same
        if len(self.convergence_history) >= self.convergence_threshold:
            last_results = self.convergence_history[-self.convergence_threshold:]
            if len(set(last_results)) == 1:
                return True  # Converged

        # Add latest result to history
        if agent_results:
            # Create a simple hash of results
            result_hash = json.dumps(agent_results, sort_keys=True, default=str)
            self.convergence_history.append(result_hash)

        return False

    def _check_state_machine(
        self,
        current_phase: Optional[str],
        total_phases: Optional[List[str]],
    ) -> bool:
        """Check if all phases are complete."""
        if not current_phase or not total_phases:
            return False

        # Check if we're past the last phase
        # Assuming phases are ordered
        terminal_phases = ["complete", "done", "finished", "review-passed", "all-steps-complete"]
        return current_phase in terminal_phases

    def _generate_reasoning(
        self,
        dimensions: Dict[CompletionDimension, bool],
        confidence: float,
    ) -> str:
        """Generate human-readable reasoning."""
        parts = []

        for dim, passed in dimensions.items():
            status = "✓" if passed else "✗"
            parts.append(f"{status} {dim.value}")

        confidence_pct = f"{confidence * 100:.0f}%"

        return f"Completion detection: {', '.join(parts)} (confidence: {confidence_pct})"

    def reset_convergence_history(self) -> None:
        """Reset convergence history."""
        self.convergence_history = []


def detect_completion(
    harness_root: str,
    transcript: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    agent_results: Optional[List[Any]] = None,
    current_phase: Optional[str] = None,
    total_phases: Optional[List[str]] = None,
) -> CompletionResult:
    """Convenience function for completion detection.

    Args:
        harness_root: Path to harness root
        transcript: Agent transcript
        evidence: Captured evidence
        agent_results: Results from agents
        current_phase: Current workflow phase
        total_phases: List of all phases

    Returns:
        CompletionResult
    """
    detector = CompletionDetector(harness_root)
    return detector.detect_completion(
        transcript=transcript,
        evidence=evidence,
        agent_results=agent_results,
        current_phase=current_phase,
        total_phases=total_phases,
    )


# Integration with workflow execution

def check_and_advance_workflow(
    harness_root: str,
    current_step: Dict[str, Any],
    next_step: Optional[Dict[str, Any]],
    transcript: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[CompletionResult, Optional["TransitionResult"]]:
    """Check completion and advance workflow if not complete.

    Args:
        harness_root: Path to harness root
        current_step: Current workflow step
        next_step: Next workflow step
        transcript: Agent transcript
        evidence: Captured evidence

    Returns:
        (CompletionResult, TransitionResult or None)
    """
    from .transition_engine import advance_workflow_step

    # Detect completion
    completion = detect_completion(
        harness_root=harness_root,
        transcript=transcript,
        evidence=evidence,
    )

    if completion.is_complete:
        return completion, None

    # Not complete, advance workflow
    transition = advance_workflow_step(
        current_step=current_step,
        next_step=next_step,
        harness_root=harness_root,
        evidence=evidence,
    )

    return completion, transition
