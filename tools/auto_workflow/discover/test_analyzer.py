"""Test suite analysis for issue discovery."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from . import DiscoveryMethod, DiscoveryFinding, IssueType, Severity, DiscoveryContext
from ..config import get_config
from ..utils.logging import get_logger


logger = get_logger("discover.tests")


class TestAnalyzer(DiscoveryMethod):
    """Analyzes test suite for failures, flakiness, and performance issues."""

    @property
    def name(self) -> str:
        return "test_analyzer"

    @property
    def enabled(self) -> bool:
        return get_config().discovery.test_analysis_enabled

    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run test analysis."""
        findings = []
        config = get_config().discovery

        # Run test suite and collect results
        test_results = self._run_tests(context, config)

        # Analyze failures
        findings.extend(self._analyze_failures(test_results, context))

        # Analyze slow tests
        findings.extend(self._analyze_slow_tests(test_results, context, config))

        # Check for flaky tests (re-run failed tests)
        if config.flaky_test_runs > 1:
            findings.extend(self._check_flakiness(test_results, context, config))

        logger.info(f"Test analyzer found {len(findings)} issues")
        return findings

    def _run_tests(self, context: DiscoveryContext, config) -> dict:
        """Run pytest and return structured results."""
        # Use pytest-json-report for structured output
        result = subprocess.run([
            "uv", "run", "pytest",
            "--json-report",
            "--json-report-file=/tmp/pytest_report.json",
            "-x",  # Stop on first failure for speed
            "--tb=short",
        ], cwd=context.repo_path, capture_output=True, text=True, timeout=config.test_timeout_seconds)

        # Try to read JSON report
        report_path = Path("/tmp/pytest_report.json")
        if report_path.exists():
            try:
                with open(report_path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass

        # Fallback: parse text output
        return self._parse_text_output(result.stdout, result.stderr, result.returncode)

    def _parse_text_output(self, stdout: str, stderr: str, returncode: int) -> dict:
        """Parse pytest text output."""
        return {
            "exitcode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "tests": [],
        }

    def _analyze_failures(self, test_results: dict, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Analyze test failures."""
        findings = []

        # Handle JSON report format
        if "tests" in test_results:
            for test in test_results["tests"]:
                if test.get("outcome") == "failed":
                    findings.append(self._create_failure_finding(test, context))
        else:
            # Parse from text output
            pass

        return findings

    def _create_failure_finding(self, test: dict, context: DiscoveryContext) -> DiscoveryFinding:
        """Create finding from failed test."""
        test_name = test.get("nodeid", "unknown")
        file_path = test.get("file", "")
        line_number = test.get("lineno", 0)
        error_msg = test.get("call", {}).get("longrepr", "")

        return DiscoveryFinding(
            id=f"test-fail-{test_name.replace('::', '-').replace('/', '-').replace('[', '-').replace(']', '')}",
            type=IssueType.TEST_FAILURE,
            severity=Severity.HIGH,
            title=f"Test failure: {test_name[:80]}",
            description=f"Test failed:\n{error_msg}\n\nTest: {test_name}\nFile: {file_path}:{line_number}",
            file_path=file_path,
            line_number=line_number,
            source="test_analyzer",
            evidence={
                "test_name": test_name,
                "test_file": file_path,
                "error": error_msg,
            },
            suggested_fix="Fix the failing test or the code it tests",
            estimated_effort_minutes=30,
        )

    def _analyze_slow_tests(self, test_results: dict, context: DiscoveryContext, config) -> list[DiscoveryFinding]:
        """Find slow tests."""
        findings = []
        threshold = config.slow_test_threshold_seconds

        if "tests" not in test_results:
            return findings

        for test in test_results["tests"]:
            duration = test.get("duration", 0)
            if duration > threshold:
                test_name = test.get("nodeid", "unknown")
                file_path = test.get("file", "")
                line_number = test.get("lineno", 0)

                findings.append(DiscoveryFinding(
                    id=f"test-slow-{test_name.replace('::', '-').replace('/', '-').replace('[', '-').replace(']', '')}",
                    type=IssueType.PERFORMANCE,
                    severity=Severity.LOW,
                    title=f"Slow test: {test_name[:80]} ({duration:.1f}s)",
                    description=f"Test takes {duration:.1f}s (threshold: {threshold}s)\n\nTest: {test_name}\nFile: {file_path}:{line_number}",
                    file_path=file_path,
                    line_number=line_number,
                    source="test_analyzer",
                    evidence={
                        "test_name": test_name,
                        "duration": duration,
                        "threshold": threshold,
                    },
                    suggested_fix="Optimize test or use fixtures to reduce setup time",
                    estimated_effort_minutes=20,
                ))

        return findings

    def _check_flakiness(self, test_results: dict, context: DiscoveryContext, config) -> list[DiscoveryFinding]:
        """Re-run failed tests to check for flakiness."""
        findings = []

        if "tests" not in test_results:
            return findings

        failed_tests = [t for t in test_results["tests"] if t.get("outcome") == "failed"]
        if not failed_tests:
            return findings

        # Re-run failed tests
        for test in failed_tests[:5]:  # Limit to 5 tests
            test_name = test.get("nodeid")
            if not test_name:
                continue

            flaky = False
            for _ in range(config.flaky_test_runs - 1):
                result = subprocess.run([
                    "uv", "run", "pytest", test_name, "-v", "--tb=short"
                ], cwd=context.repo_path, capture_output=True, text=True, timeout=60)

                if result.returncode == 0:
                    flaky = True
                    break

            if flaky:
                findings.append(DiscoveryFinding(
                    id=f"test-flaky-{test_name.replace('::', '-').replace('/', '-').replace('[', '-').replace(']', '')}",
                    type=IssueType.TEST_FAILURE,
                    severity=Severity.MEDIUM,
                    title=f"Flaky test: {test_name[:80]}",
                    description=f"Test passes on re-run, indicating flakiness\n\nTest: {test_name}",
                    file_path=test.get("file", ""),
                    line_number=test.get("lineno", 0),
                    source="test_analyzer",
                    evidence={"test_name": test_name, "flaky": True},
                    suggested_fix="Investigate and fix test flakiness (timing, isolation, etc.)",
                    estimated_effort_minutes=45,
                ))

        return findings