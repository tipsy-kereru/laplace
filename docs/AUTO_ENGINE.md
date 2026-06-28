# Laplace Auto-Execution Engine Guide

**Version:** 0.9.0+

The Auto-Execution Engine enables fully automated workflow execution from PRD to completion. This guide covers how to use, configure, and extend the auto-execution system.

## Overview

The auto-execution engine implements the conductor-workers pattern from LazyCodex and multi-phase workflows from MoAI-ADK:

```
PRD → SPEC → Workflow → Agent Execution → Completion
```

### What it does

1. **PRD → SPEC**: Automatically generates GEARS-formatted SPEC documents
2. **SPEC → Workflow**: Creates executable workflow plans with evidence requirements
3. **Workflow → Agents**: Dispatches specialized agents for each phase
4. **Completion Detection**: Multi-dimensional completion detection (Signal, Evidence, Convergence, State Machine)

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Auto-Execution Engine                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ Spec Parser  │───>│ Workflow Gen │───>│ Agent Dispatcher ││
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│         │                   │                   │            │
│         v                   v                   v            │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │                   State Machine                           │ │
│  │  intent → plan → plan-audit → run → sync-audit → done  │ │
│  └──────────────────────────────────────────────────────────┘ │
│         │                   │                   │            │
│         v                   v                   v            │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ Ledger Writer │    │Evidence Capt. │    │Completion Det.│  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### Method 1: Step-by-step

```bash
# 1. Generate SPEC from PRD
/laplace:spec-gen docs/prd-feature.md
# Output: SPEC saved to .harness/specs/SPEC-20260628-120000.md

# 2. Generate workflow from SPEC
/laplace:workflow-gen .harness/specs/SPEC-20260628-120000.md
# Output: Workflow plan saved to .harness/workflows/PLAN-20260628-120000.md

# 3. Run auto-execution
python3 scripts/auto_engine.py docs/prd-feature.md --target . --max-iterations 12 -v
```

### Method 2: One-shot

```bash
# The auto-engine handles all phases automatically
python3 scripts/auto_engine.py docs/prd-feature.md --max-iterations 12 -v
```

## Usage

### Command-line Options

```bash
python3 scripts/auto_engine.py <prd.md> [options]

Options:
  --target <dir>     Harness root directory (default: current directory)
  --max-iterations N Maximum iterations (default: 12)
  -v, --verbose      Enable verbose logging
```

### Example Output

```
[Laplace] Starting auto-execution from PRD: docs/prd-feature.md
[Laplace] Phase 1: Generating SPEC from PRD...
[Laplace] ✓ SPEC generated: SPEC-20260628-120000
[Laplace] Phase 2: Generating workflow from SPEC...
[Laplace] ✓ Workflow generated: PLAN-20260628-120000
[Laplace] Phase 3: Executing workflow...
[Laplace] Executing 4 workflow steps...
[Laplace]
--- Iteration 1/12 ---
[Laplace] Current step: Analyze Requirements (phase: analyze)
[Laplace] ✓ Agent completed: completed
[Laplace] Completion check: Completion detection: ✗ signal, ✗ evidence, ✗ convergence, ✗ state_machine (confidence: 20%)
...
[Laplace] ✓ Workflow complete!

============================================================
Laplace Auto-Execution Result
============================================================
{
  "success": true,
  "completed": true,
  "iterations": 5,
  "evidence_count": 0,
  "completion": {
    "is_complete": true,
    "confidence": 1.0,
    "dimensions": {
      "signal": false,
      "evidence": false,
      "convergence": false,
      "state_machine": true
    },
    "reasoning": "Completion detection: ✓ state_machine (confidence: 100%)"
  }
}
```

## Components

### 1. Spec Generator

**Location:** `components/intent/spec_generator.py`

Generates GEARS-formatted SPEC documents from PRDs:

- **Given**: Requirements from PRD
- **Effect**: Technical approach and design decisions
- **Acceptance**: Criteria for validation
- **Risks**: Potential blockers and mitigations
- **Steps**: Implementation plan

**Usage:**
```bash
/laplace:spec-gen docs/prd.md --target .
```

### 2. Workflow Generator

**Location:** `components/workflow/auto_generator.py`

Creates executable workflow plans from SPECs:

- Step-by-step implementation plan
- Evidence collection points
- Quality gate definitions
- Risk identification

**Usage:**
```bash
/laplace:workflow-gen .harness/specs/SPEC-*.md --target .
```

### 3. Agent Dispatcher

**Location:** `components/dispatcher/dispatcher.py`

Dispatches specialized agents for workflow phases:

| Phase | Agent Type | Role |
|-------|-----------|------|
| analyze | PM | Requirements clarification |
| design | Architect | Technical design |
| implement | Dev | Implementation |
| test | QA | Testing and validation |
| review | Reviewer | Code review |
| security | Security Auditor | Security review |

### 4. Completion Detector

**Location:** `components/engine/completion.py`

Multi-dimensional completion detection:

