#!/usr/bin/env python3
"""spec-gen skill: Generate SPEC documents from PRDs.

Usage:
    python3 skills/spec-gen/spec_gen.py <prd.md> [target]
"""

import argparse
import os
import sys
from typing import Optional

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# Direct imports to avoid policy dependency
from components.intent import generate_spec_from_prd


def _harness_root(target: Optional[str] = None) -> str:
    """Get harness root directory."""
    if target:
        return target
    return os.getcwd()


def cmd_spec_gen(prd_path: str, target: Optional[str] = None) -> int:
    """Generate SPEC from PRD.

    Args:
        prd_path: Path to PRD markdown file
        target: Repository root containing .harness/

    Returns:
        Exit code (0 = success)
    """
    # Resolve harness root
    harness_root = _harness_root(target)

    # Verify PRD exists
    if not os.path.isfile(prd_path):
        print(f"Error: PRD file not found: {prd_path}", file=sys.stderr)
        return 1

    # Generate SPEC
    ok, message, spec = generate_spec_from_prd(prd_path, harness_root)

    if not ok:
        print(f"Error: {message}", file=sys.stderr)
        return 1

    # Output result
    print("Laplace result: SPEC generated")
    print()
    print(f"SPEC: {spec.spec_id}")
    print(f"File: {message}")
    print(f"Title: {spec.title}")
    print()
    print("Sections:")
    for section in spec.sections:
        print(f"- {section}: ✓")
    print()
    print("Next: Review SPEC and proceed to /laplace:workflow-gen")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Generate SPEC from PRD")
    parser.add_argument("prd", help="Path to PRD markdown file")
    parser.add_argument("--target", default=None, help="Repository root")
    args = parser.parse_args()

    return cmd_spec_gen(args.prd, args.target)


if __name__ == "__main__":
    sys.exit(main())
