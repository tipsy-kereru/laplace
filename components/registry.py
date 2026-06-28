"""Component registry and discovery for Laplace component system.

Provides automatic component discovery and registration using Python's
stdlib importlib. Components are discovered from the components/ package.

Usage:
    from components import registry
    components = registry.discover()
    workflow_component = registry.get("workflow")
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from . import base


# Registry storage
_components: Dict[str, Type[base.Component]] = {}


def register(component_class: Type[base.Component]) -> None:
    """Register a component class.

    Args:
        component_class: Component class to register
    """
    if not component_class.name:
        raise ValueError(f"Component class {component_class.__name__} must have a 'name' attribute")

    _components[component_class.name] = component_class


def get(name: str) -> Optional[Type[base.Component]]:
    """Get a component class by name.

    Args:
        name: Component name

    Returns:
        Component class, or None if not found
    """
    return _components.get(name)


def list_all() -> Dict[str, Type[base.Component]]:
    """List all registered components.

    Returns:
        Dictionary mapping component names to component classes
    """
    return _components.copy()


def discover() -> List[Type[base.Component]]:
    """Discover and register all components in the components/ package.

    Searches each component subdirectory for a module that exports a
    Component subclass and registers it.

    Returns:
        List of discovered component classes
    """
    discovered = []

    # Get the components package directory
    components_dir = Path(__file__).parent

    # Skip __pycache__ and the base files
    skipped = {"__pycache__", "__init__.py", "base.py", "registry.py"}

    for item in components_dir.iterdir():
        if item.name in skipped or item.name.startswith("."):
            continue

        component_path = components_dir / item.name

        # Check if it's a component directory (has __init__.py)
        if component_path.is_dir():
            init_file = component_path / "__init__.py"
            if not init_file.exists():
                continue

            # Try to import the module
            module_name = f"components.{item.name}"
            try:
                module = importlib.import_module(module_name)

                # Look for Component subclasses in the module
                for attr_name in dir(module):
                    if attr_name.startswith("_"):
                        continue

                    attr = getattr(module, attr_name)

                    # Check if it's a Component subclass (but not Component itself)
                    if (isinstance(attr, type) and
                            issubclass(attr, base.Component) and
                            attr is not base.Component):
                        # Register the component
                        if attr.name:  # Has a name attribute
                            register(attr)
                            discovered.append(attr)

            except ImportError as e:
                # Log but don't fail - component might have dependencies
                print(f"Warning: Could not import component {item.name}: {e}")

    return discovered


def create(name: str, **kwargs) -> Optional[base.Component]:
    """Create a component instance by name.

    Args:
        name: Component name
        **kwargs: Arguments to pass to component constructor

    Returns:
        Component instance, or None if component not found
    """
    component_class = get(name)
    if component_class is None:
        return None

    return component_class(**kwargs)


def initialize_all(harness_root: str, config: Optional[Dict] = None) -> Dict[str, bool]:
    """Initialize all registered components.

    Args:
        harness_root: Path to .harness/ directory
        config: Optional global configuration

    Returns:
        Dictionary mapping component names to initialization success
    """
    results = {}

    for name, component_class in _components.items():
        try:
            instance = component_class()
            success = instance.initialize(harness_root, config)
            results[name] = success
        except Exception as e:
            print(f"Error initializing component {name}: {e}")
            results[name] = False

    return results


def get_component_phases() -> Dict[str, str]:
    """Get mapping of component names to workflow phases.

    Returns:
        Dictionary mapping component names to their phases
    """
    phases = {}

    for name, component_class in _components.items():
        instance = component_class()
        if instance.phase:
            phases[name] = instance.phase

    return phases


# Auto-discover on import
_discovered = discover()
