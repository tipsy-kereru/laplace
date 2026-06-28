#!/usr/bin/env python3
"""Orchestration modes: single, team, parallel with dynamic switching.

Inspired by MoAI-ADK's 6-mode orchestration (simplified to 4 modes).

Modes:
    - single: One agent handles all phases sequentially
    - team: Different agents for different phases (existing PM→Dev→Review→Security)
    - parallel: Multiple issues processed concurrently
    - workflow: Full multi-phase workflow with auditors

stdlib-only.
"""

import argparse
import json
import os
import sys
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import state  # noqa: E402


class OrchestrationMode(Enum):
    """Orchestration modes for issue execution."""
    SINGLE = "single"      # One agent, sequential
    TEAM = "team"          # Existing PM→Dev→Review→Security agents
    PARALLEL = "parallel"  # Multiple issues concurrently
    WORKFLOW = "workflow"  # Full multi-phase with auditors


# Complexity thresholds for mode selection
MODE_COMPLEXITY_THRESHOLDS = {
    "single": {"max_files": 5, "max_diff_lines": 200},
    "team": {"max_files": 20, "max_diff_lines": 1000},
    "parallel": {"min_issues": 3, "max_parallel": 2},
    "workflow": {"requires_auditors": True},
}


def select_mode(issue_data: Dict[str, Any],
                config: Optional[Dict] = None) -> OrchestrationMode:
    """Select appropriate mode based on issue complexity and config.

    Args:
        issue_data: Issue data including:
            - touches: List of files this issue touches
            - routing: Routing configuration
            - dependencies: List of dependencies
        config: Optional global configuration

    Returns:
        Selected OrchestrationMode
    """
    # Default to workflow mode (full automation with auditors)
    default_mode = OrchestrationMode.WORKFLOW

    # Check if mode is explicitly specified in config
    if config:
        explicit_mode = config.get("orchestration", {}).get("default_mode")
        if explicit_mode:
            try:
                return OrchestrationMode(explicit_mode)
            except ValueError:
                pass  # Fall through to automatic selection

    # Automatic selection based on complexity
    touches = issue_data.get("touches", [])
    dependencies = issue_data.get("depends_on", [])

    # Count files and estimate complexity
    file_count = len(touches)

    # Check mode selection rules from routing-rules.yml
    routing_rules = _load_routing_rules(config)
    mode_selection = routing_rules.get("orchestration", {}).get("mode_selection", [])

    for rule in mode_selection:
        condition = rule.get("condition", "")
        mode = rule.get("mode")

        if _evaluate_condition(condition, issue_data):
            try:
                return OrchestrationMode(mode)
            except ValueError:
                continue

    # Heuristic fallback
    if file_count <= 5:
        return OrchestrationMode.SINGLE
    elif file_count <= 20:
        return OrchestrationMode.TEAM
    else:
        return OrchestrationMode.WORKFLOW


def can_switch_mode(current: OrchestrationMode,
                    target: OrchestrationMode) -> Tuple[bool, str]:
    """Check if mode switch is allowed.

    Args:
        current: Current mode
        target: Target mode

    Returns:
        (ok, reason) tuple
    """
    # Define allowed switches
    allowed_switches = {
        OrchestrationMode.SINGLE: [OrchestrationMode.TEAM, OrchestrationMode.WORKFLOW],
        OrchestrationMode.TEAM: [OrchestrationMode.WORKFLOW, OrchestrationMode.PARALLEL],
        OrchestrationMode.PARALLEL: [OrchestrationMode.TEAM, OrchestrationMode.WORKFLOW],
        OrchestrationMode.WORKFLOW: [],  # No switching from workflow mode
    }

    allowed = allowed_switches.get(current, [])
    if target in allowed:
        return True, f"Allowed: {current.value} -> {target.value}"

    if current == target:
        return True, "No-op (same mode)"

    return False, f"Mode switch not allowed: {current.value} -> {target.value}"


