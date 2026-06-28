"""Laplace component system for modular workflow phases.

This package provides a component-based architecture inspired by LazyCodex's
workspace pattern, adapted for Laplace's pure Python stdlib approach.

Components are independently discoverable modules that implement specific
phases of the development workflow:
    - intent: Requirement clarification (Phase 0)
    - workflow: Workflow planning (Phase 1)
    - execute: Implementation execution (Phase 2)
    - verify: Independent auditors (Phase 3)
    - release: Release automation (Phase 4)

Each component maintains its own state under .harness/components/<name>/.

Example:
    from components import registry
    components = registry.discover()
    for component in components:
        component.initialize()
        result = component.execute(context)
        component.cleanup()
"""

__version__ = "0.1.0"

# Import registry for convenience
from . import registry  # noqa: F401

__all__ = ["registry"]
