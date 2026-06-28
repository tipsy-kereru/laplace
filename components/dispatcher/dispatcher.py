"""Agent dispatcher implementation.

Provides automatic agent dispatch for workflow phases.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum


class AgentSpec(Enum):
    """Agent type specifications."""

    PM = "pm"
    ARCHITECT = "architect"
    DEV = "dev"
    QA = "qa"
    REVIEWER = "reviewer"
    SECURITY = "security"
    PLAN_AUDITOR = "plan-auditor"
    SYNC_AUDITOR = "sync-auditor"


class DispatchStatus(Enum):
    """Status of agent dispatch."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class DispatchResult:
    """Result of agent dispatch."""

    def __init__(
        self,
        agent_type: str,
        status: DispatchStatus,
        output: Optional[str] = None,
        evidence: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.agent_type = agent_type
        self.status = status
        self.output = output
        self.evidence = evidence or []
        self.error = error
        self.metadata = metadata or {}
        self.dispatched_at = datetime.utcnow().isoformat() + "Z"
        self.completed_at = None

    def mark_completed(self, output: str, evidence: Optional[List[Dict[str, Any]]] = None) -> None:
        """Mark dispatch as completed."""
        self.status = DispatchStatus.COMPLETED
        self.output = output
        self.evidence = evidence or []
        self.completed_at = datetime.utcnow().isoformat() + "Z"

    def mark_failed(self, error: str) -> None:
        """Mark dispatch as failed."""
        self.status = DispatchStatus.FAILED
        self.error = error
        self.completed_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "agent_type": self.agent_type,
            "status": self.status.value,
            "output": self.output,
            "evidence": self.evidence,
            "error": self.error,
            "metadata": self.metadata,
            "dispatched_at": self.dispatched_at,
            "completed_at": self.completed_at,
        }


