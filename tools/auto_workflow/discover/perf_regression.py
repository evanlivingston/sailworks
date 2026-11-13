"""Performance regression detection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import DiscoveryMethod, DiscoveryFinding, IssueType, Severity, DiscoveryContext
from ..config import get_config
from ..utils.logging import get_logger


logger = get_logger("discover.perf")


class PerfRegressionDetector(DiscoveryMethod):
    """Detects performance regressions by comparing benchmarks."""

    @property
    def name(self) -> str:
        return "perf_regression"

    @property
    def enabled(self) -> bool:
        return get_config().discovery.perf_regression_enabled

    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run performance benchmarks and compare with baselines."""
        findings = []
        config = get_config().discovery

        baseline_dir = Path(context.repo_path) / config.benchmark_baseline_dir
        if not baseline_dir.exists():
            logger.info(f"Baseline directory not found: {baseline_dir}")
            return findings

        # Find benchmark scripts
        benchmark_scripts = self._find_benchmark_scripts(context.repo_path)
        if not benchmark_scripts:
            logger.info("No benchmark scripts found")
            return findings

        for script in benchmark_scripts:
            findings.extend(self._run_benchmark_and_compare(script, baseline_dir, context, config))

        logger.info(f"Perf regression detector found {len(findings)} issues")
        return findings

    def _find_benchmark_scripts(self, repo_path: str) -> list[Path]:
        """Find benchmark scripts in tools/."""
        tools_dir = Path(repo_path) / "tools"
        if not tools_dir.exists():
            return []

        scripts = []
        for pattern in ["benchmark_*.py", "profile_*.py", "*_benchmark.py"]:
            scripts.extend(tools_dir.glob(pattern))

        return scripts

    def _run_benchmark_and_compare(
        self, script: Path, baseline_dir: Path, context: DiscoveryContext, config
    ) -> list[DiscoveryFinding]:
        """Run benchmark and compare with baseline."""
        findings = []

        try:
            # Run benchmark
            result = subprocess.run(
                ["uv", "run", "python", str(script)],
                cwd=context.repo_path,
                capture_output=True,
                text=True,
                timeout=300,
            )

            # Parse output (expects JSON output with metrics)
            current_metrics = self._parse_benchmark_output(result.stdout)
            if not current_metrics:
                return findings

            # Find matching baseline
            baseline_file = baseline_dir / f"{script.stem}.json"
            if not baseline_file.exists():
                # Create baseline if doesn't exist
                self._save_baseline(baseline_file, current_metrics)
                return findings

            # Load baseline
            with open(baseline_file) as f:
                baseline_metrics = json.load(f)

            # Compare
            threshold = config.regression_threshold_percent / 100.0
            for metric_name, current_value in current_metrics.items():
                baseline_value = baseline_metrics.get(metric_name)
                if baseline_value is None or baseline_value == 0:
                    continue

                # Calculate regression (higher is worse for time/memory)
                ratio = current_value / baseline_value
                if ratio > (1 + threshold):
                    # Regression detected
                    findings.append(DiscoveryFinding(
                        id=f"perf-{script.stem}-{metric_name}",
                        type=IssueType.PERFORMANCE,
                        severity=Severity.HIGH if ratio > 1.5 else Severity.MEDIUM,
                        title=f"Performance regression in {script.stem}: {metric_name}",
                        description=(
                            f"Metric '{metric_name}' regressed by {(ratio - 1) * 100:.1f}%\n"
                            f"Baseline: {baseline_value:.4f}\n"
                            f"Current:  {current_value:.4f}\n"
                            f"Threshold: {config.regression_threshold_percent}%"
                        ),
                        file_path=str(script.relative_to(context.repo_path)),
                        line_number=None,
                        source="perf_regression",
                        evidence={
                            "metric": metric_name,
                            "baseline": baseline_value,
                            "current": current_value,
                            "regression_percent": (ratio - 1) * 100,
                            "threshold_percent": config.regression_threshold_percent,
                        },
                        suggested_fix=f"Investigate performance regression in {metric_name}",
                        estimated_effort_minutes=60,
                    ))

        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"Benchmark {script} failed: {e}")

        return findings

    def _parse_benchmark_output(self, output: str) -> dict[str, float] | None:
        """Parse benchmark output for metrics."""
        # Try to find JSON in output
        try:
            # Look for JSON object in output
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(output[start:end])
        except json.JSONDecodeError:
            pass

        # Try line-by-line for key: value pairs
        metrics = {}
        for line in output.split("\n"):
            if ":" in line and not line.strip().startswith("#"):
                parts = line.split(":", 1)
                key = parts[0].strip()
                try:
                    value = float(parts[1].strip().split()[0])
                    metrics[key] = value
                except ValueError:
                    pass

        return metrics if metrics else None

    def _save_baseline(self, baseline_file: Path, metrics: dict[str, float]) -> None:
        """Save baseline metrics."""
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        with open(baseline_file, "w") as f:
            json.dump(metrics, f, indent=2)
        logger.info(f"Created baseline: {baseline_file}")