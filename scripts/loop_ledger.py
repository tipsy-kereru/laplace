#!/usr/bin/env python3
"""Loop ledger: append-only JSONL audit trail for context survival.

Inspired by LazyCodex's ULW-Loop ledger pattern, adapted for Laplace.

Responsibilities:
    - Append-only writes to .harness/loop/ledger.jsonl
    - Record step completion with evidence references
    - Support context reconstruction after compaction
    - Atomic writes with os.replace() pattern
    - Redaction of all persisted fields

The ledger survives context compaction and provides complete audit trail
for all workflow operations. Each entry includes:
    - timestamp: ISO-8601 timestamp
    - run_id: Run identifier
    - issue_id: Issue identifier
    - phase: Workflow phase (intent|plan|run|sync)
    - step: Step identifier
    - status: started|completed|failed|blocked
    - evidence_refs: List of evidence references (run-id:kind format)
    - metadata: Additional metadata

stdlib-only. Imports redaction.py for secret redaction.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import redaction  # noqa: E402

# Ledger file path
LEDGER_FILE = ".harness/loop/ledger.jsonl"
LEDGER_TMP = ".harness/loop/ledger.jsonl.tmp"

# Entry schema
LEDGER_ENTRY_SCHEMA = {
    "timestamp": "ISO-8601 timestamp",
    "run_id": "Run identifier",
    "issue_id": "Issue identifier",
    "phase": "intent|plan|run|sync",
    "step": "Step identifier",
    "status": "started|completed|failed|blocked",
    "evidence_refs": ["run-id:kind", ...],
    "metadata": {},
}

# Valid statuses
VALID_STATUSES = {"started", "completed", "failed", "blocked"}

# Valid phases
VALID_PHASES = {"intent", "plan", "run", "sync"}


def _get_ledger_path(harness_root: Optional[str] = None) -> str:
    """Get the ledger file path.

    Args:
        harness_root: Path to .harness/ directory (default: cwd/.harness)

    Returns:
        Path to ledger.jsonl
    """
    if harness_root is None:
        harness_root = os.path.join(os.getcwd(), ".harness")
    return os.path.join(harness_root, "loop", "ledger.jsonl")


def _ensure_ledger_dir(harness_root: Optional[str] = None) -> str:
    """Ensure ledger directory exists.

    Args:
        harness_root: Path to .harness/ directory

    Returns:
        Path to ledger directory
    """
    if harness_root is None:
        harness_root = os.path.join(os.getcwd(), ".harness")
    ledger_dir = os.path.join(harness_root, "loop")
    os.makedirs(ledger_dir, exist_ok=True)
    return ledger_dir


def _redact_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive fields from a ledger entry.

    Args:
        entry: Ledger entry to redact

    Returns:
        Redacted entry
    """
    # Create a copy to avoid mutating original
    redacted = entry.copy()

    # Redact metadata fields that might contain secrets
    if "metadata" in redacted and redacted["metadata"]:
        redacted["metadata"] = redaction.redact_dict(redacted["metadata"])

    # Redact evidence_refs that might contain paths
    if "evidence_refs" in redacted and redacted["evidence_refs"]:
        redacted_refs = []
        for ref in redacted["evidence_refs"]:
            # Redact any potential secrets in refs
            redacted_ref = redaction.redact(str(ref))
            redacted_refs.append(redacted_ref)
        redacted["evidence_refs"] = redacted_refs

    return redacted


def append_entry(entry: Dict[str, Any],
                 harness_root: Optional[str] = None) -> Tuple[bool, str]:
    """Append a single entry to the ledger.

    Uses atomic write pattern: write to tmp file, then append to ledger.
    This ensures partial writes never corrupt the ledger.

    Args:
        entry: Ledger entry to append
        harness_root: Path to .harness/ directory

    Returns:
        (ok, reason) tuple
    """
    # Add timestamp if not present (before validation)
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.utcnow().isoformat() + "Z"

    # Validate entry
    validation = _validate_entry(entry)
    if not validation[0]:
        return validation

    # Ensure ledger directory exists
    _ensure_ledger_dir(harness_root)

    # Redact sensitive fields
    redacted = _redact_entry(entry)

    # Serialize to JSON
    try:
        line = json.dumps(redacted, separators=(",", ":"))
    except TypeError as e:
        return False, f"Failed to serialize entry: {e}"

    # Get ledger path
    ledger_path = _get_ledger_path(harness_root)

    # Append to ledger (atomic for single write)
    try:
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True, ""
    except IOError as e:
        return False, f"Failed to write to ledger: {e}"


