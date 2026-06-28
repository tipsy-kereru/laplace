"""Metric analyzer: Compare, validate, and report on metrics.

Provides tools for:
- Metric comparison (baseline vs current)
- Threshold validation
- Trend analysis
- Metric-based recommendations
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class ComparisonResult(Enum):
    """Result of metric comparison."""

    IMPROVED = "improved"      # Value improved (better than baseline)
    REGRESSED = "regressed"    # Value worsened (worse than baseline)
    UNCHANGED = "unchanged"    # Within tolerance of baseline
    NO_BASELINE = "no_baseline"  # No baseline to compare


@dataclass
class Threshold:
    """Threshold for metric validation."""

    min: Optional[float] = None
    max: Optional[float] = None
    target: Optional[float] = None

    def check(self, value: float) -> Tuple[bool, str]:
        """Check if value meets threshold.

        Returns:
            (passed, reason) tuple
        """
        if self.min is not None and value < self.min:
            return False, f"Value {value} below minimum {self.min}"

        if self.max is not None and value > self.max:
            return False, f"Value {value} above maximum {self.max}"

        if self.target is not None:
            diff = abs(value - self.target)
            # Allow 5% tolerance
            tolerance = self.target * 0.05
            if diff > tolerance:
                return False, f"Value {value} not within tolerance of target {self.target}"

        return True, "Within thresholds"


class MetricAnalyzer:
    """Analyze and compare metrics."""

    def __init__(self, tolerance: float = 0.05):
        """
        Args:
            tolerance: Percentage tolerance for "unchanged" determination (default 5%)
        """
        self.tolerance = tolerance
        self.baselines: Dict[str, float] = {}

    def set_baseline(self, metric_name: str, value: float) -> None:
        """Set a baseline value for comparison."""
        self.baselines[metric_name] = value

    def compare(
        self,
        metric_name: str,
        current_value: float,
        higher_is_better: bool = False,
    ) -> ComparisonResult:
        """Compare current value against baseline.

        Args:
            metric_name: Name of the metric
            current_value: Current metric value
            higher_is_better: Whether higher values are better

        Returns:
            ComparisonResult enum
        """
        baseline = self.baselines.get(metric_name)
        if baseline is None:
            return ComparisonResult.NO_BASELINE

        # Calculate percent change
        if baseline == 0:
            change = float("inf") if current_value > 0 else 0
        else:
            change = abs(current_value - baseline) / baseline

        if change <= self.tolerance:
            return ComparisonResult.UNCHANGED

        # Determine if improved or regressed
        if higher_is_better:
            improved = current_value > baseline
        else:
            improved = current_value < baseline

        return ComparisonResult.IMPROVED if improved else ComparisonResult.REGRESSED

    def validate_threshold(self, metric_name: str, value: float, threshold: Threshold) -> Tuple[bool, str]:
        """Validate a metric against a threshold.

        Args:
            metric_name: Name of the metric
            value: Current metric value
            threshold: Threshold to validate against

        Returns:
            (passed, reason) tuple
        """
        return threshold.check(value)

    def analyze_collection(self, metrics: Dict[str, float], thresholds: Optional[Dict[str, Threshold]] = None) -> Dict[str, Any]:
        """Analyze a collection of metrics.

        Args:
            metrics: Dictionary of metric name -> value
            thresholds: Optional dictionary of thresholds

        Returns:
            Analysis results dictionary
        """
        results = {
            "metrics": {},
            "summary": {
                "total": len(metrics),
                "improved": 0,
                "regressed": 0,
                "unchanged": 0,
                "no_baseline": 0,
                "passed_thresholds": 0,
                "failed_thresholds": 0,
            },
        }

        thresholds = thresholds or {}

        for name, value in metrics.items():
            comparison = self.compare(name, value)
            threshold_result = (True, "") if name not in thresholds else self.validate_threshold(name, value, thresholds[name])

            metric_result = {
                "value": value,
                "comparison": comparison.value,
                "baseline": self.baselines.get(name),
                "threshold_passed": threshold_result[0],
                "threshold_reason": threshold_result[1],
            }

            results["metrics"][name] = metric_result

            # Update summary
            if comparison == ComparisonResult.IMPROVED:
                results["summary"]["improved"] += 1
            elif comparison == ComparisonResult.REGRESSED:
                results["summary"]["regressed"] += 1
            elif comparison == ComparisonResult.UNCHANGED:
                results["summary"]["unchanged"] += 1
            else:
                results["summary"]["no_baseline"] += 1

            if threshold_result[0]:
                results["summary"]["passed_thresholds"] += 1
            else:
                results["summary"]["failed_thresholds"] += 1

        return results

    def get_recommendations(self, analysis: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on analysis.

        Args:
            analysis: Analysis results from analyze_collection()

        Returns:
            List of recommendation strings
        """
        recommendations = []

        summary = analysis.get("summary", {})

        if summary.get("regressed", 0) > 0:
            recommendations.append(
                f"{summary['regressed']} metric(s) regressed from baseline. "
                "Investigate and address performance degradation."
            )

        if summary.get("failed_thresholds", 0) > 0:
            recommendations.append(
                f"{summary['failed_thresholds']} metric(s) failed threshold validation. "
                "Review quality gates."
            )

        if summary.get("no_baseline", 0) > 0:
            recommendations.append(
                f"{summary['no_baseline']} metric(s) have no baseline. "
                "Consider establishing baselines for trend tracking."
            )

        return recommendations


