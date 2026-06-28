"""Workflow planner: Generate execution plans from intent.

Phase 1 component that takes clarified intent and produces:
    - Step-by-step implementation plan
    - Evidence collection points
    - Quality gate definitions
    - Risk identification
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


class WorkflowComponent(base.Component):
    """Phase 1 workflow planning component.

    Generates structured workflow plans from clarified intent.
    """

    name = "workflow"
    version = "0.1.0"
    phase = "plan"
    description = "Generate execution plans from clarified intent"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize workflow component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute workflow planning.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - intent: Clarified intent from Phase 0
                - success_criteria: List of success criteria
                - constraints: List of constraints

        Returns:
            Result dictionary with workflow plan
        """
        issue_id = context.get("issue_id", "")
        intent = context.get("intent", "")
        success_criteria = context.get("success_criteria", [])
        constraints = context.get("constraints", [])

        # Generate workflow plan
        plan = self._generate_plan(intent, success_criteria, constraints)

        # Store plan
        harness_root = context.get("harness_root", ".harness")
        self._write_state(harness_root, {"plan": plan, "issue_id": issue_id})

        # Add evidence
        run_id = context.get("run_id", "unknown")
        self._add_evidence(
            harness_root,
            run_id,
            "workflow-plan",
            f"Generated workflow plan with {len(plan.get('steps', []))} steps"
        )

        return {
            "success": True,
            "output": {"plan": plan},
            "evidence": [{"kind": "workflow-plan", "plan": plan}],
            "error": None,
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up workflow component resources.

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

    def _generate_plan(self, intent: str, success_criteria: List[str],
                      constraints: List[str]) -> Dict[str, Any]:
        """Generate a workflow plan from intent.

        Args:
            intent: Clarified intent
            success_criteria: List of success criteria
            constraints: List of constraints

        Returns:
            Workflow plan dictionary
        """
        # For now, return a basic structure
        # In production, this would delegate to a workflow agent
        steps = []

        for i, criterion in enumerate(success_criteria):
            step = {
                "id": f"step-{i+1}",
                "description": f"Implement: {criterion}",
                "acceptance": [criterion],
                "evidence_required": ["test"],
                "dependencies": [f"step-{i}"] if i > 0 else [],
            }
            steps.append(step)

        return {
            "steps": steps,
            "gates": ["plan-audit", "sync-audit"],
            "risks": self._identify_risks(constraints),
        }

    def _identify_risks(self, constraints: List[str]) -> List[str]:
        """Identify potential risks from constraints.

        Args:
            constraints: List of constraints

        Returns:
            List of identified risks
        """
        risks = []

        for constraint in constraints:
            constraint_lower = constraint.lower()
            if any(keyword in constraint_lower for keyword in
                   ["security", "auth", "permission", "credential"]):
                risks.append("Security-sensitive change requires review")

            if any(keyword in constraint_lower for keyword in
                   ["performance", "latency", "scale"]):
                risks.append("Performance impact requires testing")

            if any(keyword in constraint_lower for keyword in
                   ["api", "external", "service"]):
                risks.append("External dependency requires integration test")

        return risks


# Create instance for registry
_component = WorkflowComponent()

# Export for discovery
__all__ = ["WorkflowComponent", "_component"]
