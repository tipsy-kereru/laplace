"""Metric collector: Capture and record performance/quality metrics.

Provides helpers for capturing various metrics and converting them to
metric-capture evidence entries.
"""

import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime


class MetricPoint:
    """A single metric measurement."""

    def __init__(
        self,
        name: str,
        value: float,
        unit: str = "",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.value = value
        self.unit = unit
        self.timestamp = timestamp or datetime.utcnow().isoformat() + "Z"
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class MetricCollection:
    """Collection of metric points with metadata."""

    def __init__(
        self,
        run_id: str,
        issue_id: str,
        collection_type: str = "performance",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.run_id = run_id
        self.issue_id = issue_id
        self.collection_type = collection_type
        self.metrics: List[MetricPoint] = []
        self.metadata = metadata or {}
        self.collected_at = datetime.utcnow().isoformat() + "Z"

    def add(self, metric: MetricPoint) -> None:
        """Add a metric point to the collection."""
        self.metrics.append(metric)

    def add_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a metric point by value."""
        self.add(MetricPoint(name, value, unit, metadata=metadata))

    def get_summary(self) -> str:
        """Get a human-readable summary for evidence capture."""
        count = len(self.metrics)
        if count == 0:
            return f"{self.collection_type} metrics: 0 collected"

        # Group by name
        by_name: Dict[str, List[MetricPoint]] = {}
        for m in self.metrics:
            if m.name not in by_name:
                by_name[m.name] = []
            by_name[m.name].append(m)

        parts = []
        for name, points in by_name.items():
            if len(points) == 1:
                parts.append(f"{name}={points[0].value}{points[0].unit}")
            else:
                values = [p.value for p in points]
                parts.append(f"{name}=avg({sum(values)/len(values):.2f}){points[0].unit}")

        return f"{self.collection_type} metrics: {', '.join(parts[:5])}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "run_id": self.run_id,
            "issue_id": self.issue_id,
            "collection_type": self.collection_type,
            "metrics": [m.to_dict() for m in self.metrics],
            "metadata": self.metadata,
            "collected_at": self.collected_at,
        }


class MetricCollector:
    """Helper class for collecting metrics from various sources."""

    def __init__(self, harness_root: str):
        self.harness_root = harness_root

    def collect_from_command(
        self,
        command: List[str],
        metric_name: str,
        unit: str = "",
        parse_func: Optional[Callable[[str], float]] = None,
        cwd: Optional[str] = None,
    ) -> Optional[MetricPoint]:
        """Collect a metric by running a command and parsing output.

        Args:
            command: Command to run (e.g., ["npm", "test", "-- --coverage"])
            metric_name: Name for the collected metric
            unit: Unit of measurement (e.g., "%", "ms", "bytes")
            parse_func: Function to parse command output to float value
            cwd: Working directory for command

        Returns:
            MetricPoint or None if command failed
        """
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=cwd or self.harness_root,
                timeout=60,
            )

            if result.returncode != 0:
                return None

            output = result.stdout.strip()

            # Parse the output
            if parse_func:
                value = parse_func(output)
            else:
                # Try to convert directly
                value = float(output)

            return MetricPoint(
                name=metric_name,
                value=value,
                unit=unit,
                metadata={"command": " ".join(command)},
            )

        except (subprocess.TimeoutExpired, ValueError, subprocess.SubprocessError):
            return None

    def collect_from_file(
        self,
        filepath: str,
        metric_name: str,
        unit: str = "",
        parse_func: Optional[Callable[[str], float]] = None,
    ) -> Optional[MetricPoint]:
        """Collect a metric by reading and parsing a file.

        Args:
            filepath: Path to file containing metric value
            metric_name: Name for the collected metric
            unit: Unit of measurement
            parse_func: Function to parse file content to float value

        Returns:
            MetricPoint or None if file doesn't exist or parsing fails
        """
        try:
            full_path = os.path.join(self.harness_root, filepath)
            if not os.path.exists(full_path):
                return None

            with open(full_path, "r") as f:
                content = f.read().strip()

            if parse_func:
                value = parse_func(content)
            else:
                value = float(content)

            return MetricPoint(
                name=metric_name,
                value=value,
                unit=unit,
                metadata={"source": filepath},
            )

        except (IOError, ValueError):
            return None

    def collect_from_json(
        self,
        filepath: str,
        json_path: str,
        metric_name: str,
        unit: str = "",
    ) -> Optional[MetricPoint]:
        """Collect a metric from a JSON file using a JSON path.

        Args:
            filepath: Path to JSON file
            json_path: Dot-separated path to value (e.g., "coverage.lines")
            metric_name: Name for the collected metric
            unit: Unit of measurement

        Returns:
            MetricPoint or None if path doesn't exist
        """
        try:
            full_path = os.path.join(self.harness_root, filepath)
            if not os.path.exists(full_path):
                return None

            with open(full_path, "r") as f:
                data = json.load(f)

            # Navigate JSON path
            value = data
            for key in json_path.split("."):
                if isinstance(value, dict):
                    value = value.get(key)
                elif isinstance(value, list) and key.isdigit():
                    value = value[int(key)]
                else:
                    return None

            if value is None:
                return None

            return MetricPoint(
                name=metric_name,
                value=float(value),
                unit=unit,
                metadata={"source": filepath, "json_path": json_path},
            )

        except (IOError, ValueError, KeyError, TypeError):
            return None

    def collect_duration(
        self,
        func: Callable,
        metric_name: str,
        unit: str = "ms",
    ) -> Tuple[Optional[MetricPoint], Any]:
        """Collect execution duration of a function.

        Args:
            func: Function to measure
            metric_name: Name for the duration metric
            unit: Unit for duration (default: ms)

        Returns:
            (MetricPoint or None, function result)
        """
        start = time.time()
        try:
            result = func()
            duration = (time.time() - start) * 1000  # Convert to ms

            metric = MetricPoint(
                name=metric_name,
                value=duration,
                unit=unit,
            )

            return metric, result

        except Exception as e:
            return None, e


# Common metric parsers

def parse_percentage(output: str) -> float:
    """Parse percentage from output (e.g., "85.5%" -> 85.5)."""
    # Remove % and convert
    cleaned = output.strip().rstrip("%")
    return float(cleaned)


def parse_coverage_json(output: str) -> float:
    """Parse coverage percentage from coverage JSON output."""
    try:
        data = json.loads(output)
        return data.get("totals", {}).get("percent_covered", 0.0)
    except (json.JSONDecodeError, KeyError, TypeError):
        return 0.0


def parse_test_count(output: str) -> int:
    """Parse test count from test output."""
    # Common patterns: "100 tests", "100 passed", "100 passing"
    import re
    match = re.search(r"(\d+)\s+(?:tests?|passed|passing)", output, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def parse_duration(output: str) -> float:
    """Parse duration from output (e.g., "1.5s", "1500ms")."""
    output = output.strip().lower()
    if output.endswith("ms"):
        return float(output.rstrip("ms"))
    elif output.endswith("s"):
        return float(output.rstrip("s")) * 1000
    elif output.endswith("m"):
        return float(output.rstrip("m")) * 60000
    return float(output)


# Predefined metric collections

def collect_test_metrics(
    run_id: str,
    issue_id: str,
    harness_root: str,
) -> MetricCollection:
    """Collect standard test metrics.

    Collects:
    - Test count
    - Test duration
    - Coverage percentage (if available)
    """
    collector = MetricCollector(harness_root)
    collection = MetricCollection(run_id, issue_id, "test")

    # Test count
    test_metric = collector.collect_from_command(
        command=["npm", "test", "--", "--verbose"],
        metric_name="test_count",
        parse_func=parse_test_count,
    )
    if test_metric:
        collection.add(test_metric)

    # Coverage
    coverage_metric = collector.collect_from_command(
        command=["npm", "test", "--", "--coverage"],
        metric_name="coverage",
        unit="%",
        parse_func=parse_coverage_json,
    )
    if coverage_metric:
        collection.add(coverage_metric)

    return collection


def collect_performance_metrics(
    run_id: str,
    issue_id: str,
    harness_root: str,
) -> MetricCollection:
    """Collect performance metrics.

    Collects:
    - Build time
    - Bundle size (if applicable)
    - Lighthouse scores (if applicable)
    """
    collector = MetricCollector(harness_root)
    collection = MetricCollection(run_id, issue_id, "performance")

    # Build time
    def run_build():
        return subprocess.run(
            ["npm", "run", "build"],
            capture_output=True,
            cwd=harness_root,
        )

    build_metric, _ = collector.collect_duration(
        func=run_build,
        metric_name="build_time",
        unit="ms",
    )
    if build_metric:
        collection.add(build_metric)

    # Bundle size
    size_metric = collector.collect_from_json(
        filepath="build/stats.json",
        json_path="assets.main.size",
        metric_name="bundle_size",
        unit="bytes",
    )
    if size_metric:
        collection.add(size_metric)

    return collection


def collect_quality_metrics(
    run_id: str,
    issue_id: str,
    harness_root: str,
) -> MetricCollection:
    """Collect code quality metrics.

    Collects:
    - Linter warnings
    - Type checking coverage
    - Code duplication
    """
    collector = MetricCollector(harness_root)
    collection = MetricCollection(run_id, issue_id, "quality")

    # ESLint warnings
    lint_metric = collector.collect_from_command(
        command=["npm", "run", "lint", "--", "--format", "json"],
        metric_name="eslint_warnings",
        parse_func=lambda out: len(json.loads(out).get("results", [])),
    )
    if lint_metric:
        collection.add(lint_metric)

    return collection
