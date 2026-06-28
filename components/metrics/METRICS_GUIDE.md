# Metrics Collection Guide

Laplace의 메트릭 수집 시스템을 사용하여 성능과 품질 지표를 캡처하고 분석하는 방법을 안내합니다.

## Overview

메트릭 시스템은 다음을 제공합니다:
- **MetricCollector**: 다양한 소스에서 메트릭 수집
- **MetricAnalyzer**: 기준선 비교 및 임계값 검증
- **MetricCollection**: 메트릭 그룹화 및 evidence 변환

## When to Use Metrics

Metrics는 다음 상황에서 유용합니다:
- **Performance Optimization**: 성능 개선 검증
- **Quality Gates**: 품질 기준 충족 확인
- **Trend Analysis**: 시간 경과에 따른 추적
- **SLA Compliance**: 서비스 수준 협약 준수 확인

## Basic Usage

### Collecting Metrics

```python
from components.metrics.collector import MetricCollector, MetricCollection

# Create collector
collector = MetricCollector(harness_root="/path/to/project")

# Collect from command
test_metric = collector.collect_from_command(
    command=["npm", "test", "--", "--coverage"],
    metric_name="coverage",
    unit="%",
    parse_func=parse_coverage_json,
)

# Create collection
collection = MetricCollection(
    run_id="run-001",
    issue_id="ISSUE-001",
    collection_type="performance",
)

# Add metrics
if test_metric:
    collection.add(test_metric)

# Get summary for evidence
summary = collection.get_summary()
print(summary)  # "performance metrics: coverage=85.5%, build_time=1234ms"
```

### Capturing as Evidence

```python
from scripts.runner import capture_metrics

# Capture metrics as evidence
metrics = {
    "coverage": 85.5,
    "build_time": 1234,
    "bundle_size": 450000,
}

ok, reason = capture_metrics(
    run_id="run-001",
    metrics=metrics,
    target="/path/to/project",
)

if ok:
    print(f"Metrics captured: {reason}")
```

## Metric Sources

### Command Execution

명령어를 실행하고 결과를 파싱:

```python
metric = collector.collect_from_command(
    command=["pytest", "--cov", "--cov-report=json"],
    metric_name="coverage",
    unit="%",
    parse_func=lambda out: json.loads(out).get("totals", {}).get("percent_covered", 0),
)
```

### File Reading

파일에서 메트릭 읽기:

```python
metric = collector.collect_from_file(
    filepath="coverage/coverage.json",
    metric_name="coverage",
    unit="%",
    parse_func=parse_coverage_json,
)
```

### JSON Path Extraction

JSON 파일에서 특정 경로 추출:

```python
metric = collector.collect_from_json(
    filepath="build/stats.json",
    json_path="assets.main.size",
    metric_name="bundle_size",
    unit="bytes",
)
```

### Duration Measurement

함수 실행 시간 측정:

```python
def run_tests():
    subprocess.run(["pytest"], check=True)

metric, result = collector.collect_duration(
    func=run_tests,
    metric_name="test_duration",
    unit="ms",
)
```

## Predefined Collections

### Test Metrics

```python
from components.metrics.collector import collect_test_metrics

collection = collect_test_metrics(
    run_id="run-001",
    issue_id="ISSUE-001",
    harness_root="/path/to/project",
)

# Collects: test_count, coverage
```

### Performance Metrics

```python
from components.metrics.collector import collect_performance_metrics

collection = collect_performance_metrics(
    run_id="run-001",
    issue_id="ISSUE-001",
    harness_root="/path/to/project",
)

# Collects: build_time, bundle_size
```

### Quality Metrics

```python
from components.metrics.collector import collect_quality_metrics

collection = collect_quality_metrics(
    run_id="run-001",
    issue_id="ISSUE-001",
    harness_root="/path/to/project",
)

# Collects: eslint_warnings
```

## Analysis and Validation

### Comparing Against Baselines

```python
from components.metrics.analyzer import MetricAnalyzer, ComparisonResult

analyzer = MetricAnalyzer(tolerance=0.05)

# Set baselines
analyzer.set_baseline("coverage", 80.0)
analyzer.set_baseline("build_time", 1000.0)

# Compare current values
result = analyzer.compare("coverage", 85.0, higher_is_better=True)

if result == ComparisonResult.IMPROVED:
    print("Coverage improved!")
elif result == ComparisonResult.REGRESSED:
    print("Coverage regressed!")
```

### Threshold Validation

```python
from components.metrics.analyzer import Threshold

# Define threshold
threshold = Threshold(min=70, target=80)

# Validate
passed, reason = analyzer.validate_threshold("coverage", 75.0, threshold)

if not passed:
    print(f"Threshold failed: {reason}")
```

### Batch Analysis

