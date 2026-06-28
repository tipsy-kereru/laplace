"""Workflow auto-generator: Generate execution plans from SPEC documents.

This component extends the workflow planner to automatically generate
executable workflow plans from SPEC documents.

Generated workflow includes:
- Step-by-step implementation plan
- Evidence collection points
- Quality gate definitions
- Risk identification

Inspired by LazyCodex's workflow generation and MoAI-ADK's planning phase.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime


class WorkflowStep:
    """Single workflow step."""

    def __init__(
        self,
        step_id: str,
        name: str,
        description: str,
        phase: str = "implement",
        dependencies: Optional[List[str]] = None,
        evidence_required: Optional[List[str]] = None,
        agent_type: str = "dev",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.step_id = step_id
        self.name = name
        self.description = description
        self.phase = phase
        self.dependencies = dependencies or []
        self.evidence_required = evidence_required or []
        self.agent_type = agent_type
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "step_id": self.step_id,
            "name": self.name,
            "description": self.description,
            "phase": self.phase,
            "dependencies": self.dependencies,
            "evidence_required": self.evidence_required,
            "agent_type": self.agent_type,
            "metadata": self.metadata,
        }


class QualityGate:
    """Quality gate between workflow steps."""

    def __init__(
        self,
        gate_id: str,
        from_step: str,
        to_step: str,
        required_evidence: List[str],
        auditor: Optional[str] = None,
        description: str = "",
    ):
        self.gate_id = gate_id
        self.from_step = from_step
        self.to_step = to_step
        self.required_evidence = required_evidence
        self.auditor = auditor
        self.description = description

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "gate_id": self.gate_id,
            "from_step": self.from_step,
            "to_step": self.to_step,
            "required_evidence": self.required_evidence,
            "auditor": self.auditor,
            "description": self.description,
        }


class WorkflowPlan:
    """Complete workflow plan."""

    def __init__(
        self,
        plan_id: str,
        spec_id: str,
        title: str,
        description: str = "",
    ):
        self.plan_id = plan_id
        self.spec_id = spec_id
        self.title = title
        self.description = description
        self.steps: List[WorkflowStep] = []
        self.gates: List[QualityGate] = []
        self.metadata: Dict[str, Any] = {}
        self.created_at = datetime.utcnow().isoformat() + "Z"

    def add_step(self, step: WorkflowStep) -> None:
        """Add a workflow step."""
        self.steps.append(step)

    def add_gate(self, gate: QualityGate) -> None:
        """Add a quality gate."""
        self.gates.append(gate)

    def get_execution_order(self) -> List[WorkflowStep]:
        """Get steps in dependency order."""
        # Topological sort based on dependencies
        ordered = []
        visited = set()

        def visit(step: WorkflowStep):
            if step.step_id in visited:
                return
            visited.add(step.step_id)

            # Visit dependencies first
            for dep_id in step.dependencies:
                dep_step = self._get_step(dep_id)
                if dep_step:
                    visit(dep_step)

            ordered.append(step)

        for step in self.steps:
            visit(step)

        return ordered

    def _get_step(self, step_id: str) -> Optional[WorkflowStep]:
        """Get step by ID."""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "plan_id": self.plan_id,
            "spec_id": self.spec_id,
            "title": self.title,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "gates": [g.to_dict() for g in self.gates],
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class WorkflowAutoGenerator:
    """Generate workflow plans from SPEC documents."""

    def __init__(self, harness_root: str):
        self.harness_root = harness_root
        self.spec_dir = os.path.join(harness_root, ".harness", "specs")
        self.workflow_dir = os.path.join(harness_root, ".harness", "workflows")

    def generate_from_spec(
        self,
        spec_path: str,
        plan_id: Optional[str] = None,
    ) -> Tuple[Optional[WorkflowPlan], str]:
        """Generate workflow plan from SPEC document.

        Args:
            spec_path: Path to SPEC markdown file
            plan_id: Optional plan identifier (auto-generated if not provided)

        Returns:
            (WorkflowPlan, status message) tuple
        """
        # Read SPEC
        try:
            with open(spec_path, "r", encoding="utf-8") as f:
                spec_content = f.read()
        except IOError as e:
            return None, f"Failed to read SPEC: {e}"

        # Parse SPEC
        spec_data = self._parse_spec(spec_content)
        if not spec_data:
            return None, "Failed to parse SPEC"

        # Generate plan_id
        if not plan_id:
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            plan_id = f"PLAN-{timestamp}"

        # Create workflow plan
        plan = WorkflowPlan(
            plan_id=plan_id,
            spec_id=spec_data.get("spec_id", "unknown"),
            title=spec_data.get("title", "Untitled"),
            description=f"Auto-generated workflow for {spec_data.get('title', 'feature')}",
        )

        # Set metadata
        plan.metadata = {
            "spec_path": spec_path,
            "auto_generated": True,
            "source": "workflow-auto-generator",
        }

        # Generate steps from requirements
        step_num = 1
        ac_num = 1

        # Analysis step
        plan.add_step(WorkflowStep(
            step_id=f"step-{step_num}",
            name="Analyze Requirements",
            description="Review SPEC requirements and acceptance criteria",
            phase="analyze",
            agent_type="pm",
            evidence_required=["spec-validation"],
        ))
        step_num += 1

        # Design step
        plan.add_step(WorkflowStep(
            step_id=f"step-{step_num}",
            name="Create Design",
            description="Create technical design and component structure",
            phase="design",
            agent_type="architect",
            dependencies=[f"step-{step_num-1}"],
            evidence_required=["workflow-plan"],
        ))
        step_num += 1

        # Implementation steps (one per AC)
        acceptance_criteria = spec_data.get("acceptance_criteria", [])
        if isinstance(acceptance_criteria, str):
            # Parse AC sections
            ac_sections = self._extract_ac_sections(acceptance_criteria)

            for ac_section in ac_sections:
                ac_title = ac_section.get("title", f"AC-{ac_num}")
                ac_content = ac_section.get("content", "")

                plan.add_step(WorkflowStep(
                    step_id=f"step-{step_num}",
                    name=f"Implement {ac_title}",
                    description=ac_content[:200],
                    phase="implement",
                    agent_type="dev",
                    dependencies=[f"step-{step_num-1}"],
                    evidence_required=["test", "review"],
                    metadata={"ac": ac_title},
                ))
                step_num += 1
                ac_num += 1

        # Testing step
        plan.add_step(WorkflowStep(
            step_id=f"step-{step_num}",
            name="Execute Tests",
            description="Run test suite and capture evidence",
            phase="test",
            agent_type="qa",
            dependencies=[f"step-{step_num-1}"],
            evidence_required=["test", "integration-test"],
        ))
        step_num += 1

        # Review step
        plan.add_step(WorkflowStep(
            step_id=f"step-{step_num}",
            name="Review Implementation",
            description="Code review against acceptance criteria",
            phase="review",
            agent_type="reviewer",
            dependencies=[f"step-{step_num-1}"],
            evidence_required=["review"],
        ))
        step_num += 1

        # Security review step (if needed)
        if spec_data.get("security_sensitive", False):
            plan.add_step(WorkflowStep(
                step_id=f"step-{step_num}",
                name="Security Review",
                description="Security-focused review for vulnerabilities",
                phase="security-review",
                agent_type="security",
                dependencies=[f"step-{step_num-1}"],
                evidence_required=["security"],
            ))
            step_num += 1

        # Add quality gates
        gate_num = 1

        # Plan audit gate
        plan.add_gate(QualityGate(
            gate_id=f"gate-{gate_num}",
            from_step="step-2",
            to_step="step-3",
            required_evidence=["spec-validation", "workflow-plan"],
            auditor="plan",
            description="Plan audit: Verify workflow plan is complete",
        ))
        gate_num += 1

        # Sync audit gate (final)
        final_step = f"step-{len(plan.steps)}"
        plan.add_gate(QualityGate(
            gate_id=f"gate-{gate_num}",
            from_step=final_step,
            to_step="complete",
            required_evidence=["test", "review", "audit-report"],
            auditor="sync",
            description="Sync audit: Verify implementation meets acceptance criteria",
        ))

        return plan, f"Generated workflow plan {plan_id}"

    def _parse_spec(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse SPEC markdown content."""
        data = {}
        current_section = None
        section_content = []
        in_frontmatter = False
        frontmatter_lines = []

        lines = content.split("\n")

        for line in lines:
            # Parse frontmatter
            if line.strip() == "---" and not in_frontmatter and not current_section:
                in_frontmatter = True
                continue
            elif line.strip() == "---" and in_frontmatter:
                in_frontmatter = False
                # Parse frontmatter
                for fmline in frontmatter_lines:
                    if ":" in fmline:
                        key, value = fmline.split(":", 1)
                        data[key.strip()] = value.strip()
                frontmatter_lines = []
                continue

            if in_frontmatter:
                frontmatter_lines.append(line)
                continue

            # Parse sections
            if line.startswith("## "):
                if current_section:
                    data[current_section] = "\n".join(section_content).strip()
                current_section = line[3:].strip().lower()
                section_content = []
            elif current_section:
                section_content.append(line)

        if current_section:
            data[current_section] = "\n".join(section_content).strip()

        return data

    def _extract_ac_sections(self, content: str) -> List[Dict[str, str]]:
        """Extract acceptance criteria sections."""
        sections = []
        lines = content.split("\n")
        current_ac = None
        ac_content = []

        for line in lines:
            line = line.strip()

            # Detect AC header
            if line.startswith("### AC-") or line.startswith("### AC-"):
                if current_ac:
                    sections.append({
                        "title": current_ac,
                        "content": "\n".join(ac_content).strip()
                    })
                current_ac = line[4:].strip()
                ac_content = []
            elif line.startswith("- "):
                ac_content.append(line[2:])
            elif line and current_ac:
                ac_content.append(line)

        if current_ac:
            sections.append({
                "title": current_ac,
                "content": "\n".join(ac_content).strip()
            })

        return sections

    def save_plan(self, plan: WorkflowPlan) -> Tuple[bool, str]:
        """Save workflow plan to file.

        Args:
            plan: WorkflowPlan to save

        Returns:
            (ok, file_path) tuple
        """
        # Ensure workflow directory exists
        os.makedirs(self.workflow_dir, exist_ok=True)

        # Generate file path
        file_path = os.path.join(self.workflow_dir, f"{plan.plan_id}.json")

        # Write plan
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(plan.to_dict(), f, indent=2)
            return True, file_path
        except IOError as e:
            return False, f"Failed to write plan: {e}"

    def save_plan_markdown(self, plan: WorkflowPlan) -> Tuple[bool, str]:
        """Save workflow plan as markdown.

        Args:
            plan: WorkflowPlan to save

        Returns:
            (ok, file_path) tuple
        """
        # Ensure workflow directory exists
        os.makedirs(self.workflow_dir, exist_ok=True)

        # Generate file path
        file_path = os.path.join(self.workflow_dir, f"{plan.plan_id}.md")

        # Write markdown
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(plan.to_markdown())
            return True, file_path
        except IOError as e:
            return False, f"Failed to write plan: {e}"