def _load_routing_rules(config: Optional[Dict] = None) -> Dict[str, Any]:
    """Load routing rules from .harness/routing-rules.yml.

    Args:
        config: Optional configuration (unused, kept for API compatibility)

    Returns:
        Routing rules dictionary
    """
    target = config.get("target") if config else None
    harness_root = state._harness_root(target)
    rules_path = os.path.join(harness_root, ".harness", "routing-rules.yml")

    if not os.path.exists(rules_path):
        return {}

    try:
        import yaml  # Try to import yaml (optional dependency)
        with open(rules_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # YAML not available, return empty dict
        return {}
    except Exception:
        return {}


def _evaluate_condition(condition: str, issue_data: Dict[str, Any]) -> bool:
    """Evaluate a mode selection condition.

    Args:
        condition: Condition string (e.g., "files_changed >= 20")
        issue_data: Issue data

    Returns:
        True if condition evaluates to true
    """
    # Simple condition parser for common patterns
    try:
        # files_changed patterns
        if "files_changed" in condition:
            touches = issue_data.get("touches", [])
            file_count = len(touches)
            if ">=" in condition:
                threshold = int(condition.split(">=")[1].strip())
                return file_count >= threshold
            elif ">" in condition:
                threshold = int(condition.split(">")[1].strip())
                return file_count > threshold

        # dependencies patterns
        if "dependencies.length" in condition:
            deps = issue_data.get("depends_on", [])
            dep_count = len(deps)
            if ">" in condition:
                threshold = int(condition.split(">")[1].strip())
                return dep_count > threshold

        # queue.length patterns
        if "queue.length" in condition:
            tasks = state._load_tasks()
            queue = state._load_queue()
            queue_length = len(queue.get("draft", []))
            if ">=" in condition:
                threshold = int(condition.split(">=")[1].strip())
                return queue_length >= threshold

    except (ValueError, IndexError):
        pass

    return False


def get_mode_info(mode: OrchestrationMode) -> Dict[str, Any]:
    """Get information about an orchestration mode.

    Args:
        mode: OrchestrationMode

    Returns:
        Mode information dictionary
    """
    info = {
        "name": mode.value,
        "description": "",
        "thresholds": MODE_COMPLEXITY_THRESHOLDS.get(mode.value, {}),
    }

    if mode == OrchestrationMode.SINGLE:
        info["description"] = "One agent handles all phases sequentially"
    elif mode == OrchestrationMode.TEAM:
        info["description"] = "Different agents for different phases (PM→Dev→Review→Security)"
    elif mode == OrchestrationMode.PARALLEL:
        info["description"] = "Multiple issues processed concurrently"
    elif mode == OrchestrationMode.WORKFLOW:
        info["description"] = "Full multi-phase workflow with auditors"

    return info


# CLI interface

def cmd_select(args: argparse.Namespace) -> int:
    """CLI: Select mode for an issue."""
    tasks = state._load_tasks(args.target)
    issue_id = args.issue

    if issue_id not in tasks:
        print(f"Error: issue not found: {issue_id}", file=sys.stderr)
        return 1

    issue_data = {
        "touches": tasks[issue_id].get("touches", []),
        "depends_on": tasks[issue_id].get("depends_on", []),
        "routing": tasks[issue_id].get("routing", {}),
    }

    config = state.load_config(args.target) if args.use_config else None
    mode = select_mode(issue_data, config)

    if args.json:
        print(json.dumps({"mode": mode.value, "issue": issue_id}, indent=2))
    else:
        print(f"Selected mode: {mode.value}")
        print(f"Reason: {get_mode_info(mode)['description']}")

    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """CLI: Show information about orchestration modes."""
    if args.mode:
        try:
            mode = OrchestrationMode(args.mode)
            info = get_mode_info(mode)
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(f"Mode: {info['name']}")
                print(f"Description: {info['description']}")
                print(f"Thresholds: {info['thresholds']}")
        except ValueError:
            print(f"Error: unknown mode: {args.mode}", file=sys.stderr)
            return 1
    else:
        # List all modes
        modes_info = {}
        for mode in OrchestrationMode:
            modes_info[mode.value] = get_mode_info(mode)

        if args.json:
            print(json.dumps(modes_info, indent=2))
        else:
            for mode_name, info in modes_info.items():
                print(f"{mode_name}: {info['description']}")


def cmd_can_switch(args: argparse.Namespace) -> int:
    """CLI: Check if mode switch is allowed."""
    try:
        current = OrchestrationMode(args.current)
        target = OrchestrationMode(args.target_mode)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    ok, reason = can_switch_mode(current, target)

    if args.json:
        print(json.dumps({"allowed": ok, "reason": reason}, indent=2))
    else:
        print(f"Switch {current.value} -> {target_mode}: {reason}")

    return 0 if ok else 1


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Orchestration modes for Laplace workflow"
    )
    parser.add_argument(
        "--target",
        help="Path to repository root (default: CWD)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # select command
    select_parser = subparsers.add_parser("select", help="Select mode for an issue")
    select_parser.add_argument("--issue", required=True, help="Issue identifier")
    select_parser.add_argument("--use-config", action="store_true",
                              help="Use config.yml for mode selection")
    select_parser.set_defaults(func=cmd_select)

    # info command
    info_parser = subparsers.add_parser("info", help="Show mode information")
    info_parser.add_argument("--mode", help="Mode to show info for (omit for all)")
    info_parser.set_defaults(func=cmd_info)

    # can-switch command
    switch_parser = subparsers.add_parser("can-switch", help="Check mode switch permission")
    switch_parser.add_argument("--current", required=True,
                              choices=[m.value for m in OrchestrationMode],
                              help="Current mode")
    switch_parser.add_argument("--target-mode", dest="target_mode", required=True,
                              choices=[m.value for m in OrchestrationMode],
                              help="Target mode")
    switch_parser.set_defaults(func=cmd_can_switch)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Set target for subcommands
    if hasattr(args, 'target') and args.target:
        # Save target in state module context
        pass  # state.py handles this internally

    handler = getattr(args, 'func', None)
    if not handler:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
