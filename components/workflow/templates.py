"""Workflow templates: Predefined workflows for common issue types.

These templates define the phases, gates, and evidence requirements for different
types of work. Each template can be customized for project-specific needs.

Inspired by LazyCodex's success criteria and MoAI-ADK's multi-phase patterns.
"""

import os
from typing import Any, Dict, List, Optional
from enum import Enum


class WorkflowTemplate(Enum):
    """Available workflow templates."""

    FEATURE = "feature"
    BUG_FIX = "bug-fix"
    SECURITY_FIX = "security-fix"
    CHORE = "chore"
    REFACTORING = "refactoring"
    PERFORMANCE = "performance"
    DEPENDENCY_UPDATE = "dependency-update"


class WorkflowPhase(Enum):
    """Standard workflow phases."""

    INTENT = "intent"
    PLAN = "plan"
    PLAN_AUDIT = "plan-audit"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    SECURITY_REVIEW = "security-review"
    SYNC_AUDIT = "sync-audit"
    RELEASE = "release"


class EvidenceRequirement:
    """Defines evidence requirements for a phase."""

    def __init__(
        self,
        kind: str,
        required: bool = True,
        min_count: int = 1,
        description: str = "",
    ):
        self.kind = kind
        self.required = required
        self.min_count = min_count
        self.description = description


class QualityGate:
    """Defines a quality gate between phases."""

    def __init__(
        self,
        from_phase: str,
        to_phase: str,
        required_evidence: List[EvidenceRequirement],
        auditor: Optional[str] = None,  # "plan", "sync", or custom
        block_on_fail: bool = True,
        description: str = "",
    ):
        self.from_phase = from_phase
        self.to_phase = to_phase
        self.required_evidence = required_evidence
        self.auditor = auditor
        self.block_on_fail = block_on_fail
        self.description = description


class WorkflowDefinition:
    """Complete workflow definition."""

    def __init__(
        self,
        name: str,
        description: str,
        phases: List[str],
        gates: List[QualityGate],
        orchestration_mode: str = "workflow",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.description = description
        self.phases = phases
        self.gates = gates
        self.orchestration_mode = orchestration_mode
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "description": self.description,
            "phases": self.phases,
            "gates": [
                {
                    "from": gate.from_phase,
                    "to": gate.to_phase,
                    "required_evidence": [
                        {
                            "kind": e.kind,
                            "required": e.required,
                            "min_count": e.min_count,
                            "description": e.description,
                        }
                        for e in gate.required_evidence
                    ],
                    "auditor": gate.auditor,
                    "block_on_fail": gate.block_on_fail,
                    "description": gate.description,
                }
                for gate in self.gates
            ],
            "orchestration_mode": self.orchestration_mode,
            "metadata": self.metadata,
        }


# ============================================================================
# Template Definitions
# ============================================================================

def feature_workflow() -> WorkflowDefinition:
    """Standard feature development workflow.

    Phases: Intent → Plan → Plan-Audit → Implement → Test → Review → Sync-Audit

    Suitable for new features requiring planning and full validation.
    """
    return WorkflowDefinition(
        name="feature",
        description="Standard feature development with full validation",
        phases=[
            WorkflowPhase.INTENT.value,
            WorkflowPhase.PLAN.value,
            WorkflowPhase.PLAN_AUDIT.value,
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.REVIEW.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Plan Audit: Ensure plan is complete before implementation
            QualityGate(
                from_phase=WorkflowPhase.PLAN.value,
                to_phase=WorkflowPhase.IMPLEMENT.value,
                required_evidence=[
                    EvidenceRequirement("spec-validation", description="SPEC document validated"),
                    EvidenceRequirement("workflow-plan", description="Implementation plan defined"),
                ],
                auditor="plan",
                description="Plan must be complete and feasible",
            ),
            # Test Gate: Tests must pass before review
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.REVIEW.value,
                required_evidence=[
                    EvidenceRequirement("test", min_count=1, description="Unit tests executed"),
                ],
                description="All tests must pass",
            ),
            # Sync Audit: Final validation before release
            QualityGate(
                from_phase=WorkflowPhase.REVIEW.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("test", description="Test evidence present"),
                    EvidenceRequirement("review", description="Code review completed"),
                ],
                auditor="sync",
                description="Implementation verified against acceptance criteria",
            ),
        ],
        orchestration_mode="workflow",
        metadata={"complexity": "high", "estimated_duration": "days"},
    )


