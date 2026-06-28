"""Agent dispatcher: Automatic agent dispatch for workflow execution.

This component provides automatic agent dispatch based on workflow phases,
implementing the conductor-workers pattern from LazyCodex and the
multi-agent system from MoAI-ADK.

Agent types:
- pm: Project management and requirements clarification
- architect: Technical design and architecture
- dev: Implementation and coding
- qa: Testing and quality assurance
- reviewer: Code review and validation
- security: Security-focused review

Inspired by LazyCodex's conductor-workers pattern and MoAI-ADK's agent catalog.
"""

from .dispatcher import (
    AgentDispatcher,
    DispatchResult,
    AgentSpec,
    DispatchStatus,
    dispatch_agent_for_step,
    spawn_agent_for_phase,
)

# Alias for compatibility with auto_engine
AgentType = AgentSpec

__all__ = [
    "AgentDispatcher",
    "DispatchResult",
    "AgentSpec",
    "AgentType",  # Alias
    "DispatchStatus",
    "dispatch_agent_for_step",
    "spawn_agent_for_phase",
]
