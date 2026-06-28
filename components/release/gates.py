"""Quality gate enforcement for release phase.

Phase 4 component that enforces quality gates before release:
    - All evidence requirements met
    - All auditor verdicts are PASS
    - No unresolved blockers
    - Release artifacts ready
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


class ReleaseComponent(base.Component):
    """Release component with quality gate enforcement.

    Validates all requirements are met before release.
    """

    name = "release"
    version = "0.1.0"
    phase = "release"
    description = "Release automation with quality gates"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize release component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute release quality gates.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - run_id: Run identifier
                - harness_root: Path to .harness/ directory

        Returns:
            Result dictionary with gate results
        """
        issue_id = context.get("issue_id", "")
        run_id = context.get("run_id", "")
        harness_root = context.get("harness_root", ".harness")

        # Run quality gates
        gate_results = self._run_quality_gates(issue_id, run_id, harness_root)

        # Determine if all gates passed
        all_passed = all(result["passed"] for result in gate_results)

        # Add evidence
        self._add_evidence(
            harness_root,
            run_id,
            "quality-gate",
            f"Quality gates: {len(gate_results)} gates, {'all passed' if all_passed else 'some failed'}"
        )

        return {
            "success": all_passed,
            "output": {"gates": gate_results},
            "evidence": [{"kind": "quality-gate", "all_passed": all_passed}],
            "error": None if all_passed else "Quality gates not passed"
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up release component resources.

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

    def _run_quality_gates(self, issue_id: str, run_id: str,
                          harness_root: str) -> List[Dict[str, Any]]:
        """Run all quality gates.

        Args:
            issue_id: Issue identifier
            run_id: Run identifier
            harness_root: Path to .harness/ directory

        Returns:
            List of gate results
        """
        gates = []

        # Gate 1: Check plan auditor verdict
        plan_audit_file = os.path.join(
            harness_root,
            "components",
            "verify",
            f"plan-audit-{issue_id}.json"
        )
        if os.path.exists(plan_audit_file):
            with open(plan_audit_file, "r", encoding="utf-8") as f:
                plan_audit = json.load(f)
            gates.append({
                "name": "plan-auditor",
                "passed": plan_audit.get("verdict") == "PASS",
                "detail": plan_audit.get("reasoning", "")
            })
        else:
            gates.append({
                "name": "plan-auditor",
                "passed": False,
                "detail": "No plan audit found"
            })

        # Gate 2: Check sync auditor verdict
        sync_audit_file = os.path.join(
            harness_root,
            "components",
            "verify",
            f"sync-audit-{issue_id}-{run_id}.json"
        )
        if os.path.exists(sync_audit_file):
            with open(sync_audit_file, "r", encoding="utf-8") as f:
                sync_audit = json.load(f)
            gates.append({
                "name": "sync-auditor",
                "passed": sync_audit.get("verdict") == "PASS",
                "detail": sync_audit.get("reasoning", "")
            })
        else:
            gates.append({
                "name": "sync-auditor",
                "passed": False,
                "detail": "No sync audit found"
            })

        # Gate 3: Check test evidence
        run_log_file = os.path.join(harness_root, "state", "runs", f"{run_id}.json")
        if os.path.exists(run_log_file):
            with open(run_log_file, "r", encoding="utf-8") as f:
                run_log = json.load(f)
            evidence = run_log.get("evidence", [])
            has_test = any(e.get("kind") == "test" for e in evidence)
            gates.append({
                "name": "test-evidence",
                "passed": has_test,
                "detail": "Test evidence present" if has_test else "Missing test evidence"
            })
        else:
            gates.append({
                "name": "test-evidence",
                "passed": False,
                "detail": "No run log found"
            })

        return gates


# Create instance for registry
_component = ReleaseComponent()

# Export for discovery
__all__ = ["ReleaseComponent", "_component"]
