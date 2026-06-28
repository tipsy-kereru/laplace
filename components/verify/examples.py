"""Example custom auditors demonstrating the AuditorBase pattern.

These examples show how to create project-specific auditors for:
- Security checks
- Performance thresholds
- Dependency validation
- Code quality gates
"""

import json
import os
from typing import Any, Dict, List, Optional

from .template import AuditorBase, AuditResult, AuditFinding, get_verdict_from_findings


class SecurityAuditor(AuditorBase):
    """Example: Security-focused auditor for sensitive changes.

    Checks for:
    - Hardcoded secrets/credentials
    - Insecure API patterns
    - Authentication/authorization changes
    """

    def __init__(self):
        super().__init__(
            name="security-auditor",
            description="Audits code for security vulnerabilities and patterns",
        )

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []
        harness_root = context.get("harness_root", "")
        issue_id = context.get("issue_id", "")

        # Check for secrets in code
        secret_patterns = ["password", "api_key", "secret", "token", "credential"]
        findings.extend(self._check_for_secrets(harness_root, secret_patterns))

        # Check for insecure HTTP usage
        findings.extend(self._check_insecure_http(harness_root))

        # Determine verdict
        verdict = get_verdict_from_findings(findings, {"critical", "high"})

        return AuditResult(
            verdict=verdict,
            reasoning=f"Security audit found {len(findings)} findings",
            findings=findings,
            metadata={"issue_id": issue_id},
        )

    def _check_for_secrets(self, root: str, patterns: List[str]) -> List[AuditFinding]:
        """Check for hardcoded secrets in code."""
        findings = []
        # Simplified example - in production, scan actual diff files
        return findings

    def _check_insecure_http(self, root: str) -> List[AuditFinding]:
        """Check for insecure HTTP (non-HTTPS) usage."""
        findings = []
        # Simplified example
        return findings


class PerformanceAuditor(AuditorBase):
    """Example: Performance-focused auditor for implementation checks.

    Checks for:
    - Database query efficiency
    - Algorithmic complexity concerns
    - Resource management
    """

    def __init__(self):
        super().__init__(
            name="performance-auditor",
            description="Audits code for performance and efficiency concerns",
        )

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []
        harness_root = context.get("harness_root", "")

        # Check for N+1 query patterns
        findings.extend(self._check_n_plus_one_queries(harness_root))

        # Check for missing caching
        findings.extend(self._check_caching_opportunities(harness_root))

        # Performance audits rarely block (use lenient threshold)
        verdict = get_verdict_from_findings(findings, {"critical"})

        return AuditResult(
            verdict=verdict,
            reasoning=f"Performance audit found {len(findings)} findings",
            findings=findings,
        )

    def _check_n_plus_one_queries(self, root: str) -> List[AuditFinding]:
        """Check for potential N+1 query patterns."""
        return []

    def _check_caching_opportunities(self, root: str) -> List[AuditFinding]:
        """Check for missed caching opportunities."""
        return []


class DependencyAuditor(AuditorBase):
    """Example: Dependency-focused auditor for third-party packages.

    Checks for:
    - Known vulnerabilities (CVEs)
    - Outdated packages
    - License compatibility
    """

    def __init__(self):
        super().__init__(
            name="dependency-auditor",
            description="Audits dependencies for security and compatibility",
        )

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []

        # Check package.json, requirements.txt, etc.
        harness_root = context.get("harness_root", "")

        # Check for known vulnerabilities
        findings.extend(self._check_vulnerabilities(harness_root))

        # Check license compatibility
        findings.extend(self._check_licenses(harness_root))

        verdict = get_verdict_from_findings(findings, {"critical", "high"})

        return AuditResult(
            verdict=verdict,
            reasoning=f"Dependency audit found {len(findings)} findings",
            findings=findings,
        )

    def _check_vulnerabilities(self, root: str) -> List[AuditFinding]:
        """Check for known vulnerabilities in dependencies."""
        return []

    def _check_licenses(self, root: str) -> List[AuditFinding]:
        """Check for incompatible licenses."""
        return []


class ComplianceAuditor(AuditorBase):
    """Example: Compliance-focused auditor for regulatory requirements.

    Checks for:
    - Data privacy (GDPR, CCPA)
    - Accessibility (WCAG)
    - Logging and monitoring
    """

    def __init__(self):
        super().__init__(
            name="compliance-auditor",
            description="Audits for regulatory compliance requirements",
        )

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []

        harness_root = context.get("harness_root", "")

        # Check for PII handling
        findings.extend(self._check_pii_handling(harness_root))

        # Check for accessibility compliance
        findings.extend(self._check_accessibility(harness_root))

        verdict = get_verdict_from_findings(findings, {"critical", "high"})

        return AuditResult(
            verdict=verdict,
            reasoning=f"Compliance audit found {len(findings)} findings",
            findings=findings,
        )

    def _check_pii_handling(self, root: str) -> List[AuditFinding]:
        """Check for proper PII (Personally Identifiable Information) handling."""
        return []

    def _check_accessibility(self, root: str) -> List[AuditFinding]:
        """Check for accessibility compliance."""
        return []


class TestCoverageAuditor(AuditorBase):
    """Example: Test coverage auditor for quality gates.

    Checks for:
    - Minimum test coverage percentage
    - Critical path coverage
    - Test quality indicators
    """

    def __init__(self, min_coverage: float = 80.0):
        super().__init__(
            name="test-coverage-auditor",
            description="Audits test coverage and quality",
        )
        self.min_coverage = min_coverage

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []
        run_id = context.get("run_id")
        harness_root = context.get("harness_root", "")

        # Load run log to check test evidence
        run_log_path = os.path.join(
            harness_root, ".harness", "state", "runs", f"{run_id}.json"
        )

        if not os.path.exists(run_log_path):
            return AuditResult(
                verdict="INCONCLUSIVE",
                reasoning="Run log not found - cannot verify test coverage",
                findings=[],
            )

        with open(run_log_path, "r") as f:
            run_log = json.load(f)

        # Check for test evidence
        test_evidence = [e for e in run_log.get("evidence", []) if e.get("kind") == "test"]

        if not test_evidence:
            findings.append(
                AuditFinding(
                    severity="high",
                    category="coverage",
                    location=None,
                    description="No test evidence found in run log",
                    recommendation="Add test evidence before completing run",
                )
            )

        # Check coverage threshold (would parse coverage reports in production)
        # findings.extend(self._check_coverage_threshold(harness_root))

        verdict = get_verdict_from_findings(findings, {"high", "critical"})

        return AuditResult(
            verdict=verdict,
            reasoning=f"Test coverage audit: {len(findings)} findings, {len(test_evidence)} test entries",
            findings=findings,
            metadata={"min_coverage": self.min_coverage, "test_count": len(test_evidence)},
        )


# Registry of example auditors
EXAMPLE_AUDITORS = {
    "security": SecurityAuditor,
    "performance": PerformanceAuditor,
    "dependency": DependencyAuditor,
    "compliance": ComplianceAuditor,
    "test-coverage": TestCoverageAuditor,
}


def create_auditor(auditor_type: str, **kwargs) -> Optional[AuditorBase]:
    """Factory function to create auditor instances.

    Args:
        auditor_type: Type of auditor to create
        **kwargs: Additional arguments to pass to auditor constructor

    Returns:
        Auditor instance or None if type not found
    """
    auditor_class = EXAMPLE_AUDITORS.get(auditor_type)
    if auditor_class:
        return auditor_class(**kwargs)
    return None