def _validate_entry(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Validate a ledger entry.

    Args:
        entry: Entry to validate

    Returns:
        (ok, reason) tuple
    """
    # Check required fields (timestamp added automatically)
    required_fields = {"run_id", "issue_id", "phase", "step", "status"}
    missing = required_fields - set(entry.keys())
    if missing:
        return False, f"Missing required fields: {missing}"

    # Validate status
    if entry["status"] not in VALID_STATUSES:
        return False, f"Invalid status: {entry['status']}"

    # Validate phase
    if entry["phase"] not in VALID_PHASES:
        return False, f"Invalid phase: {entry['phase']}"

    # Validate evidence_refs is a list
    if "evidence_refs" in entry and not isinstance(entry["evidence_refs"], list):
        return False, "evidence_refs must be a list"

    return True, ""


def get_entries(run_id: Optional[str] = None,
                issue_id: Optional[str] = None,
                phase: Optional[str] = None,
                status: Optional[str] = None,
                harness_root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Query ledger by optional filters.

    Args:
        run_id: Filter by run_id
        issue_id: Filter by issue_id
        phase: Filter by phase
        status: Filter by status
        harness_root: Path to .harness/ directory

    Returns:
        List of matching ledger entries (empty if ledger doesn't exist)
    """
    ledger_path = _get_ledger_path(harness_root)

    if not os.path.exists(ledger_path):
        return []

    entries = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # Skip malformed entries

            # Apply filters
            if run_id is not None and entry.get("run_id") != run_id:
                continue
            if issue_id is not None and entry.get("issue_id") != issue_id:
                continue
            if phase is not None and entry.get("phase") != phase:
                continue
            if status is not None and entry.get("status") != status:
                continue

            entries.append(entry)

    return entries


def reconstruct_context(run_id: str,
                        harness_root: Optional[str] = None) -> Dict[str, Any]:
    """Reconstruct full context from ledger for a given run_id.

    Used after context compaction to resume workflow execution.

    Args:
        run_id: Run identifier to reconstruct
        harness_root: Path to .harness/ directory

    Returns:
        Context dictionary with:
            - run_id: Run identifier
            - issue_id: Issue identifier
            - phases: Dict mapping phase names to phase data
            - steps: List of completed steps
            - current_phase: Current workflow phase
            - current_step: Current step (if any)
            - evidence_refs: All evidence references
    """
    entries = get_entries(run_id=run_id, harness_root=harness_root)

    if not entries:
        return {}

    # Build context
    context = {
        "run_id": run_id,
        "issue_id": entries[0].get("issue_id", ""),
        "phases": {},
        "steps": [],
        "current_phase": None,
        "current_step": None,
        "evidence_refs": [],
    }

    # Process entries in order
    for entry in entries:
        phase = entry["phase"]
        step = entry["step"]
        status = entry["status"]

        # Initialize phase if not exists
        if phase not in context["phases"]:
            context["phases"][phase] = {
                "started_at": None,
                "completed_at": None,
                "steps": [],
            }

        # Update phase timestamps
        if status == "started":
            context["phases"][phase]["started_at"] = entry["timestamp"]
            context["current_phase"] = phase
        elif status == "completed":
            context["phases"][phase]["completed_at"] = entry["timestamp"]

        # Add step
        context["phases"][phase]["steps"].append({
            "step": step,
            "status": status,
            "timestamp": entry["timestamp"],
        })

        # Track current step (last started but not completed)
        if status == "started":
            context["current_step"] = step

        # Add to steps list
        context["steps"].append({
            "phase": phase,
            "step": step,
            "status": status,
            "timestamp": entry["timestamp"],
        })

        # Collect evidence refs
        if "evidence_refs" in entry:
            context["evidence_refs"].extend(entry["evidence_refs"])

    return context


def get_latest_status(run_id: str,
                     harness_root: Optional[str] = None) -> Optional[str]:
    """Get the latest status for a run_id.

    Args:
        run_id: Run identifier
        harness_root: Path to .harness/ directory

    Returns:
        Latest status, or None if no entries found
    """
    entries = get_entries(run_id=run_id, harness_root=harness_root)
    if not entries:
        return None
    return entries[-1].get("status")


def is_complete(run_id: str,
               harness_root: Optional[str] = None) -> bool:
    """Check if a run is complete (has completed entry).

    Args:
        run_id: Run identifier
        harness_root: Path to .harness/ directory

    Returns:
        True if run has completed status
    """
    latest = get_latest_status(run_id, harness_root)
    return latest == "completed"


def get_incomplete_runs(harness_root: Optional[str] = None) -> List[str]:
    """Get list of run_ids that have started but not completed.

    Args:
        harness_root: Path to .harness/ directory

    Returns:
        List of incomplete run_ids
    """
    ledger_path = _get_ledger_path(harness_root)
    if not os.path.exists(ledger_path):
        return []

    # Track run status
    run_status = {}

    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
                run_id = entry.get("run_id")
                if run_id:
                    run_status[run_id] = entry.get("status")
            except json.JSONDecodeError:
                continue

    # Return runs that are not complete
    return [run_id for run_id, status in run_status.items()
            if status != "completed"]


# CLI interface

def cmd_append(args: argparse.Namespace) -> int:
    """CLI: Append an entry to the ledger."""
    entry = {
        "run_id": args.run_id,
        "issue_id": args.issue_id,
        "phase": args.phase,
        "step": args.step,
        "status": args.status,
        "evidence_refs": args.evidence_refs or [],
        "metadata": args.metadata or {},
    }

    ok, reason = append_entry(entry, args.harness_root)
    if not ok:
        print(f"Error: {reason}", file=sys.stderr)
        return 1

    print(f"Entry appended for run {args.run_id}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """CLI: Query ledger entries."""
    entries = get_entries(
        run_id=args.run_id,
        issue_id=args.issue_id,
        phase=args.phase,
        status=args.status,
        harness_root=args.harness_root,
    )

    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        for entry in entries:
            print(f"[{entry['timestamp']}] {entry['phase']}/{entry['step']}: {entry['status']}")

    return 0


def cmd_reconstruct(args: argparse.Namespace) -> int:
    """CLI: Reconstruct context for a run_id."""
    context = reconstruct_context(args.run_id, args.harness_root)

    if not context:
        print(f"No entries found for run {args.run_id}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(context, indent=2))
    else:
        print(f"Run: {context['run_id']}")
        print(f"Issue: {context['issue_id']}")
        print(f"Current phase: {context['current_phase']}")
        print(f"Current step: {context['current_step']}")
        print(f"Phases: {list(context['phases'].keys())}")
        print(f"Steps completed: {len(context['steps'])}")

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """CLI: Show latest status for a run."""
    status = get_latest_status(args.run_id, args.harness_root)

    if status is None:
        print(f"No entries found for run {args.run_id}", file=sys.stderr)
        return 1

    print(status)
    return 0


def cmd_incomplete(args: argparse.Namespace) -> int:
    """CLI: List incomplete runs."""
    runs = get_incomplete_runs(args.harness_root)

    if args.json:
        print(json.dumps(runs, indent=2))
    else:
        for run_id in runs:
            print(run_id)

    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Loop ledger: append-only audit trail for context survival"
    )
    parser.add_argument(
        "--harness-root",
        help="Path to .harness/ directory (default: cwd/.harness)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # append command
    append_parser = subparsers.add_parser("append", help="Append an entry")
    append_parser.add_argument("--run-id", required=True, help="Run identifier")
    append_parser.add_argument("--issue-id", required=True, help="Issue identifier")
    append_parser.add_argument("--phase", required=True, choices=list(VALID_PHASES), help="Workflow phase")
    append_parser.add_argument("--step", required=True, help="Step identifier")
    append_parser.add_argument("--status", required=True, choices=list(VALID_STATUSES), help="Status")
    append_parser.add_argument("--evidence-ref", action="append", dest="evidence_refs", help="Evidence reference")
    append_parser.add_argument("--metadata", help="JSON metadata")

    # query command
    query_parser = subparsers.add_parser("query", help="Query ledger entries")
    query_parser.add_argument("--run-id", help="Filter by run_id")
    query_parser.add_argument("--issue-id", help="Filter by issue_id")
    query_parser.add_argument("--phase", choices=list(VALID_PHASES), help="Filter by phase")
    query_parser.add_argument("--status", choices=list(VALID_STATUSES), help="Filter by status")

    # reconstruct command
    reconstruct_parser = subparsers.add_parser("reconstruct", help="Reconstruct context")
    reconstruct_parser.add_argument("--run-id", required=True, help="Run identifier")

    # status command
    status_parser = subparsers.add_parser("status", help="Show latest status")
    status_parser.add_argument("--run-id", required=True, help="Run identifier")

    # incomplete command
    subparsers.add_parser("incomplete", help="List incomplete runs")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Parse metadata JSON if provided
    if hasattr(args, "metadata") and args.metadata:
        try:
            args.metadata = json.loads(args.metadata)
        except json.JSONDecodeError:
            print(f"Invalid metadata JSON: {args.metadata}", file=sys.stderr)
            return 1

    # Dispatch to command handler
    handlers = {
        "append": cmd_append,
        "query": cmd_query,
        "reconstruct": cmd_reconstruct,
        "status": cmd_status,
        "incomplete": cmd_incomplete,
    }

    handler = handlers.get(args.command)
    if not handler:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
