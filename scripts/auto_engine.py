#!/usr/bin/env python3
"""
Laplace Auto-Execution Engine

Main orchestrator for automated workflow execution.
Integrates spec generation, workflow planning, agent dispatch, and completion detection.

Inspired by:
- LazyCodex ULW-Loop (conductor-workers, evidence-driven)
- MoAI-ADK (multi-phase workflow, checkpoint recovery)
- Ralph Loop (fail-safe, bounded iterations)
- Claude Code goal/ultrawork (convergence detection)

Usage:
    python3 scripts/auto_engine.py <prd.md> [--target <dir>]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from components.intent import generate_spec_from_prd
from components.workflow import generate_workflow_from_spec
from components.dispatcher import AgentDispatcher, AgentType
from components.engine import CompletionDetector, check_and_advance_workflow


class LaplaceAutoEngine:
    """Main auto-execution engine for Laplace."""

    def __init__(
        self,
        harness_root: str,
        max_iterations: int = 12,
        verbose: bool = False,
    ):
        self.harness_root = harness_root
        self.max_iterations = max_iterations
        self.verbose = verbose

        # Initialize components
        self.dispatcher = AgentDispatcher(harness_root)
        self.completion_detector = CompletionDetector(harness_root)

        # State
        self.current_iteration = 0
        self.evidence: List[Dict[str, Any]] = []

        # Ensure directories exist
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        for dir_path in [
            os.path.join(self.harness_root, ".harness", "specs"),
            os.path.join(self.harness_root, ".harness", "workflows"),
            os.path.join(self.harness_root, ".harness", "evidence"),
            os.path.join(self.harness_root, ".harness", "loop"),
        ]:
            os.makedirs(dir_path, exist_ok=True)

    def _log(self, message: str) -> None:
        """Log message."""
        if self.verbose:
            print(f"[Laplace] {message}")

    def execute_from_prd(
        self,
        prd_path: str,
    ) -> Dict[str, Any]:
        """Execute complete workflow from PRD.

        Args:
            prd_path: Path to PRD markdown file

        Returns:
            Execution result dictionary
        """
        self._log(f"Starting auto-execution from PRD: {prd_path}")

        # Phase 1: Generate SPEC from PRD
        self._log("Phase 1: Generating SPEC from PRD...")
        ok, message, spec = generate_spec_from_prd(prd_path, self.harness_root)
        if not ok:
            return {
                "success": False,
                "error": f"SPEC generation failed: {message}",
                "phase": "spec-generation",
            }

        # Extract path from message (format: "SPEC saved to <path>")
        if "saved to" in message:
            spec_path = message.split("saved to ")[-1].strip()
        else:
            spec_path = message

        spec_id = spec.spec_id if spec else "unknown"
        self._log(f"✓ SPEC generated: {spec_id}")

        # Read SPEC content
        with open(spec_path, "r") as f:
            spec_content = f.read()

        # Phase 2: Generate workflow from SPEC
        self._log("Phase 2: Generating workflow from SPEC...")
        ok, message, plan = generate_workflow_from_spec(spec_path, self.harness_root)
        if not ok:
            return {
                "success": False,
                "error": f"Workflow generation failed: {message}",
                "phase": "workflow-generation",
            }

        workflow_plan = plan.to_dict() if plan else {}
        plan_id = plan.plan_id if plan else "unknown"
        self._log(f"✓ Workflow generated: {plan_id}")

        # Phase 3: Execute workflow
        self._log("Phase 3: Executing workflow...")
        execution_result = self._execute_workflow(workflow_plan, spec_content)

        return execution_result

    def _execute_workflow(
        self,
        workflow: Dict[str, Any],
        spec_content: str,
    ) -> Dict[str, Any]:
        """Execute workflow plan.

        Args:
            workflow: Workflow plan dictionary
            spec_content: SPEC document content

        Returns:
            Execution result
        """
        steps = workflow.get("steps", [])
        total_steps = len(steps)

        self._log(f"Executing {total_steps} workflow steps...")

        current_step_index = 0
        agent_results: List[Any] = []

        while self.current_iteration < self.max_iterations:
            self.current_iteration += 1
            self._log(f"\n--- Iteration {self.current_iteration}/{self.max_iterations} ---")

            # Get current step
            if current_step_index >= total_steps:
                self._log("All steps completed")
                # Mark as complete via state machine check
                completion_result = self.completion_detector.detect_completion(
                    transcript=None,
                    evidence=self.evidence,
                    agent_results=agent_results,
                    current_phase="all-steps-complete",  # Terminal state
                    total_phases=[s.get("phase") for s in steps],
                )
                self._log(f"✓ Workflow complete!")
                return {
                    "success": True,
                    "completed": True,
                    "iterations": self.current_iteration,
                    "evidence_count": len(self.evidence),
                    "completion": completion_result.to_dict(),
                }

            current_step = steps[current_step_index]
            step_name = current_step.get("name", f"step-{current_step_index}")
            step_phase = current_step.get("phase", "dev")

            self._log(f"Current step: {step_name} (phase: {step_phase})")

            # Dispatch agent for current step
            agent_result = self._dispatch_agent_for_step(
                current_step,
                spec_content,
            )

            if agent_result:
                agent_results.append(agent_result)
                self._log(f"✓ Agent completed: {agent_result.get('status', 'unknown')}")

                # Capture evidence if provided
                if "evidence" in agent_result:
                    self.evidence.extend(agent_result["evidence"])

            # Check completion
            completion_result = self.completion_detector.detect_completion(
                transcript=agent_result.get("output") if agent_result else None,
                evidence=self.evidence,
                agent_results=agent_results,
                current_phase=step_phase,
                total_phases=[s.get("phase") for s in steps],
            )

            self._log(f"Completion check: {completion_result.reasoning}")

            if completion_result.is_complete:
                self._log("✓ Workflow complete!")
                return {
                    "success": True,
                    "completed": True,
                    "iterations": self.current_iteration,
                    "evidence_count": len(self.evidence),
                    "completion": completion_result.to_dict(),
                }

            # Move to next step
            current_step_index += 1

        # Max iterations reached
        self._log(f"Max iterations ({self.max_iterations}) reached")

        return {
            "success": False,
            "completed": False,
            "iterations": self.current_iteration,
            "evidence_count": len(self.evidence),
            "reason": "max_iterations_reached",
        }

    def _dispatch_agent_for_step(
        self,
        step: Dict[str, Any],
        spec_content: str,
    ) -> Optional[Dict[str, Any]]:
        """Dispatch appropriate agent for workflow step.

        Args:
            step: Workflow step dictionary
            spec_content: SPEC document content

        Returns:
            Agent result dictionary or None
        """
        step_phase = step.get("phase", "dev")

        # Map phase to agent type (using AgentSpec enum values)
        phase_to_agent = {
            "pm": AgentType.PM,
            "architect": AgentType.ARCHITECT,
            "dev": AgentType.DEV,
            "review": AgentType.REVIEWER,
            "security": AgentType.SECURITY,
            "qa": AgentType.QA,
            "analyze": AgentType.PM,  # Map analyze to PM
            "design": AgentType.ARCHITECT,  # Map design to ARCHITECT
            "implement": AgentType.DEV,  # Map implement to DEV
            "test": AgentType.QA,  # Map test to QA
        }

        agent_type = phase_to_agent.get(step_phase)
        if not agent_type:
            self._log(f"Unknown phase: {step_phase}, skipping")
            return None

        # Prepare context
        context = {
            "spec": spec_content,
            "step": step,
            "iteration": self.current_iteration,
        }

        # Dispatch agent
        try:
            result = self.dispatcher.dispatch_agent(
                agent_type=agent_type,
                context=context,
            )

            return result

        except Exception as e:
            self._log(f"Agent dispatch failed: {e}")
            return {
                "status": "error",
                "error": str(e),
            }


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Laplace Auto-Execution Engine",
    )
    parser.add_argument(
        "prd",
        help="Path to PRD markdown file",
    )
    parser.add_argument(
        "--target",
        help="Harness root directory (default: current directory)",
        default=None,
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=12,
        help="Maximum iterations (default: 12)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Resolve harness root
    harness_root = args.target or os.getcwd()

    # Verify PRD exists
    if not os.path.isfile(args.prd):
        print(f"Error: PRD file not found: {args.prd}", file=sys.stderr)
        return 1

    # Initialize engine
    engine = LaplaceAutoEngine(
        harness_root=harness_root,
        max_iterations=args.max_iterations,
        verbose=args.verbose,
    )

    # Execute
    try:
        result = engine.execute_from_prd(args.prd)

        # Output result
        print("\n" + "=" * 60)
        print("Laplace Auto-Execution Result")
        print("=" * 60)
        print(json.dumps(result, indent=2))

        return 0 if result.get("success") else 1

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