- **Signal**: Explicit completion messages in transcript
- **Evidence**: Required evidence captured
- **Convergence**: Agent results stabilized
- **State Machine**: All phases complete

## Configuration

### Iteration Limits

The auto-engine uses bounded iterations (default: 12):

```bash
python3 scripts/auto_engine.py prd.md --max-iterations 20
```

### Completion Signals

Customize completion detection by editing `components/engine/completion.py`:

```python
self.completion_signals = [
    "COMPLETE", "DONE", "FINISHED", "SUCCESS",
    "LAPLACE-P0P6-COMPLETE",  # Add custom signals
]
```

### Required Evidence

Specify required evidence kinds:

```python
self.required_evidence = [
    "test", "review", "audit-report",
    "integration-test",  # Add custom kinds
]
```

## Advanced Usage

### Custom Agent Dispatch

Extend the dispatcher for custom agent types:

```python
from components.dispatcher import AgentDispatcher, AgentSpec

dispatcher = AgentDispatcher(harness_root)

# Custom dispatch logic
result = dispatcher.dispatch_agent(
    agent_type=AgentSpec.DEV,
    context={
        "spec": spec_content,
        "step": workflow_step,
        "custom_param": value,
    }
)
```

### Completion Callbacks

Monitor completion detection:

```python
from components.engine import CompletionDetector

detector = CompletionDetector(harness_root)

# Check completion
result = detector.detect_completion(
    transcript=agent_output,
    evidence=captured_evidence,
    current_phase="implement",
    total_phases=["analyze", "design", "implement", "test", "review"],
)

if result.is_complete:
    print(f"Complete with {result.confidence:.0%} confidence")
    print(f"Reasoning: {result.reasoning}")
```

## Integration with Manual Loop

The auto-engine complements the manual loop workflow:

### Manual Workflow

```bash
/laplace:intake prd.md
/laplace:approve ISSUE-001
/laplace:run ISSUE-001
```

### Auto Workflow

```bash
python3 scripts/auto_engine.py prd.md
```

### Hybrid Approach

```bash
# Generate SPEC and workflow automatically
/laplace:spec-gen prd.md
/laplace:workflow-gen .harness/specs/SPEC-*.md

# Review and approve manually
/laplace:intake prd.md
/laplace:approve ISSUE-001

# Run with auto-generated workflow
/laplace:run ISSUE-001
```

## Troubleshooting

### Issue: SPEC generation fails

**Symptom:** `SPEC generation failed: ...`

**Solution:**
- Verify PRD markdown format
- Check that required sections are present
- Ensure harness directory is writable

### Issue: Workflow generation fails

**Symptom:** `Workflow generation failed: ...`

**Solution:**
- Verify SPEC document exists and is valid
- Check SPEC has required sections (plan, acceptance)
- Ensure harness directory is writable

### Issue: Agent dispatch fails

**Symptom:** `Agent dispatch failed: ...`

**Solution:**
- Verify agent type is valid
- Check context provides required fields
- Ensure dispatcher log path is writable

### Issue: Completion not detected

**Symptom:** Max iterations reached without completion

**Solution:**
- Increase `--max-iterations`
- Add completion signals to agent output
- Verify state machine reaches terminal phase

## Limitations

### Current Mock Agents

The v0.9.0 release uses mock agents that simulate execution:

```python
# Current implementation
return {
    "status": "completed",
    "output": f"Agent {agent_type} executed with context: {list(context.keys())}",
    "evidence": [],
}
```

### Real Agent Integration

To integrate real Claude Code agents, modify `scripts/auto_engine.py`:

```python
# Replace mock dispatch with real Agent tool
from claude_code import Agent

agent = Agent(
    subagent_type=agent_type.value,
    prompt=context["step"]["description"],
)
result = agent.run()
```

## Performance

### Iteration Behavior

- **Default**: 12 iterations (configurable)
- **Convergence Detection**: Stabilizes after N identical results
- **State Machine Check**: Completes when all phases done

### Scalability

- **Small PRDs** (< 10 acceptance criteria): ~3-5 iterations
- **Medium PRDs** (10-30 criteria): ~5-8 iterations
- **Large PRDs** (> 30 criteria): ~8-12 iterations

## References

- **LazyCodex ULW-Loop**: Evidence-driven loops with conductor-workers
- **MoAI-ADK**: Multi-phase workflows with checkpoint recovery
- **Ralph Loop**: Fail-safe design with bounded iterations
- **Claude Code goal/ultrawork**: Convergence detection and evaluation agents

## See Also

- [README.md](../README.md) - Project overview
- [USAGE.md](USAGE.md) - Manual workflow guide
- Component guides:
  - [components/intent/spec_generator.py](../components/intent/spec_generator.py)
  - [components/workflow/auto_generator.py](../components/workflow/auto_generator.py)
  - [components/dispatcher/dispatcher.py](../components/dispatcher/dispatcher.py)
  - [components/engine/completion.py](../components/engine/completion.py)