def bug_fix_workflow() -> WorkflowDefinition:
    """Bug fix workflow (simplified).

    Phases: Intent → Plan → Implement → Test → Sync-Audit

    Streamlined for bug fixes, skips plan audit for faster iteration.
    """
    return WorkflowDefinition(
        name="bug-fix",
        description="Streamlined bug fix workflow",
        phases=[
            WorkflowPhase.INTENT.value,
            WorkflowPhase.PLAN.value,
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.REVIEW.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Plan Gate: No plan audit, just evidence
            QualityGate(
                from_phase=WorkflowPhase.PLAN.value,
                to_phase=WorkflowPhase.IMPLEMENT.value,
                required_evidence=[
                    EvidenceRequirement("workflow-plan", description="Fix approach documented"),
                ],
                auditor=None,  # No auditor for speed
                description="Fix approach documented",
            ),
            # Test Gate: Reproduction + fix validation
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.REVIEW.value,
                required_evidence=[
                    EvidenceRequirement("test", description="Fix validated with tests"),
                    EvidenceRequirement("reproduction", required=False, description="Bug reproduction steps"),
                ],
                description="Fix validated",
            ),
            # Sync Audit: Ensure fix doesn't introduce regressions
            QualityGate(
                from_phase=WorkflowPhase.REVIEW.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("test", description="Regression tests pass"),
                ],
                auditor="sync",
                description="No regressions introduced",
            ),
        ],
        orchestration_mode="team",  # Faster than full workflow
        metadata={"complexity": "medium", "estimated_duration": "hours"},
    )


def security_fix_workflow() -> WorkflowDefinition:
    """Security fix workflow (strict).

    Phases: Intent → Plan → Plan-Audit → Implement → Test → Security-Review → Sync-Audit

    Enhanced validation for security fixes with mandatory security review.
    """
    return WorkflowDefinition(
        name="security-fix",
        description="Strict security fix workflow with mandatory security review",
        phases=[
            WorkflowPhase.INTENT.value,
            WorkflowPhase.PLAN.value,
            WorkflowPhase.PLAN_AUDIT.value,
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.SECURITY_REVIEW.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Plan Audit: Security fixes need thorough planning
            QualityGate(
                from_phase=WorkflowPhase.PLAN.value,
                to_phase=WorkflowPhase.IMPLEMENT.value,
                required_evidence=[
                    EvidenceRequirement("spec-validation", description="Security requirements validated"),
                    EvidenceRequirement("workflow-plan", description="Security fix plan"),
                ],
                auditor="plan",
                description="Security fix must be thoroughly planned",
            ),
            # Test Gate: Security tests required
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.SECURITY_REVIEW.value,
                required_evidence=[
                    EvidenceRequirement("test", description="Security-specific tests executed"),
                    EvidenceRequirement("integration-test", description="Integration tests pass"),
                ],
                description="Security tests must pass",
            ),
            # Security Review Gate: Mandatory security audit
            QualityGate(
                from_phase=WorkflowPhase.SECURITY_REVIEW.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("security", description="Security review completed"),
                    EvidenceRequirement("audit-report", description="Security audit report"),
                ],
                auditor="sync",  # Double audit for security
                description="Security review must pass",
            ),
        ],
        orchestration_mode="workflow",
        metadata={"complexity": "high", "estimated_duration": "days", "security_sensitive": True},
    )