```python
from components.metrics.analyzer import create_performance_thresholds

metrics = {
    "coverage": 85.5,
    "build_time": 1200,
    "bundle_size": 480000,
}

thresholds = create_performance_thresholds()
analysis = analyzer.analyze_collection(metrics, thresholds)

print(f"Improved: {analysis['summary']['improved']}")
print(f"Regressed: {analysis['summary']['regressed']}")
print(f"Failed thresholds: {analysis['summary']['failed_thresholds']}")

# Get recommendations
recommendations = analyzer.get_recommendations(analysis)
for rec in recommendations:
    print(f"- {rec}")
```

## Integration with Workflows

### Performance Workflow

```python
from components.workflow.templates import performance_workflow

# Performance workflow includes metric-capture evidence
workflow = performance_workflow()

# The workflow gates require:
# - Performance tests executed
# - Performance metrics captured
# - Improvement verified
```

### Custom Metric Gates

워크플로우 템플릿에 메트릭 게이트 추가:

```python
from components.workflow.templates import QualityGate, EvidenceRequirement

# Add metric requirement to gate
gate = QualityGate(
    from_phase="test",
    to_phase="sync-audit",
    required_evidence=[
        EvidenceRequirement(
            kind="metric-capture",
            description="Performance metrics meet thresholds",
        ),
    ],
    auditor="sync",
    description="Performance must not regress",
)
```

## Common Metrics

### Test Coverage

```python
# Parse coverage from JSON output
metric = collector.collect_from_command(
    command=["npm", "test", "--", "--coverage", "--coverage-report=json"],
    metric_name="coverage",
    unit="%",
    parse_func=parse_coverage_json,
)
```

### Build Performance

```python
# Measure build time
metric, _ = collector.collect_duration(
    func=lambda: subprocess.run(["npm", "run", "build"]),
    metric_name="build_time",
    unit="ms",
)
```

### Bundle Size

```python
# Read from webpack stats
metric = collector.collect_from_json(
    filepath="build/stats.json",
    json_path="assets.main.size",
    metric_name="bundle_size",
    unit="bytes",
)
```

### Lighthouse Scores

```python
# Parse Lighthouse JSON output
metric = collector.collect_from_json(
    filepath="lighthouse-report.json",
    json_path="categories.performance.score",
    metric_name="lighthouse_performance",
    unit="score",
)
```

## Best Practices

1. **Establish Baselines**: 초기 기준선 설정
2. **Use Appropriate Tolerances**: 현실적인 허용 오차 사용
3. **Track Trends**: 시간 경과에 따른 추적
4. **Automate Collection**: 자동화된 수집 통합
5. **Validate Gates**: 품질 게이트에 사용

## Examples

### Example 1: Performance Regression Check

```python
from components.metrics.collector import collect_performance_metrics
from components.metrics.analyzer import MetricAnalyzer

# Collect current metrics
current = collect_performance_metrics("run-001", "ISSUE-001", "/project")

# Analyze against baselines
analyzer = MetricAnalyzer()
analyzer.set_baseline("build_time", 1000)
analyzer.set_baseline("bundle_size", 400000)

# Check for regressions
for metric in current.metrics:
    result = analyzer.compare(
        metric.name,
        metric.value,
        higher_is_better=False,  # Lower is better
    )

    if result == ComparisonResult.REGRESSED:
        print(f"WARNING: {metric.name} regressed!")
```

### Example 2: Quality Gate Validation

```python
from components.metrics.analyzer import create_performance_thresholds, analyze_as_evidence

# Define metrics
metrics = {
    "coverage": 75.5,
    "build_time": 900,
    "bundle_size": 420000,
}

# Define baselines
baselines = {
    "coverage": 80.0,
    "build_time": 1000,
    "bundle_size": 400000,
}

# Analyze and format as evidence
evidence = analyze_as_evidence(
    run_id="run-001",
    harness_root="/project",
    metrics=metrics,
    baselines=baselines,
)

# Check verdict
if evidence["verdict"] == "FAIL":
    print(f"Metrics failed: {evidence['summary']}")
    for rec in evidence["recommendations"]:
        print(f"  - {rec}")
```

### Example 3: Custom Metric Collection

```python
from components.metrics.collector import MetricCollector, MetricCollection

collector = MetricCollector("/project")
collection = MetricCollection("run-001", "ISSUE-001", "custom")

# Collect custom metric
metric = collector.collect_from_command(
    command=["python", "scripts/benchmark.py"],
    metric_name="request_latency",
    unit="ms",
    parse_func=lambda out: float(out.strip()),
)

if metric:
    collection.add(metric)

# Capture as evidence
summary = collection.get_summary()
```

## References

- Collector: `components/metrics/collector.py`
- Analyzer: `components/metrics/analyzer.py`
- Evidence capture: `scripts/runner.py` (capture_metrics function)
- Workflow templates: `components/workflow/templates.py`
