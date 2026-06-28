# Workflow Templates Guide

Laplace의 워크플로우 템플릿 시스템을 사용하여 자주 사용되는 개발 패턴을 표준화하는 방법을 안내합니다.

## Overview

워크플로우 템플릿은 특정 유형의 작업에 대해 사전 정의된 단계, 품질 게이트, 증거 요구사항을 제공합니다. LazyCodex의 success criteria 패턴과 MoAI-ADK의 multi-phase 패턴을 차용했습니다.

## Available Templates

| Template | Use Case | Complexity | Duration | Orchestration |
|----------|----------|------------|----------|---------------|
| **feature** | New features | High | Days | workflow |
| **bug-fix** | Bug fixes | Medium | Hours | team |
| **security-fix** | Security vulnerabilities | High | Days | workflow |
| **chore** | Small tasks | Low | Minutes | single |
| **refactoring** | Code restructuring | Medium | Hours | team |
| **performance** | Performance optimization | Medium | Hours | team |
| **dependency-update** | Third-party updates | Medium | Hours | team |

## Template Phases

### Standard Phases

```
intent → plan → plan-audit → implement → test → review → security-review → sync-audit → release
```

### Template Comparison

```
feature:    intent → plan → plan-audit → implement → test → review → sync-audit
bug-fix:    intent → plan → implement → test → review → sync-audit
            (no plan-audit for speed)

security:   intent → plan → plan-audit → implement → test → security-review → sync-audit
            (mandatory security review)

chore:      implement → test
            (minimal workflow)
```

## Using Templates

### Programmatic Usage

```python
from components.workflow.templates import get_template, list_templates

# List available templates
templates = list_templates()
print(f"Available templates: {templates}")

# Get a specific template
feature = get_template("feature")
if feature:
    print(f"Feature workflow: {feature.description}")
    print(f"Phases: {feature.phases}")
```

### Applying Templates to Issues

Add to your issue frontmatter:

```yaml
---
type: feature
workflow: feature
priority: high
---

## Feature: User Authentication

Implement OAuth2 login flow.
```

The workflow is automatically selected based on the `workflow` key or inferred from `type`.

## Customizing Templates

### Creating a Custom Template

```python
from components.workflow.templates import (
    WorkflowDefinition,
    WorkflowPhase,
    QualityGate,
    EvidenceRequirement
)

# Define custom workflow
custom_workflow = WorkflowDefinition(
    name="my-custom",
    description="My project-specific workflow",
    phases=[
        "intent",
        "plan",
        "implement",
        "test",
        "sync-audit",
    ],
    gates=[
        QualityGate(
            from_phase="plan",
            to_phase="implement",
            required_evidence=[
                EvidenceRequirement(
                    kind="workflow-plan",
                    description="Implementation plan documented"
                ),
            ],
            auditor="plan",
            description="Plan must be approved before implementation"
        ),
        QualityGate(
            from_phase="test",
            to_phase="sync-audit",
            required_evidence=[
                EvidenceRequirement(
                    kind="test",
                    min_count=1,
                    description="Unit tests pass"
                ),
                EvidenceRequirement(
                    kind="integration-test",
                    description="Integration tests pass"
                ),
            ],
            auditor="sync",
            description="All tests must pass before final validation"
        ),
    ],
    orchestration_mode="team",
    metadata={"complexity": "medium", "estimated_duration": "hours"}
)
```

### Saving Custom Templates

```python
from components.workflow.templates import save_template_to_file

# Save to file for reuse
save_template_to_file(custom_workflow, ".harness/templates/my-custom.json")
```

### Loading Custom Templates

```python
from components.workflow.templates import load_template_from_file

# Load from file
custom = load_template_from_file(".harness/templates/my-custom.json")
if custom:
    print(f"Loaded custom workflow: {custom.name}")
```

## Evidence Requirements

Each gate defines required evidence kinds:

### Standard Evidence Kinds

| Kind | Description | Required For |
|------|-------------|--------------|
| `test` | Unit test execution | Most workflows |
| `integration-test` | Integration tests | Feature, security |
| `review` | Code review findings | All except chore |
| `security` | Security review results | Security-sensitive |
| `spec-validation` | SPEC document validation | Feature workflow |
| `workflow-plan` | Implementation plan | Feature workflow |
| `metric-capture` | Performance metrics | Performance workflow |
| `audit-report` | Auditor verdicts | Plan/Sync audits |
| `reproduction` | Bug reproduction steps | Bug fixes |

