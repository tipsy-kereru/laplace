"""Base component interface for Laplace component system.

Provides the abstract Component class that all workflow components implement.
Inspired by LazyCodex's component pattern but simplified for Python stdlib.
"""

import abc
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path


class Component(abc.ABC):
    """Abstract base class for Laplace workflow components.

    Each component represents a phase in the development workflow:
    - Phase 0 (intent): Requirement clarification
    - Phase 1 (workflow): Workflow planning
    - Phase 2 (execute): Implementation execution
    - Phase 3 (verify): Independent audit
    - Phase 4 (release): Release automation

    Components maintain their own state under .harness/components/<name>/.
    """

    # Class attributes for component metadata
    name: str = ""
    version: str = "0.1.0"
    phase: str = ""
    description: str = ""

    @abc.abstractmethod
    def initialize(self, harness_root: str, config: Optional[Dict] = None) -> bool:
        """Initialize the component with harness context.

        Creates component state directory under .harness/components/<name>/.

        Args:
            harness_root: Path to .harness/ directory
            config: Optional component-specific configuration

        Returns:
            True if initialization successful, False otherwise
        """
        pass

    @abc.abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the component's primary function.

        Args:
            context: Execution context including:
                - issue_id: Issue identifier
                - run_id: Run identifier
                - phase: Current workflow phase
                - ... other phase-specific data

        Returns:
            Result dictionary with:
                - success: bool
                - output: Dict[str, Any]
                - evidence: List[Dict[str, str]]  # Evidence entries
                - error: Optional[str]
        """
        pass

    @abc.abstractmethod
    def cleanup(self, harness_root: str) -> bool:
        """Clean up component resources.

        Args:
            harness_root: Path to .harness/ directory

        Returns:
            True if cleanup successful, False otherwise
        """
        pass

    @abc.abstractmethod
    def get_evidence(self, harness_root: str, run_id: str) -> List[Dict[str, str]]:
        """Retrieve all evidence captured by this component.

        Args:
            harness_root: Path to .harness/ directory
            run_id: Run identifier

        Returns:
            List of evidence entries, each with:
                - ts: ISO timestamp
                - kind: Evidence kind
                - summary: Evidence summary
                - source_path: Optional path to evidence artifact
        """
        pass

    # Helper methods for components

    def _get_component_dir(self, harness_root: str) -> str:
        """Get the component's state directory path.

        Args:
            harness_root: Path to .harness/ directory

        Returns:
            Path to .harness/components/<self.name>/
        """
        return os.path.join(harness_root, "components", self.name)

    def _ensure_component_dir(self, harness_root: str) -> str:
        """Ensure component state directory exists.

        Args:
            harness_root: Path to .harness/ directory

        Returns:
            Path to component directory
        """
        comp_dir = self._get_component_dir(harness_root)
        os.makedirs(comp_dir, exist_ok=True)
        return comp_dir

    def _write_state(self, harness_root: str, state: Dict[str, Any]) -> None:
        """Write component state atomically.

        Uses os.replace pattern from state.py for atomic writes.

        Args:
            harness_root: Path to .harness/ directory
            state: State dictionary to persist
        """
        comp_dir = self._ensure_component_dir(harness_root)
        state_file = os.path.join(comp_dir, "state.json")
        tmp_file = state_file + ".tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)

        os.replace(tmp_file, state_file)

    def _read_state(self, harness_root: str) -> Dict[str, Any]:
        """Read component state.

        Args:
            harness_root: Path to .harness/ directory

        Returns:
            State dictionary, or empty dict if no state exists
        """
        comp_dir = self._get_component_dir(harness_root)
        state_file = os.path.join(comp_dir, "state.json")

        if not os.path.exists(state_file):
            return {}

        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _add_evidence(self, harness_root: str, run_id: str,
                      kind: str, summary: str, source_path: Optional[str] = None) -> None:
        """Add an evidence entry for this component.

        Args:
            harness_root: Path to .harness/ directory
            run_id: Run identifier
            kind: Evidence kind (test, review, security, etc.)
            summary: Evidence summary
            source_path: Optional path to evidence artifact
        """
        from datetime import datetime

        comp_dir = self._ensure_component_dir(harness_root)
        evidence_file = os.path.join(comp_dir, f"evidence-{run_id}.jsonl")

        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "run_id": run_id,
            "component": self.name,
            "kind": kind,
            "summary": summary,
            "source_path": source_path,
        }

        with open(evidence_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def get_metadata(self) -> Dict[str, Any]:
        """Get component metadata.

        Returns:
            Dictionary with component metadata
        """
        return {
            "name": self.name,
            "version": self.version,
            "phase": self.phase,
            "description": self.description,
        }


class ComponentError(Exception):
    """Base exception for component errors."""

    def __init__(self, component: str, message: str):
        self.component = component
        self.message = message
        super().__init__(f"[{component}] {message}")


class ComponentInitializationError(ComponentError):
    """Raised when component initialization fails."""

    pass


class ComponentExecutionError(ComponentError):
    """Raised when component execution fails."""

    pass
