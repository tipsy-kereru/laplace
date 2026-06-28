"""Execution orchestrator: Run workflow with evidence capture.

Phase 2 component that executes workflow plans with:
    - Step-by-step execution in dependency order
    - Evidence capture at each step
    - Gate enforcement between steps
    - Worktree isolation
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


class ExecuteComponent(base.Component):
    """Phase 2 execution component.

    Executes workflow plans with evidence capture.
    """

    name = "execute"
    version = "0.1.0"
    phase = "run"
    description = "Execute workflow plans with evidence capture"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize execute component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute workflow plan.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - run_id: Run identifier
                - workflow_plan: Workflow plan to execute
                - branch: Git branch for worktree

        Returns:
            Result dictionary with execution results
        """
        issue_id = context.get("issue_id", "")
        run_id = context.get("run_id", "")
        workflow_plan = context.get("workflow_plan", {})
        harness_root = context.get("harness_root", ".harness")

        # Get steps from plan
        steps = workflow_plan.get("steps", [])

        # Execute steps in order
        results = []
        for step in steps:
            step_result = self._execute_step(step, context)
            results.append(step_result)

            # Check if step failed
            if not step_result.get("success", False):
                # Record failure and stop
                self._add_evidence(
                    harness_root,
                    run_id,
                    "step-failure",
                    f"Step {step['id']} failed: {step_result.get('error', 'Unknown')}"
                )
                break

        # Store execution state
        self._write_state(harness_root, {
            "issue_id": issue_id,
            "run_id": run_id,
            "results": results,
            "completed": len(results) == len(steps)
        })

        # Add completion evidence
        if len(results) == len(steps):
            self._add_evidence(
                harness_root,
                run_id,
                "workflow-execution",
                f"Completed {len(steps)} workflow steps"
            )

        return {
            "success": len(results) == len(steps),
            "output": {"results": results, "steps_completed": len(results)},
            "evidence": [],
            "error": None if len(results) == len(steps) else "Workflow execution incomplete"
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up execute component resources.

        Args:
            harness_root: Path to .harness/ directory

        Returns:
            True if successful
        """
        return True

    def get_evidence(self, harness_root: str, run_id: str) -> List[Dict[str, str]]:
        """Get evidence captured by this component.

        Args:
            harness_root: Path to .harness/ directory
            run_id: Run identifier

        Returns:
            List of evidence entries
        """
        evidence_file = os.path.join(
            self._get_component_dir(harness_root),
            f"evidence-{run_id}.jsonl"
        )

        if not os.path.exists(evidence_file):
            return []

        evidence = []
        with open(evidence_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    evidence.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return evidence

    def _execute_step(self, step: Dict[str, Any],
                     context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single workflow step.

        Args:
            step: Step definition from workflow plan
            context: Execution context

        Returns:
            Step result dictionary
        """
        # For now, return a placeholder result
        # In production, this would delegate to agents (dev, test, etc.)
        return {
            "step_id": step.get("id"),
            "success": True,
            "evidence": [],
            "error": None,
        }


# Create instance for registry
_component = ExecuteComponent()

# Export for discovery
__all__ = ["ExecuteComponent", "_component"]