class AgentDispatcher:
    """Automatic agent dispatcher for workflow execution.

    Implements the conductor-workers pattern where the dispatcher
    orchestrates specialized agents for each workflow phase.
    """

    def __init__(self, harness_root: str):
        self.harness_root = harness_root
        self.dispatch_log_path = os.path.join(
            harness_root, ".harness", "state", "dispatch-log.jsonl"
        )

    def dispatch_for_step(
        self,
        step_id: str,
        agent_type: str,
        context: Dict[str, Any],
    ) -> DispatchResult:
        """Dispatch an agent for a workflow step.

        Args:
            step_id: Workflow step identifier
            agent_type: Type of agent to dispatch
            context: Execution context (spec, workflow, issue, etc.)

        Returns:
            DispatchResult
        """
        # Create dispatch result
        result = DispatchResult(
            agent_type=agent_type,
            status=DispatchStatus.RUNNING,
            metadata={"step_id": step_id, "context_keys": list(context.keys())},
        )

        # Log dispatch
        self._log_dispatch({
            "action": "dispatch",
            "step_id": step_id,
            "agent_type": agent_type,
            "status": "running",
            "timestamp": result.dispatched_at,
        })

        # In a real implementation, this would spawn the agent
        # For now, return a result that can be updated later
        return result

    def complete_dispatch(
        self,
        step_id: str,
        output: str,
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> DispatchResult:
        """Mark a dispatch as completed with results.

        Args:
            step_id: Workflow step identifier
            output: Agent output
            evidence: Captured evidence

        Returns:
            Updated DispatchResult
        """
        # Log completion
        self._log_dispatch({
            "action": "complete",
            "step_id": step_id,
            "status": "completed",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })

        return DispatchResult(
            agent_type="completed",
            status=DispatchStatus.COMPLETED,
            output=output,
            evidence=evidence,
        )

    def fail_dispatch(
        self,
        step_id: str,
        error: str,
    ) -> DispatchResult:
        """Mark a dispatch as failed.

        Args:
            step_id: Workflow step identifier
            error: Error message

        Returns:
            Failed DispatchResult
        """
        # Log failure
        self._log_dispatch({
            "action": "fail",
            "step_id": step_id,
            "status": "failed",
            "error": error,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })

        return DispatchResult(
            agent_type="failed",
            status=DispatchStatus.FAILED,
            error=error,
        )

    def get_agent_for_phase(self, phase: str) -> str:
        """Get the appropriate agent type for a workflow phase.

        Args:
            phase: Workflow phase (analyze, design, implement, test, review, security)

        Returns:
            Agent type string
        """
        # Phase to agent mapping (MoAI-ADK style)
        phase_agent_map = {
            "analyze": AgentSpec.PM.value,
            "design": AgentSpec.ARCHITECT.value,
            "implement": AgentSpec.DEV.value,
            "test": AgentSpec.QA.value,
            "review": AgentSpec.REVIEWER.value,
            "security-review": AgentSpec.SECURITY.value,
            "plan-audit": AgentSpec.PLAN_AUDITOR.value,
            "sync-audit": AgentSpec.SYNC_AUDITOR.value,
        }

        return phase_agent_map.get(phase, AgentSpec.DEV.value)

    def dispatch_agent(
        self,
        agent_type: AgentSpec,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Dispatch an agent with the given type and context.

        This is the main dispatch method used by the auto-engine.
        In a real implementation, this would spawn a Claude Code agent.

        Args:
            agent_type: Type of agent to dispatch
            context: Execution context

        Returns:
            Agent result dictionary or None
        """
        # Log dispatch intent
        self._log_dispatch({
            "action": "dispatch_agent",
            "agent_type": agent_type.value if isinstance(agent_type, AgentSpec) else agent_type,
            "context_keys": list(context.keys()),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })

        # In a real implementation, this would:
        # 1. Build the agent prompt from context
        # 2. Spawn the agent via Agent tool
        # 3. Wait for completion
        # 4. Return the result

        # For now, return a mock result
        return {
            "status": "completed",
            "output": f"Agent {agent_type} executed with context: {list(context.keys())}",
            "evidence": [],
            "agent_type": agent_type.value if isinstance(agent_type, AgentSpec) else agent_type,
        }

    def build_dispatch_context(
        self,
        spec_path: Optional[str] = None,
        workflow_path: Optional[str] = None,
        issue_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build execution context for agent dispatch.

        Args:
            spec_path: Path to SPEC document
            workflow_path: Path to workflow plan
            issue_id: Issue identifier
            run_id: Run identifier

        Returns:
            Context dictionary
        """
        context = {
            "harness_root": self.harness_root,
        }

        # Load SPEC if provided
        if spec_path and os.path.exists(spec_path):
            try:
                with open(spec_path, "r") as f:
                    context["spec_content"] = f.read()
                context["spec_path"] = spec_path
            except IOError:
                pass

        # Load workflow if provided
        if workflow_path and os.path.exists(workflow_path):
            try:
                with open(workflow_path, "r") as f:
                    if workflow_path.endswith(".json"):
                        context["workflow"] = json.load(f)
                    else:
                        context["workflow"] = f.read()
                context["workflow_path"] = workflow_path
            except IOError:
                pass

        # Add identifiers
        if issue_id:
            context["issue_id"] = issue_id
        if run_id:
            context["run_id"] = run_id

        return context

    def _log_dispatch(self, entry: Dict[str, Any]) -> None:
        """Log dispatch event to JSONL file.

        Args:
            entry: Log entry
        """
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.dispatch_log_path), exist_ok=True)

            # Append to log
            with open(self.dispatch_log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except IOError:
            pass  # Don't fail if logging fails


def dispatch_agent_for_step(
    step_id: str,
    phase: str,
    harness_root: str,
    context: Optional[Dict[str, Any]] = None,
) -> DispatchResult:
    """Convenience function to dispatch agent for a step.

    Args:
        step_id: Workflow step identifier
        phase: Workflow phase
        harness_root: Path to harness root
        context: Optional execution context

    Returns:
        DispatchResult
    """
    dispatcher = AgentDispatcher(harness_root)

    # Get agent type for phase
    agent_type = dispatcher.get_agent_for_phase(phase)

    # Build context
    if context is None:
        context = dispatcher.build_dispatch_context()

    # Dispatch
    return dispatcher.dispatch_for_step(step_id, agent_type, context)


# Integration with Claude Code Agent system

class AgentSpawnConfig:
    """Configuration for spawning Claude Code agents."""

    def __init__(
        self,
        agent_type: str,
        prompt: str,
        tools: Optional[List[str]] = None,
        model: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        self.agent_type = agent_type
        self.prompt = prompt
        self.tools = tools or []
        self.model = model
        self.context = context or {}

    def to_spawn_args(self) -> Dict[str, Any]:
        """Convert to arguments for Agent spawning."""
        return {
            "subagent_type": self.agent_type,
            "prompt": self.prompt,
            "tools": self.tools,
            "model": self.model,
        }


def spawn_agent_for_phase(
    phase: str,
    context: Dict[str, Any],
    harness_root: str,
) -> Tuple[bool, str]:
    """Spawn an agent for a workflow phase.

    In a real implementation, this would call the Agent tool.
    For now, it logs the intent and returns success.

    Args:
        phase: Workflow phase
        context: Execution context
        harness_root: Harness root directory

    Returns:
        (ok, message) tuple
    """
    dispatcher = AgentDispatcher(harness_root)
    agent_type = dispatcher.get_agent_for_phase(phase)

    # Build prompt based on phase and context
    prompt = f"""Execute {phase} phase with the following context:

{json.dumps(context, indent=2)}

Follow the workflow steps and capture required evidence.
"""

    # In real implementation:
    # agent = Agent(subagent_type=agent_type, prompt=prompt)
    # result = agent.run()

    # For now, log and return success
    dispatcher._log_dispatch({
        "action": "spawn_intent",
        "phase": phase,
        "agent_type": agent_type,
        "context_keys": list(context.keys()),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })

    return True, f"Would spawn {agent_type} agent for {phase} phase"
