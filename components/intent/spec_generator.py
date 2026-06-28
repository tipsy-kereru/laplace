"""Spec generator: Automatic SPEC document generation from PRDs.

This component extends the intent capturer to generate structured SPEC
documents from PRD input, following MoAI-ADK's Plan phase pattern.

Generated SPEC includes:
- spec/plan: Implementation plan
- spec/acceptance: Acceptance criteria
- spec/research: Technical research
- spec/design: Design decisions

Inspired by MoAI-ADK's Manager-Spec agent workflow.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime


class SpecDocument:
    """Structured SPEC document following GEARS format."""

    def __init__(
        self,
        spec_id: str,
        title: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.spec_id = spec_id
        self.title = title
        self.description = description
        self.metadata = metadata or {}
        self.frontmatter = {}
        self.sections = {}

    def set_frontmatter(self, **kwargs) -> None:
        """Set SPEC frontmatter fields."""
        self.frontmatter.update(kwargs)

    def add_section(self, name: str, content: str) -> None:
        """Add a section to the SPEC."""
        self.sections[name] = content

    def to_markdown(self) -> str:
        """Convert SPEC to markdown format."""
        lines = []

        # Frontmatter
        if self.frontmatter:
            lines.append("---")
            for key, value in self.frontmatter.items():
                if isinstance(value, bool):
                    lines.append(f"{key}: {str(value).lower()}")
                elif isinstance(value, list):
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append(f"  - {item}")
                else:
                    lines.append(f"{key}: {value}")
            lines.append("---")
            lines.append("")

        # Title and description
        lines.append(f"# {self.title}")
        lines.append("")
        if self.description:
            lines.append(self.description)
            lines.append("")

        # Sections
        for name, content in self.sections.items():
            lines.append(f"## {name}")
            lines.append("")
            lines.append(content)
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert SPEC to dictionary."""
        return {
            "spec_id": self.spec_id,
            "title": self.title,
            "description": self.description,
            "frontmatter": self.frontmatter,
            "sections": self.sections,
            "metadata": self.metadata,
        }


