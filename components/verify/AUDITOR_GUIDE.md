# Custom Auditor Guide

Laplace의 Auditor 시스템을 사용하여 프로젝트 특화된 검증 규칙을 정의하는 방법을 안내합니다.

## Overview

Auditor는 워크플로우의 중요한 지점에서 독립적으로 검증을 수행하는 컴포넌트입니다. MoAI-ADK 패턴을 차용하여 **fresh context**로 실행되어 편향 없는 검증을 보장합니다.

## When to Use Auditors

Auditor는 다음 상황에서 유용합니다:

- **Quality Gates**: 코드 병합 전 품질 기준 강제
- **Security Checks**: 보안 취약점 사전 검증
- **Compliance**: 규제 요구사항 준수 확인
- **Performance**: 성능 기준 충족 검증
- **Custom Rules**: 프로젝트 특화 규칙 자동화

## Auditor Architecture

```
┌─────────────────┐
│   Workflow      │
│                 │
│  ┌───────────┐  │    ┌──────────────┐
│  │   Plan    │──┼───>│ Plan-Auditor│ (Phase 1 → 2)
│  └───────────┘  │    └──────────────┘
│                 │
│  ┌───────────┐  │    ┌──────────────┐
│  │  Execute  │──┼───>│ Sync-Auditor │ (Phase 2 → 3)
│  └───────────┘  │    └──────────────┘
└─────────────────┘
```

## Creating a Custom Auditor

### Step 1: Define Your Auditor Class

```python
from components.verify.template import AuditorBase, AuditResult, AuditFinding

class MyAuditor(AuditorBase):
    def __init__(self):
        super().__init__(
            name="my-auditor",
            description="Description of what this auditor checks"
        )

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []

        # Your audit logic here
        # ...

        return AuditResult(
            verdict="PASS",  # or "FAIL", "INCONCLUSIVE"
            reasoning="Explanation of the verdict",
            findings=findings,
            metadata={"key": "value"}  # Optional metadata
        )
```

### Step 2: Implement Audit Logic

```python
def audit(self, context: Dict[str, Any]) -> AuditResult:
    findings = []
    harness_root = context.get("harness_root", "")
    issue_id = context.get("issue_id", "")

    # Check specific conditions
    if self._check_violation(harness_root):
        findings.append(AuditFinding(
            severity="high",  # critical, high, medium, low, info
            category="correctness",
            location="path/to/file.py",
            description="Description of the issue",
            recommendation="How to fix it"
        ))

    # Determine verdict
    verdict = "FAIL" if any(f.severity in ["critical", "high"] for f in findings) else "PASS"

    return AuditResult(
        verdict=verdict,
        reasoning=f"Audit complete with {len(findings)} findings",
        findings=findings
    )
```

### Step 3: Register Your Auditor

Add to `components/verify/__init__.py`:

```python
from .my_auditor import MyAuditor

__all__ = ["PlanAuditorComponent", "SyncAuditorComponent", "MyAuditor"]
```

## Verdict Guidelines

| Verdict | When to Use | Behavior |
|---------|-------------|----------|
| **PASS** | All checks pass, no blocking issues | Transition allowed |
| **FAIL** | Critical/high severity issues found | Transition blocked |
| **INCONCLUSIVE** | Cannot determine (missing data, errors) | Logged, does not block |

## Severity Levels

| Severity | Impact on Verdict | Use Case |
|----------|-------------------|----------|
| **critical** | Always blocks FAIL | Security vulnerabilities, data loss |
| **high** | Blocks FAIL (default) | Breaking changes, API violations |
| **medium** | Blocks FAIL (strict mode) | Performance issues, quality concerns |
| **low** | Does not block | Style, documentation |
| **info** | Does not block | Suggestions, optimizations |

## Integration Points

### Plan Audit (pm-review → ready-for-dev)

Use for validating implementation plans:

```python
# Called by runner.py before pm-review → ready-for-dev transition
def _call_plan_auditor(issue_id, plan_path, harness_root, run_id):
    auditor = PlanAuditorComponent()
    result = auditor.execute({"issue_id": issue_id, "plan_path": plan_path})
    return result["success"], result.get("error", "")
```

### Sync Audit (security-review → review-passed)

Use for validating implementation results:

```python
# Called by runner.py before security-review → review-passed transition
def _call_sync_auditor(issue_id, run_id, harness_root):
    auditor = SyncAuditorComponent()
    result = auditor.execute({"issue_id": issue_id, "run_id": run_id})
    return result["success"], result.get("error", "")
```

## Example: Code Style Auditor

```python
import re
from components.verify.template import AuditorBase, AuditResult, AuditFinding

class CodeStyleAuditor(AuditorBase):
    """Enforces code style conventions."""

    def __init__(self, max_line_length=100):
        super().__init__(
            name="code-style-auditor",
            description="Checks code style and formatting"
        )
        self.max_line_length = max_line_length

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []
        harness_root = context.get("harness_root", "")

        # Check Python files for line length violations
        for root, dirs, files in os.walk(harness_root):
            for file in files:
                if file.endswith('.py'):
                    filepath = os.path.join(root, file)
                    findings.extend(self._check_file(filepath))

        # Style violations don't block (low severity)
        return AuditResult(
            verdict="PASS",  # Always PASS for style
            reasoning=f"Style check: {len(findings)} findings",
            findings=findings
        )

    def _check_file(self, filepath: str) -> List[AuditFinding]:
        findings = []
        with open(filepath, 'r') as f:
            for i, line in enumerate(f, 1):
                if len(line) > self.max_line_length:
                    findings.append(AuditFinding(
                        severity="low",
                        category="style",
                        location=f"{filepath}:{i}",
                        description=f"Line exceeds {self.max_line_length} characters",
                        recommendation="Break line or use continuation"
                    ))
        return findings
```

## Example: API Contract Auditor

```python
class APIContractAuditor(AuditorBase):
    """Validates API contract compliance."""

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        findings = []
        harness_root = context.get("harness_root", "")

        # Check for breaking API changes
        findings.extend(self._check_breaking_changes(harness_root))

        # Check for proper error handling
        findings.extend(self._check_error_handling(harness_root))

        # API violations block deployment
        verdict = "FAIL" if any(
            f.severity in ["critical", "high"] for f in findings
        ) else "PASS"

        return AuditResult(
            verdict=verdict,
            reasoning=f"API contract audit: {len(findings)} findings",
            findings=findings
        )
```

## Testing Your Auditor

```python
# Test your auditor in isolation
from components.verify.my_auditor import MyAuditor

auditor = MyAuditor()
result = auditor.audit({
    "issue_id": "TEST-001",
    "run_id": "test-run-001",
    "harness_root": "/path/to/harness"
})

print(f"Verdict: {result.verdict}")
print(f"Reasoning: {result.reasoning}")
for finding in result.findings:
    print(f"  - {finding.severity}: {finding.description}")
```

## Best Practices

1. **Fresh Context**: Auditors spawn with clean context - no bias from prior phases
2. **Read-Only**: Prefer analysis over modification
3. **Clear Findings**: Each finding should have clear description and recommendation
4. **Appropriate Severity**: Use severity levels consistently
5. **Fail-Safe**: Auditor failures should not block workflows (return INCONCLUSIVE)

## Advanced Patterns

### Composition

Multiple auditors can be chained:

```python
class CompositeAuditor(AuditorBase):
    def __init__(self, auditors: List[AuditorBase]):
        super().__init__("composite", "Runs multiple auditors")
        self.auditors = auditors

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        all_findings = []
        for auditor in self.auditors:
            result = auditor.audit(context)
            all_findings.extend(result.findings)

        return AuditResult(
            verdict=get_verdict_from_findings(all_findings),
            reasoning=f"Composite: {len(all_findings)} total findings",
            findings=all_findings
        )
```

### Configurable Thresholds

```python
class ThresholdAuditor(AuditorBase):
    def __init__(self, threshold: float):
        super().__init__("threshold", "Checks against threshold")
        self.threshold = threshold

    def audit(self, context: Dict[str, Any]) -> AuditResult:
        value = self._measure_metric(context)
        if value < self.threshold:
            return AuditResult(
                verdict="FAIL",
                reasoning=f"Metric {value} below threshold {self.threshold}",
                findings=[AuditFinding(...)]
            )
        return AuditResult(verdict="PASS", reasoning="Threshold met", findings=[])
```

## References

- Template: `components/verify/template.py`
- Examples: `components/verify/examples.py`
- Plan Auditor: `components/verify/plan_auditor.py`
- Sync Auditor: `components/verify/sync_auditor.py`