def chore_workflow() -> WorkflowDefinition:
    """Chore workflow (minimal).

    Phases: Implement → Test

    Minimal workflow for small tasks, documentation updates, etc.
    """
    return WorkflowDefinition(
        name="chore",
        description="Minimal workflow for small tasks",
        phases=[
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
        ],
        gates=[
            # Minimal test gate
            QualityGate(
                from_phase=WorkflowPhase.IMPLEMENT.value,
                to_phase=WorkflowPhase.TEST.value,
                required_evidence=[
                    EvidenceRequirement("test", required=False, description="Tests if applicable"),
                ],
                auditor=None,
                description="Basic validation only",
            ),
        ],
        orchestration_mode="single",  # Single agent, fastest
        metadata={"complexity": "low", "estimated_duration": "minutes"},
    )


def refactoring_workflow() -> WorkflowDefinition:
    """Refactoring workflow (performance-focused).

    Phases: Intent → Plan → Implement → Test → Performance-Check → Sync-Audit

    Focuses on maintaining behavior while improving code structure.
    """
    return WorkflowDefinition(
        name="refactoring",
        description="Refactoring workflow with performance validation",
        phases=[
            WorkflowPhase.INTENT.value,
            WorkflowPhase.PLAN.value,
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Plan Gate: Refactoring needs clear scope
            QualityGate(
                from_phase=WorkflowPhase.PLAN.value,
                to_phase=WorkflowPhase.IMPLEMENT.value,
                required_evidence=[
                    EvidenceRequirement("workflow-plan", description="Refactoring scope defined"),
                ],
                auditor=None,
                description="Refactoring scope must be clear",
            ),
            # Test Gate: Behavior preservation critical
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("test", description="All existing tests pass"),
                    EvidenceRequirement("integration-test", description="Integration tests pass"),
                ],
                auditor=None,
                description="Behavior must be preserved",
            ),
            # Sync Audit: Verify no regressions
            QualityGate(
                from_phase=WorkflowPhase.SYNC_AUDIT.value,
                to_phase=WorkflowPhase.RELEASE.value,
                required_evidence=[
                    EvidenceRequirement("metric-capture", required=False, description="Performance metrics captured"),
                    EvidenceRequirement("test", description="No regressions"),
                ],
                auditor="sync",
                description="Refactoring verified",
            ),
        ],
        orchestration_mode="team",
        metadata={"complexity": "medium", "estimated_duration": "hours"},
    )


def performance_workflow() -> WorkflowDefinition:
    """Performance optimization workflow.

    Phases: Intent → Plan → Implement → Benchmark → Test → Sync-Audit

    Focuses on measurable performance improvements.
    """
    return WorkflowDefinition(
        name="performance",
        description="Performance optimization with metric validation",
        phases=[
            WorkflowPhase.INTENT.value,
            WorkflowPhase.PLAN.value,
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Plan Gate: Performance targets defined
            QualityGate(
                from_phase=WorkflowPhase.PLAN.value,
                to_phase=WorkflowPhase.IMPLEMENT.value,
                required_evidence=[
                    EvidenceRequirement("workflow-plan", description="Performance targets defined"),
                ],
                auditor=None,
                description="Performance targets must be defined",
            ),
            # Test Gate: Metrics required
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("test", description="Performance tests executed"),
                    EvidenceRequirement("metric-capture", description="Performance metrics captured"),
                ],
                auditor=None,
                description="Performance metrics must show improvement",
            ),
            # Sync Audit: Verify improvement
            QualityGate(
                from_phase=WorkflowPhase.SYNC_AUDIT.value,
                to_phase=WorkflowPhase.RELEASE.value,
                required_evidence=[
                    EvidenceRequirement("metric-capture", description="Improvement verified"),
                ],
                auditor="sync",
                description="Performance improvement verified",
            ),
        ],
        orchestration_mode="team",
        metadata={"complexity": "medium", "estimated_duration": "hours"},
    )


