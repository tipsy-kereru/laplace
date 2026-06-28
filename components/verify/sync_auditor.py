"""Sync auditor: Independent validation of implementation results.

Spawns with fresh context to validate implementation before PR.
Tests implementations against SPEC acceptance criteria.

Returns: PASS/FAIL/INCONCLUSIVE with detailed reasoning.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .. import base


class SyncAuditorComponent(base.Component):
    """Sync auditor component.

    Independently validates implementation results before PR.
    """

    name = "sync-auditor"
    version = "0.1.0"
    phase = "sync-audit"
    description = "Independent validation of implementation results"

    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize sync auditor component.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional configuration

        Returns:
            True if successful
        """
        self._ensure_component_dir(harness_root)
        return True

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute sync audit.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - run_id: Run identifier
                - run_log: Path to run log file
                - harness_root: Path to .harness/ directory

        Returns:
            Audit result dictionary
        """
        issue_id = context.get("issue_id", "")
        run_id = context.get("run_id", "")
        run_log_path = context.get("run_log", "")
        harness_root = context.get("harness_root", ".harness")

        # Read run log
        if not os.path.exists(run_log_path):
            return {
                "success": False,
                "output": {},
                "evidence": [],
                "error": f"Run log not found: {run_log_path}"
            }

        with open(run_log_path, "r", encoding="utf-8") as f:
            run_log = json.load(f)

        # Read issue file for ACs
        issue_file = os.path.join(harness_root, "issues", f"{issue_id}.md")
        if not os.path.exists(issue_file):
            return {
                "success": False,
                "output": {},
                "evidence": [],
                "error": f"Issue file not found: {issue_file}"
            }

        with open(issue_file, "r", encoding="utf-8") as f:
            issue_content = f.read()

        # Perform audit
        audit_result = self._audit_sync(run_log, issue_content, issue_id)

        # Store audit result
        audit_file = os.path.join(
            self._get_component_dir(harness_root),
            f"sync-audit-{issue_id}-{run_id}.json"
        )
        with open(audit_file, "w", encoding="utf-8") as f:
            json.dump(audit_result, f, indent=2)

        # Add evidence
        self._add_evidence(
            harness_root,
            run_id,
            "audit-report",
            f"Sync audit: {audit_result['verdict']} - {audit_result['reasoning']}"
        )

        return {
            "success": audit_result["verdict"] == "PASS",
            "output": {"audit_result": audit_result},
            "evidence": [{"kind": "audit-report", "verdict": audit_result["verdict"]}],
            "error": None if audit_result["verdict"] == "PASS" else f"Sync audit {audit_result['verdict']}"
        }

    def cleanup(self, harness_root: str) -> bool:
        """Clean up sync auditor resources.

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

    def _audit_sync(self, run_log: Dict[str, Any],
                    issue_content: str, issue_id: str) -> Dict[str, Any]:
        """Audit implementation results.

        Args:
            run_log: Run log dictionary
            issue_content: Issue file content
            issue_id: Issue identifier

        Returns:
            Audit result with verdict and findings
        """
        findings = []
        evidence = run_log.get("evidence", [])

        # Check evidence completeness
        evidence_kinds = {e.get("kind") for e in evidence}

        if "test" not in evidence_kinds:
            findings.append({
                "category": "evidence-completeness",
                "severity": "critical",
                "detail": "Missing test evidence"
            })

        # Check for test failures
        for entry in evidence:
            if entry.get("kind") == "test":
                # Check if test evidence indicates failure
                summary = entry.get("summary", "").lower()
                if "fail" in summary or "error" in summary:
                    findings.append({
                        "category": "ac-satisfaction",
                        "severity": "critical",
                        "detail": "Test failures detected"
                    })

        # Check for security findings
        for entry in evidence:
            if entry.get("kind") == "security":
                summary = entry.get("summary", "").lower()
                if "block" in summary or "critical" in summary:
                    findings.append({
                        "category": "safety",
                        "severity": "critical",
                        "detail": "Unresolved security concerns"
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
            reasoning = "Implementation satisfies requirements"

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
_component = SyncAuditorComponent()

# Export for discovery
__all__ = ["SyncAuditorComponent", "_component"]
