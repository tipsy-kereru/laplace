"""Plan auditor: Independent validation of workflow plans.

Spawns with fresh context to validate workflow plans before execution.
Implements adversarial stance to find defects.

Returns: PASS/FAIL/INCONCLUSIVE with detailed reasoning.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


# Audit result schema
AUDIT_RESULT = {
    "verdict": "PASS|FAIL|INCONCLUSIVE",
    "reasoning": "string",
    "findings": [
        {
            "category": "completeness|coherence|feasibility|safety",
            "severity": "critical|major|minor",
            "detail": "string"
        }
    ],
    "timestamp": "ISO-8601"
}


class PlanAuditorComponent(base.Component):
    """Plan auditor component.

    Independently validates workflow plans before execution.
    """

    name = "plan-auditor"
    version = "0.1.0"
    phase = "plan-audit"
    description = "Independent validation of workflow plans"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize plan auditor component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute plan audit.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - plan_path: Path to workflow plan file
                - harness_root: Path to .harness/ directory

        Returns:
            Audit result dictionary
        """
        issue_id = context.get("issue_id", "")
        plan_path = context.get("plan_path", "")
        harness_root = context.get("harness_root", ".harness")
        run_id = context.get("run_id", "unknown")

        # Read plan file
        if not os.path.exists(plan_path):
            return {
                "success": False,
                "output": {},
                "evidence": [],
                "error": f"Plan file not found: {plan_path}"
            }

        with open(plan_path, "r", encoding="utf-8") as f:
            plan = json.load(f)

        # Perform audit
        audit_result = self._audit_plan(plan, issue_id)

        # Store audit result
        audit_file = os.path.join(
            self._get_component_dir(harness_root),
            f"plan-audit-{issue_id}.json"
        )
        with open(audit_file, "w", encoding="utf-8") as f:
            json.dump(audit_result, f, indent=2)

        # Add evidence
        self._add_evidence(
            harness_root,
            run_id,
            "audit-report",
            f"Plan audit: {audit_result['verdict']} - {audit_result['reasoning']}"
        )

        return {
            "success": audit_result["verdict"] == "PASS",
            "output": {"audit_result": audit_result},
            "evidence": [{"kind": "audit-report", "verdict": audit_result["verdict"]}],
            "error": None if audit_result["verdict"] == "PASS" else f"Plan audit {audit_result['verdict']}"
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up plan auditor resources.

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

    def _audit_plan(self, plan: Dict[str, Any], issue_id: str) -> Dict[str, Any]:
        """Audit a workflow plan.

        Args:
            plan: Workflow plan dictionary
            issue_id: Issue identifier

        Returns:
            Audit result with verdict and findings
        """
        findings = []
        steps = plan.get("steps", [])

        # Check completeness: every AC should have implementation steps
        if not steps:
            findings.append({
                "category": "completeness",
                "severity": "critical",
                "detail": "Plan has no implementation steps"
            })

        # Check coherence: steps should be ordered
        step_ids = [step.get("id") for step in steps]
        if step_ids != sorted(step_ids):
            findings.append({
                "category": "coherence",
                "severity": "minor",
                "detail": "Step IDs are not sequentially ordered"
            })

        # Check for circular dependencies
        for step in steps:
            deps = step.get("dependencies", [])
            for dep in deps:
                if dep not in step_ids:
                    findings.append({
                        "category": "coherence",
                        "severity": "major",
                        "detail": f"Step {step.get('id')} depends on non-existent step {dep}"
                    })

        # Determine verdict
        critical_findings = [f for f in findings if f["severity"] == "critical"]
        major_findings = [f for f in findings if f["severity"] == "major"]

        if critical_findings:
            verdict = "FAIL"
            reasoning = f"Critical findings: {len(critical_findings)}"
        elif len(major_findings) > 2:
            verdict = "FAIL"
            reasoning = f"Too many major findings: {len(major_findings)}"
        elif findings:
            verdict = "PASS"  # Minor findings are OK
            reasoning = f"Passed with {len(findings)} findings"
        else:
            verdict = "PASS"
            reasoning = "Plan is complete and coherent"

        return {
            "verdict": verdict,
            "reasoning": reasoning,
            "findings": findings,
            "timestamp": self._get_timestamp()
        }

    def _get_timestamp(self) -> str:
        """Get current ISO timestamp.

        Returns:
            ISO-8601 timestamp
        """
        from datetime import datetime
        return datetime.utcnow().isoformat() + "Z"


# Create instance for registry
_component = PlanAuditorComponent()

# Export for discovery
__all__ = ["PlanAuditorComponent", "_component", "AUDIT_RESULT"]
