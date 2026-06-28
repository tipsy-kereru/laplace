#!/usr/bin/env python3
"""workflow-gen skill: Generate workflow plans from SPEC.

Usage:
    python3 skills/workflow-gen/workflow_gen.py <spec.md> [target]
"""

import argparse
import os
import sys
from typing import Optional

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from components.workflow import generate_workflow_from_spec


def _harness_root(target: Optional[str] = None) -> str:
    """Get harness root directory."""
    if target:
        return target
    return os.getcwd()


def cmd_workflow_gen(spec_path: str, target: Optional[str] = None) -> int:
    """Generate workflow from SPEC.

    Args:
        spec_path: Path to SPEC markdown file
        target: Repository root containing .harness/

    Returns:
        Exit code (0 = success)
    """
    # Resolve harness root
    harness_root = _harness_root(target)

    # Verify SPEC exists
    if not os.path.isfile(spec_path):
        print(f"Error: SPEC file not found: {spec_path}", file=sys.stderr)
        return 1

    # Generate workflow
    ok, message, plan = generate_workflow_from_spec(spec_path, harness_root)

    if not ok:
        print(f"Error: {message}", file=sys.stderr)
        return 1

    # Output result
    print("Laplace result: Workflow plan generated")
    print()
    print(f"Plan: {plan.plan_id}")
    print(f"File: {message}")
    print(f"Steps: {len(plan.steps)}")
    print(f"Gates: {len(plan.gates)}")
    print()
    print("Workflow Steps:")
    for i, step in enumerate(plan.get_execution_order(), 1):
        print(f"{i}. {step.name} ({step.agent_type})")
    print()
    print("Quality Gates:")
    for gate in plan.gates:
        auditor = f" [{gate.auditor}]" if gate.auditor else ""
        print(f"- {gate.gate_id}: {gate.from_step} → {gate.to_step}{auditor}")
    print()
    print("Next: Review workflow and proceed to execution")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Generate workflow from SPEC")
    parser.add_argument("spec", help="Path to SPEC markdown file")
    parser.add_argument("--target", default=None, help="Repository root")
    args = parser.parse_args()

    return cmd_workflow_gen(args.spec, args.target)


if __name__ == "__main__":
    sys.exit(main())