def dependency_update_workflow() -> WorkflowDefinition:
    """Dependency update workflow.

    Phases: Implement → Test → Security-Check → Sync-Audit

    For updating third-party dependencies.
    """
    return WorkflowDefinition(
        name="dependency-update",
        description="Dependency update with security and compatibility checks",
        phases=[
            WorkflowPhase.IMPLEMENT.value,
            WorkflowPhase.TEST.value,
            WorkflowPhase.SECURITY_REVIEW.value,
            WorkflowPhase.SYNC_AUDIT.value,
        ],
        gates=[
            # Test Gate: All tests must pass
            QualityGate(
                from_phase=WorkflowPhase.TEST.value,
                to_phase=WorkflowPhase.SECURITY_REVIEW.value,
                required_evidence=[
                    EvidenceRequirement("test", description="All tests pass with new dependencies"),
                ],
                auditor=None,
                description="Compatibility verified",
            ),
            # Security Check: No new vulnerabilities
            QualityGate(
                from_phase=WorkflowPhase.SECURITY_REVIEW.value,
                to_phase=WorkflowPhase.SYNC_AUDIT.value,
                required_evidence=[
                    EvidenceRequirement("security", description="No new vulnerabilities introduced"),
                    EvidenceRequirement("audit-report", required=False, description="Dependency audit report"),
                ],
                auditor=None,
                description="Security verified",
            ),
            # Sync Audit: Final validation
            QualityGate(
                from_phase=WorkflowPhase.SYNC_AUDIT.value,
                to_phase=WorkflowPhase.RELEASE.value,
                required_evidence=[
                    EvidenceRequirement("integration-test", description="Integration tests pass"),
                ],
                auditor="sync",
                description="Update validated",
            ),
        ],
        orchestration_mode="team",
        metadata={"complexity": "medium", "estimated_duration": "hours"},
    )


# ============================================================================
# Template Registry
# ============================================================================

WORKFLOW_TEMPLATES: Dict[str, WorkflowDefinition] = {
    "feature": feature_workflow(),
    "bug-fix": bug_fix_workflow(),
    "security-fix": security_fix_workflow(),
    "chore": chore_workflow(),
    "refactoring": refactoring_workflow(),
    "performance": performance_workflow(),
    "dependency-update": dependency_update_workflow(),
}


def get_template(name: str) -> Optional[WorkflowDefinition]:
    """Get a workflow template by name.

    Args:
        name: Template name (feature, bug-fix, etc.)

    Returns:
        WorkflowDefinition or None if not found
    """
    return WORKFLOW_TEMPLATES.get(name)


def list_templates() -> List[str]:
    """List available workflow templates."""
    return list(WORKFLOW_TEMPLATES.keys())


def save_template_to_file(template: WorkflowDefinition, filepath: str) -> None:
    """Save a workflow template to a JSON file."""
    import json

    with open(filepath, "w") as f:
        json.dump(template.to_dict(), f, indent=2)


def load_template_from_file(filepath: str) -> Optional[WorkflowDefinition]:
    """Load a workflow template from a JSON file."""
    import json

    if not os.path.exists(filepath):
        return None

    with open(filepath, "r") as f:
        data = json.load(f)

    # Reconstruct gates
    gates = []
    for gate_data in data.get("gates", []):
        evidence_reqs = [
            EvidenceRequirement(
                kind=e["kind"],
                required=e.get("required", True),
                min_count=e.get("min_count", 1),
                description=e.get("description", ""),
            )
            for e in gate_data.get("required_evidence", [])
        ]
        gates.append(
            QualityGate(
                from_phase=gate_data["from"],
                to_phase=gate_data["to"],
                required_evidence=evidence_reqs,
                auditor=gate_data.get("auditor"),
                block_on_fail=gate_data.get("block_on_fail", True),
                description=gate_data.get("description", ""),
            )
        )

    return WorkflowDefinition(
        name=data["name"],
        description=data["description"],
        phases=data["phases"],
        gates=gates,
        orchestration_mode=data.get("orchestration_mode", "workflow"),
        metadata=data.get("metadata", {}),
    )