# Common threshold definitions

class PerformanceThresholds:
    """Common performance metric thresholds."""

    @staticmethod
    def coverage() -> Threshold:
        """Test coverage threshold (target 80%)."""
        return Threshold(min=70, target=80)

    @staticmethod
    def build_time() -> Threshold:
        """Build time threshold (max 5 minutes)."""
        return Threshold(max=300000)  # 5 minutes in ms

    @staticmethod
    def bundle_size() -> Threshold:
        """Bundle size threshold (max 500KB)."""
        return Threshold(max=500000)  # 500KB in bytes

    @staticmethod
    def lighthouse_performance() -> Threshold:
        """Lighthouse performance score (min 90)."""
        return Threshold(min=90)

    @staticmethod
    def lighthouse_accessibility() -> Threshold:
        """Lighthouse accessibility score (min 90)."""
        return Threshold(min=90)


def create_performance_thresholds() -> Dict[str, Threshold]:
    """Create a dictionary of common performance thresholds."""
    return {
        "coverage": PerformanceThresholds.coverage(),
        "build_time": PerformanceThresholds.build_time(),
        "bundle_size": PerformanceThresholds.bundle_size(),
        "lighthouse_performance": PerformanceThresholds.lighthouse_performance(),
        "lighthouse_accessibility": PerformanceThresholds.lighthouse_accessibility(),
    }


def metric_direction(metric_name: str) -> bool:
    """Determine if higher values are better for a metric.

    Args:
        metric_name: Name of the metric

    Returns:
        True if higher is better, False if lower is better
    """
    # Higher is better
    higher_better = [
        "coverage",
        "test_count",
        "lighthouse_performance",
        "lighthouse_accessibility",
        "lighthouse_best_practices",
        "lighthouse_seo",
    ]

    # Lower is better (default for most duration/size metrics)
    return metric_name in higher_better


def format_metric_change(current: float, baseline: Optional[float], unit: str = "") -> str:
    """Format metric change for display.

    Args:
        current: Current metric value
        baseline: Baseline value (if available)
        unit: Unit string

    Returns:
        Formatted string
    """
    if baseline is None:
        return f"{current}{unit} (no baseline)"

    diff = current - baseline
    if baseline == 0:
        pct = "∞"
    else:
        pct = f"{diff / baseline * 100:+.1f}%"

    return f"{current}{unit} (was {baseline}{unit}, {pct})"


# Integration with evidence capture

def analyze_as_evidence(
    run_id: str,
    harness_root: str,
    metrics: Dict[str, float],
    baselines: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Analyze metrics and format as evidence.

    Args:
        run_id: Run identifier
        harness_root: Path to harness root
        metrics: Dictionary of metric name -> value
        baselines: Optional baseline values

    Returns:
        Evidence-ready dictionary
    """
    analyzer = MetricAnalyzer()

    # Set baselines
    if baselines:
        for name, value in baselines.items():
            analyzer.set_baseline(name, value)

    # Get thresholds
    thresholds = create_performance_thresholds()

    # Analyze
    analysis = analyzer.analyze_collection(metrics, thresholds)

    # Get recommendations
    recommendations = analyzer.get_recommendations(analysis)

    return {
        "verdict": "PASS" if analysis["summary"]["failed_thresholds"] == 0 else "FAIL",
        "summary": f"Metric analysis: {analysis['summary']['improved']} improved, "
                  f"{analysis['summary']['regressed']} regressed, "
                  f"{analysis['summary']['failed_thresholds']} failed thresholds",
        "analysis": analysis,
        "recommendations": recommendations,
        "metrics": metrics,
    }