def generate_workflow_from_spec(
    spec_path: str,
    harness_root: str,
    plan_id: Optional[str] = None,
) -> Tuple[bool, str, Optional[WorkflowPlan]]:
    """Convenience function to generate workflow from SPEC.

    Args:
        spec_path: Path to SPEC file
        harness_root: Path to harness root
        plan_id: Optional plan identifier

    Returns:
        (ok, message, plan) tuple
    """
    generator = WorkflowAutoGenerator(harness_root)
    plan, message = generator.generate_from_spec(spec_path, plan_id)

    if not plan:
        return False, message, None

    ok, path = generator.save_plan(plan)
    if ok:
        return True, f"Workflow plan saved to {path}", plan
    else:
        return False, path, plan


# Add to_markdown method to WorkflowPlan
def _workflow_plan_to_markdown(self) -> str:
    """Convert workflow plan to markdown."""
    lines = []

    # Header
    lines.append(f"# {self.title}")
    lines.append("")
    lines.append(f"**Plan ID:** {self.plan_id}")
    lines.append(f"**SPEC ID:** {self.spec_id}")
    lines.append(f"**Created:** {self.created_at}")
    lines.append("")

    # Description
    if self.description:
        lines.append(f"**Description:** {self.description}")
        lines.append("")

    # Steps
    lines.append("## Workflow Steps")
    lines.append("")

    ordered_steps = self.get_execution_order()
    for i, step in enumerate(ordered_steps, 1):
        lines.append(f"### {i}. {step.name}")
        lines.append("")
        lines.append(f"**Step ID:** {step.step_id}")
        lines.append(f"**Phase:** {step.phase}")
        lines.append(f"**Agent:** {step.agent_type}")
        lines.append("")
        lines.append(step.description)
        lines.append("")

        if step.dependencies:
            lines.append(f"**Dependencies:** {', '.join(step.dependencies)}")
            lines.append("")

        if step.evidence_required:
            lines.append(f"**Evidence Required:** {', '.join(step.evidence_required)}")
            lines.append("")

    # Gates
    if self.gates:
        lines.append("## Quality Gates")
        lines.append("")

        for gate in self.gates:
            lines.append(f"### {gate.gate_id}")
            lines.append("")
            lines.append(f"**From:** {gate.from_step} → **To:** {gate.to_step}")
            lines.append("")
            lines.append(gate.description)
            lines.append("")

            if gate.required_evidence:
                lines.append(f"**Required Evidence:** {', '.join(gate.required_evidence)}")
                lines.append("")

            if gate.auditor:
                lines.append(f"**Auditor:** {gate.auditor}")
                lines.append("")

    return "\n".join(lines)


# Monkey patch the method
WorkflowPlan.to_markdown = _workflow_plan_to_markdown
