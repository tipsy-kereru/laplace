"""Intent capturer: Extract structured intent from PRDs.

Phase 0 component that captures:
    - User intent in structured format
    - Success criteria with measurable outcomes
    - Constraints and assumptions
    - Required evidence kinds
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


class IntentComponent(base.Component):
    """Intent component for requirement clarification.

    Captures structured intent from PRDs.
    """

    name = "intent"
    version = "0.1.0"
    phase = "intent"
    description = "Capture structured intent from PRDs"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize intent component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute intent capture.

        Args:
            context: Execution context including:
                - prd_path: Path to PRD file
                - issue_id: Issue identifier
                - harness_root: Path to .harness/ directory

        Returns:
            Result dictionary with captured intent
        """
        prd_path = context.get("prd_path", "")
        issue_id = context.get("issue_id", "")
        harness_root = context.get("harness_root", ".harness")
        run_id = context.get("run_id", "unknown")

        # Read PRD file
        if not os.path.exists(prd_path):
            return {
                "success": False,
                "output": {},
                "evidence": [],
                "error": f"PRD file not found: {prd_path}"
            }

        with open(prd_path, "r", encoding="utf-8") as f:
            prd_content = f.read()

        # Extract structured intent
        intent = self._extract_intent(prd_content)

        # Store intent
        self._write_state(harness_root, {
            "intent": intent,
            "issue_id": issue_id,
            "prd_path": prd_path
        })

        # Add evidence
        self._add_evidence(
            harness_root,
            run_id,
            "spec-validation",
            f"Captured intent with {len(intent.get('success_criteria', []))} criteria"
        )

        return {
            "success": True,
            "output": {"intent": intent},
            "evidence": [{"kind": "spec-validation", "criteria_count": len(intent.get("success_criteria", []))}],
            "error": None
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up intent component resources.

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

    def _extract_intent(self, prd_content: str) -> Dict[str, Any]:
        """Extract structured intent from PRD content.

        Args:
            prd_content: PRD file content

        Returns:
            Structured intent dictionary
        """
        # For now, return a basic structure
        # In production, this would delegate to an intent agent
        # or parse structured PRD formats (YAML, JSON, etc.)

        lines = prd_content.split("\n")

        # Extract title (first # header)
        title = "Untitled"
        for line in lines:
            if line.strip().startswith("# "):
                title = line.strip()[2:].strip()
                break

        # Look for numbered lists as criteria
        criteria = []
        for line in lines:
            line = line.strip()
            if line.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
                criteria.append(line[3:].strip())

        return {
            "title": title,
            "intent": f"Implement: {title}",
            "success_criteria": criteria[:5] if criteria else [
                "Feature implemented as specified",
                "Tests pass",
                "Documentation updated"
            ],
            "constraints": [
                "Follow existing code patterns",
                "Maintain test coverage"
            ],
            "assumptions": [
                "Dependencies are available",
                "Environment is configured"
            ],
            "required_evidence": ["test", "review"]
        }


# Create instance for registry
_component = IntentComponent()

# Export for discovery
__all__ = ["IntentComponent", "_component"]