### Evidence Requirement Properties

```python
EvidenceRequirement(
    kind="test",              # Evidence kind
    required=True,            # Whether it's mandatory
    min_count=1,              # Minimum number of entries
    description="Unit tests"  # Human-readable description
)
```

## Quality Gates

Gates enforce requirements between phases:

### Gate Properties

| Property | Type | Description |
|----------|------|-------------|
| `from_phase` | string | Source phase |
| `to_phase` | string | Target phase |
| `required_evidence` | list | Evidence requirements |
| `auditor` | string | Auditor to run (plan, sync, or None) |
| `block_on_fail` | bool | Whether to block on failure |
| `description` | string | Human-readable description |

### Gate Behavior

- **With auditor**: Auditor spawns with fresh context, returns PASS/FAIL/INCONCLUSIVE
- **Without auditor**: Only checks evidence presence
- **Block on fail**: Transition blocked if gate fails
- **No block**: Gate runs but doesn't block (advisory)

## Workflow Selection Logic

Automatic selection based on issue metadata:

```python
# In routing-rules.yml or issue frontmatter:
workflow_rules:
  - type: feature
    template: feature

  - type: bug
    template: bug-fix

  - type: security
    template: security-fix

  - type: chore
    template: chore

  # Default fallback
  - template: feature
```

## Orchestration Modes

Each template specifies an orchestration mode:

| Mode | Description | Use Case |
|------|-------------|----------|
| `single` | One agent, sequential | Small tasks, chores |
| `team` | Different agents per phase | Bug fixes, refactoring |
| `workflow` | Full multi-phase with auditors | Features, security |
| `parallel` | Multiple issues concurrently | Multiple similar tasks |

## Examples

### Feature Development

```yaml
---
workflow: feature
---

## Add User Profile

Implement user profile page with avatar upload.
```

**Flow**: intent → plan → **plan-audit** → implement → test → review → **sync-audit**

**Gates**:
- Plan audit: Plan must be complete
- Test gate: All tests pass
- Sync audit: AC verified

### Bug Fix

```yaml
---
workflow: bug-fix
---

## Fix Login Redirect

Login doesn't redirect properly after authentication.
```

**Flow**: intent → plan → implement → test → review → **sync-audit**

**Gates**:
- Test gate: Fix validated with tests
- Sync audit: No regressions

### Security Fix

```yaml
---
workflow: security-fix
---

## Fix SQL Injection

User input not properly sanitized in search query.
```

**Flow**: intent → plan → **plan-audit** → implement → test → **security-review** → **sync-audit**

**Gates**:
- Plan audit: Security fix planned
- Test gate: Security tests pass
- Security review: No vulnerabilities
- Sync audit: Double verification

### Performance Optimization

```yaml
---
workflow: performance
---

## Optimize Database Queries

Reduce N+1 queries in dashboard loading.
```

**Flow**: intent → plan → implement → test → **sync-audit**

**Gates**:
- Test gate: Performance metrics captured
- Sync audit: Improvement verified

## Best Practices

1. **Choose Appropriate Template**: Match template to work complexity
2. **Respect Gates**: Don't bypass quality gates
3. **Capture Evidence**: Record all required evidence
4. **Update Estimates**: Adjust `estimated_duration` based on experience
5. **Customize Wisely**: Extend templates rather than modifying core ones

## Template Maintenance

### Updating Core Templates

To update a core template:

1. Copy the template definition
2. Modify as needed
3. Save as custom template in `.harness/templates/`
4. Reference in routing rules

### Versioning

Templates can be versioned:

```python
custom_workflow_v2 = WorkflowDefinition(
    name="my-custom-v2",
    version="2.0",
    # ...
)
```

### Deprecation

Mark deprecated templates:

```python
DEPRECATED_TEMPLATES = ["old-workflow"]
```

## Integration with Routing Rules

Templates integrate with `.harness/routing-rules.yml`:

```yaml
workflow_routing:
  default_template: feature

  type_mapping:
    feature: feature
    bug: bug-fix
    security: security-fix
    chore: chore
    refactor: refactoring
    perf: performance
    deps: dependency-update

  overrides:
    - condition: "files_changed > 50"
      template: feature  # Force full workflow for large changes

    - condition: "security_sensitive == true"
      template: security-fix  # Force security workflow
```

## References

- Template definitions: `components/workflow/templates.py`
- Workflow planner: `components/workflow/planner.py`
- Auditor guide: `components/verify/AUDITOR_GUIDE.md`
