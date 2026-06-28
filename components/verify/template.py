"""Auditor template: Base class for custom auditors.

This template provides a foundation for project-specific auditors.
Custom auditors inherit from AuditorBase and implement the audit() method.

Example:
    class MyAuditor(AuditorBase):
        def audit(self, context: Dict[str, Any]) -> AuditResult:
            # Your audit logic here
            return AuditResult(
                verdict="PASS",
                reasoning="All checks passed",
                findings=[]
            )
"""

import abc
import os
from typing import Any, Dict, List, Optional


class AuditFinding:
    """Represents a single audit finding."""

    def __init__(
        self,
        severity: str,  # "critical", "high", "medium", "low", "info"
        category: str,  # "security", "correctness", "performance", etc.
        location: Optional[str],  # File path or component name
        description: str,
        recommendation: Optional[str] = None,
    ):
        self.severity = severity
        self.category = category
        self.location = location
        self.description = description
        self.recommendation = recommendation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "location": self.location,
            "description": self.description,
            "recommendation": self.recommendation,
        }


class AuditResult:
    """Represents the result of an audit."""

    def __init__(
        self,
        verdict: str,  # "PASS", "FAIL", "INCONCLUSIVE"
        reasoning: str,
        findings: List[AuditFinding],
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.verdict = verdict
        self.reasoning = reasoning
        self.findings = findings
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasoning": self.reasoning,
            "findings": [f.to_dict() for f in self.findings],
            "metadata": self.metadata,
        }

    def is_pass(self) -> bool:
        return self.verdict == "PASS"

    def is_fail(self) -> bool:
        return self.verdict == "FAIL"

    def is_inconclusive(self) -> bool:
        return self.verdict == "INCONCLUSIVE"


class AuditorBase(abc.ABC):
    """Base class for custom auditors.

    Custom auditors should:
    1. Inherit from AuditorBase
    2. Implement the audit() method
    3. Return an AuditResult

    The auditor will be spawned with fresh context to ensure independence.
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description

    @abc.abstractmethod
    def audit(self, context: Dict[str, Any]) -> AuditResult:
        """Perform the audit and return a result.

        Args:
            context: Audit context containing:
                - issue_id: Issue identifier
                - run_id: Run identifier (if applicable)
                - harness_root: Path to .harness/ directory
                - Additional keys based on audit type

        Returns:
            AuditResult with verdict, reasoning, and findings
        """
        pass

    def initialize(self, harness_root: str) -> None:
        """Initialize the auditor (optional override).

        Called before audit() for any setup needed.
        """
        pass

    def cleanup(self) -> None:
        """Cleanup resources (optional override)."""
        pass

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the audit and return a serialized result.

        This is the main entry point called by the harness.
        """
        try:
            self.initialize(context.get("harness_root", ""))
            result = self.audit(context)
            self.cleanup()
            return {"success": True, "audit_result": result.to_dict()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "verdict": "INCONCLUSIVE",
            }


class SeverityThreshold:
    """Severity threshold configuration for auditors."""

    # Default: any critical or high finding causes FAIL
    DEFAULT_FAIL_SEVERITIES = {"critical", "high"}

    # Lenient: only critical causes FAIL
    LENIENT_FAIL_SEVERITIES = {"critical"}

    # Strict: medium and above causes FAIL
    STRICT_FAIL_SEVERITIES = {"critical", "high", "medium"}

    @staticmethod
    def should_fail(findings: List[AuditFinding], fail_severities: Optional[set] = None) -> bool:
        """Determine if findings should cause a FAIL verdict.

        Args:
            findings: List of audit findings
            fail_severities: Severities that cause FAIL (default: critical, high)

        Returns:
            True if any finding has a severity that causes FAIL
        """
        if fail_severities is None:
            fail_severities = SeverityThreshold.DEFAULT_FAIL_SEVERITIES
        return any(f.severity in fail_severities for f in findings)


def get_verdict_from_findings(
    findings: List[AuditFinding],
    fail_severities: Optional[set] = None,
    inconclusive_on_empty: bool = False,
) -> str:
    """Determine verdict based on findings.

    Args:
        findings: List of audit findings
        fail_severities: Severities that cause FAIL
        inconclusive_on_empty: Return INCONCLUSIVE if no findings (default: PASS)

    Returns:
        "PASS", "FAIL", or "INCONCLUSIVE"
    """
    if not findings:
        return "INCONCLUSIVE" if inconclusive_on_empty else "PASS"

    if SeverityThreshold.should_fail(findings, fail_severities):
        return "FAIL"
    return "PASS"