class SpecGenerator:
    """Generate SPEC documents from PRDs."""

    def __init__(self, harness_root: str):
        self.harness_root = harness_root
        self.spec_dir = os.path.join(harness_root, ".harness", "specs")

    def generate_from_prd(
        self,
        prd_path: str,
        spec_id: Optional[str] = None,
    ) -> Tuple[Optional[SpecDocument], str]:
        """Generate a SPEC document from a PRD file.

        Args:
            prd_path: Path to PRD markdown file
            spec_id: Optional SPEC identifier (auto-generated if not provided)

        Returns:
            (SpecDocument, status message) tuple
        """
        # Read PRD
        try:
            with open(prd_path, "r", encoding="utf-8") as f:
                prd_content = f.read()
        except IOError as e:
            return None, f"Failed to read PRD: {e}"

        # Parse PRD
        prd_data = self._parse_prd(prd_content)
        if not prd_data:
            return None, "Failed to parse PRD"

        # Generate spec_id
        if not spec_id:
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            spec_id = f"SPEC-{timestamp}"

        # Create SPEC document
        spec = SpecDocument(
            spec_id=spec_id,
            title=prd_data.get("title", "Untitled"),
            description=prd_data.get("description", ""),
        )

        # Set frontmatter
        spec.set_frontmatter(
            spec_id=spec_id,
            status="draft",
            created_at=datetime.utcnow().isoformat() + "Z",
            type=prd_data.get("type", "feature"),
            priority=prd_data.get("priority", "medium"),
        )

        # Generate sections
        spec.add_section(
            "Overview",
            self._generate_overview(prd_data),
        )

        spec.add_section(
            "Requirements",
            self._generate_requirements(prd_data),
        )

        spec.add_section(
            "Acceptance Criteria",
            self._generate_acceptance_criteria(prd_data),
        )

        spec.add_section(
            "Technical Approach",
            self._generate_technical_approach(prd_data),
        )

        spec.add_section(
            "Implementation Plan",
            self._generate_implementation_plan(prd_data),
        )

        spec.add_section(
            "Risk Assessment",
            self._generate_risk_assessment(prd_data),
        )

        return spec, f"Generated SPEC {spec_id}"

    def _parse_prd(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse PRD markdown content into structured data."""
        lines = content.split("\n")
        data = {}
        current_section = None
        section_content = []

        # Parse frontmatter
        if lines and lines[0].startswith("---"):
            frontmatter_lines = []
            for line in lines[1:]:
                if line.startswith("---"):
                    break
                frontmatter_lines.append(line)

            # Parse YAML frontmatter
            for line in frontmatter_lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    data[key] = value

        # Extract title
        for line in lines:
            if line.startswith("# "):
                data["title"] = line[2:].strip()
                break

        # Extract sections
        in_code_block = False
        for line in lines:
            if line.startswith("```"):
                in_code_block = not in_code_block
                continue

            if in_code_block:
                continue

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

    def _generate_overview(self, prd_data: Dict[str, Any]) -> str:
        """Generate overview section."""
        overview = []

        if prd_data.get("overview"):
            overview.append(prd_data["overview"])
        else:
            overview.append("This document specifies the implementation requirements.")

        # Add purpose
        if prd_data.get("purpose"):
            overview.append(f"\n**Purpose:** {prd_data['purpose']}")

        # Add scope
        if prd_data.get("scope"):
            overview.append(f"\n**Scope:** {prd_data['scope']}")

        return "\n".join(overview)

    def _generate_requirements(self, prd_data: Dict[str, Any]) -> str:
        """Generate requirements section using GEARS format."""
        requirements = []

        # Functional requirements
        if prd_data.get("requirements"):
            requirements.append("### Functional Requirements")
            requirements.append("")
            reqs = prd_data["requirements"]
            if isinstance(reqs, str):
                for line in reqs.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # Convert to GEARS format
                        if re.match(r"^\d+\.", line):
                            requirements.append(f"- {line}")
                        elif line.startswith("-"):
                            requirements.append(line)
                        else:
                            requirements.append(f"- {line}")
                requirements.append("")

        # Non-functional requirements
        if prd_data.get("non-functional requirements") or prd_data.get("nfr"):
            requirements.append("### Non-Functional Requirements")
            requirements.append("")
            nfrs = prd_data.get("non-functional requirements") or prd_data.get("nfr", "")
            if isinstance(nfrs, str):
                for line in nfrs.split("\n"):
                    line = line.strip()
                    if line:
                        requirements.append(f"- {line}")
                requirements.append("")

        return "\n".join(requirements) if requirements else "No requirements specified."

    def _generate_acceptance_criteria(self, prd_data: Dict[str, Any]) -> str:
        """Generate acceptance criteria section."""
        ac = []

        if prd_data.get("acceptance criteria") or prd_data.get("ac"):
            criteria = prd_data.get("acceptance criteria") or prd_data.get("ac", "")
            if isinstance(criteria, str):
                lines = criteria.split("\n")
                current_ac = []
                ac_num = 1

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    if re.match(r"^###? AC-\d+", line, re.IGNORECASE):
                        if current_ac:
                            ac.extend(current_ac)
                            current_ac = []
                        ac.append(f"### AC-{ac_num}")
                        ac_num += 1
                    elif re.match(r"^(Given|When|Then|And)", line, re.IGNORECASE):
                        current_ac.append(f"  - {line}")
                    else:
                        current_ac.append(f"  - {line}")

                ac.extend(current_ac)

        return "\n".join(ac) if ac else "### AC-1\n  - Acceptance criteria will be defined."

    def _generate_technical_approach(self, prd_data: Dict[str, Any]) -> str:
        """Generate technical approach section."""
        approach = []

        # Technical notes
        if prd_data.get("technical notes"):
            approach.append("### Technical Considerations")
            approach.append("")
            approach.append(prd_data["technical notes"])
            approach.append("")

        # Architecture
        if prd_data.get("architecture"):
            approach.append("### Architecture")
            approach.append("")
            approach.append(prd_data["architecture"])
            approach.append("")

        # Components
        if prd_data.get("components"):
            approach.append("### Components")
            approach.append("")
            components = prd_data["components"]
            if isinstance(components, str):
                for line in components.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        approach.append(f"- {line}")
            approach.append("")

        # Dependencies
        if prd_data.get("dependencies"):
            approach.append("### Dependencies")
            approach.append("")
            deps = prd_data["dependencies"]
            if isinstance(deps, str):
                for line in deps.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        approach.append(f"- {line}")
            approach.append("")

        return "\n".join(approach) if approach else "Technical approach to be defined during planning phase."

    def _generate_implementation_plan(self, prd_data: Dict[str, Any]) -> str:
        """Generate implementation plan section."""
        plan = []

        plan.append("### Phase 1: Planning")
        plan.append("")
        plan.append("- Review and finalize requirements")
        plan.append("- Create detailed design documents")
        plan.append("- Define testing strategy")
        plan.append("")

        plan.append("### Phase 2: Implementation")
        plan.append("")
        plan.append("- Implement core functionality")
        plan.append("- Write unit tests")
        plan.append("- Conduct code reviews")
        plan.append("")

        plan.append("### Phase 3: Validation")
        plan.append("")
        plan.append("- Execute test suite")
        plan.append("- Verify acceptance criteria")
        plan.append("- Performance testing (if applicable)")
        plan.append("")

        # Add custom phases if specified
        if prd_data.get("implementation phases"):
            plan.append("### Additional Phases")
            plan.append("")
            phases = prd_data["implementation phases"]
            if isinstance(phases, str):
                for line in phases.split("\n"):
                    line = line.strip()
                    if line:
                        plan.append(line)
            plan.append("")

        return "\n".join(plan)

    def _generate_risk_assessment(self, prd_data: Dict[str, Any]) -> str:
        """Generate risk assessment section."""
        risks = []

        # Risk from routing metadata
        if prd_data.get("risk") or prd_data.get("risk / release impact"):
            risk_text = prd_data.get("risk") or prd_data.get("risk / release impact", "")
            risks.append("### Identified Risks")
            risks.append("")
            risks.append(risk_text)
            risks.append("")

        # Default risk categories
        if not risks:
            risks.append("### Risk Categories")
            risks.append("")
            risks.append("- **Technical Risk**: Implementation complexity")
            risks.append("- **Integration Risk**: Compatibility with existing systems")
            risks.append("- **Performance Risk**: Impact on system performance")
            risks.append("- **Security Risk**: Potential security vulnerabilities")
            risks.append("")

        # Mitigation strategies
        if prd_data.get("mitigation"):
            risks.append("### Mitigation Strategies")
            risks.append("")
            risks.append(prd_data["mitigation"])
            risks.append("")

        return "\n".join(risks)

    def save_spec(self, spec: SpecDocument) -> Tuple[bool, str]:
        """Save SPEC document to file.

        Args:
            spec: SpecDocument to save

        Returns:
            (ok, file_path) tuple
        """
        # Ensure spec directory exists
        os.makedirs(self.spec_dir, exist_ok=True)

        # Generate file path
        file_path = os.path.join(self.spec_dir, f"{spec.spec_id}.md")

        # Write SPEC
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(spec.to_markdown())
            return True, file_path
        except IOError as e:
            return False, f"Failed to write SPEC: {e}"


def generate_spec_from_prd(
    prd_path: str,
    harness_root: str,
    spec_id: Optional[str] = None,
) -> Tuple[bool, str, Optional[SpecDocument]]:
    """Convenience function to generate SPEC from PRD.

    Args:
        prd_path: Path to PRD file
        harness_root: Path to harness root
        spec_id: Optional SPEC identifier

    Returns:
        (ok, message, spec) tuple
    """
    generator = SpecGenerator(harness_root)
    spec, message = generator.generate_from_prd(prd_path, spec_id)

    if not spec:
        return False, message, None

    ok, path = generator.save_spec(spec)
    if ok:
        return True, f"SPEC saved to {path}", spec
    else:
        return False, path, spec
