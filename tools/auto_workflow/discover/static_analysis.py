"""Static analysis-based issue discovery (pylint, mypy, bandit, radon)."""

from __future__ import annotations

import json
import subprocess
import os
from pathlib import Path
from typing import Any

from . import DiscoveryMethod, DiscoveryFinding, IssueType, Severity, DiscoveryContext
from ..config import get_config
from ..utils.logging import get_logger


logger = get_logger("discover.static")


class StaticAnalysisScanner(DiscoveryMethod):
    """Discovers issues using static analysis tools."""

    @property
    def name(self) -> str:
        return "static_analysis"

    @property
    def enabled(self) -> bool:
        return get_config().discovery.static_analysis_enabled

    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run all enabled static analysis tools."""
        findings = []
        config = get_config().discovery

        if config.pylint_enabled:
            findings.extend(self._run_pylint(context))
        if config.mypy_enabled:
            findings.extend(self._run_mypy(context))
        if config.bandit_enabled:
            findings.extend(self._run_bandit(context))
        if config.radon_enabled:
            findings.extend(self._run_radon(context))

        logger.info(f"Static analysis found {len(findings)} issues")
        return findings

    def _run_pylint(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run pylint on changed files."""
        findings = []
        if not context.changed_files:
            return findings

        # Filter to Python files
        py_files = [f for f in context.changed_files if f.endswith(".py")]
        if not py_files:
            return findings

        try:
            result = subprocess.run(
                ["pylint", "--output-format=json"] + py_files,
                cwd=context.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.stdout:
                pylint_results = json.loads(result.stdout)
                for item in pylint_results:
                    findings.append(self._convert_pylint(item, context))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"Pylint failed: {e}")

        return findings

    def _convert_pylint(self, item: dict, context: DiscoveryContext) -> DiscoveryFinding:
        """Convert pylint result to DiscoveryFinding."""
        severity_map = {
            "fatal": Severity.CRITICAL,
            "error": Severity.HIGH,
            "warning": Severity.MEDIUM,
            "refactor": Severity.LOW,
            "convention": Severity.INFO,
            "info": Severity.INFO,
        }

        msg_id = item.get("message-id", "")
        issue_type = IssueType.CODE_SMELL
        if msg_id.startswith("E"):  # Error
            issue_type = IssueType.BUG
        elif msg_id.startswith("W"):  # Warning
            issue_type = IssueType.CODE_SMELL
        elif msg_id.startswith("R"):  # Refactor
            issue_type = IssueType.REFACTOR
        elif msg_id.startswith("C"):  # Convention
            issue_type = IssueType.CODE_SMELL

        return DiscoveryFinding(
            id=f"pylint-{item.get('path', '').replace('/', '-')}-{item.get('line', 0)}-{msg_id}",
            type=issue_type,
            severity=severity_map.get(item.get("type", "").lower(), Severity.MEDIUM),
            title=f"Pylint: {item.get('message', '')[:80]}",
            description=f"{item.get('message', '')}\n\nFile: {item.get('path')}:{item.get('line')}\nSymbol: {item.get('symbol')}\nMessage-ID: {msg_id}",
            file_path=item.get("path"),
            line_number=item.get("line"),
            source="pylint",
            evidence={
                "pylint_symbol": item.get("symbol"),
                "pylint_message_id": msg_id,
                "pylint_type": item.get("type"),
            },
            suggested_fix=None,
            estimated_effort_minutes=15,
        )

    def _run_mypy(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run mypy on changed files."""
        findings = []
        if not context.changed_files:
            return findings

        py_files = [f for f in context.changed_files if f.endswith(".py")]
        if not py_files:
            return findings

        try:
            result = subprocess.run(
                ["mypy", "--show-error-codes", "--no-error-summary"] + py_files,
                cwd=context.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # Parse mypy output (text format)
            for line in result.stdout.split("\n"):
                if ":" in line and "error:" in line:
                    findings.append(self._parse_mypy_line(line, context))
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"Mypy failed: {e}")

        return findings

    def _parse_mypy_line(self, line: str, context: DiscoveryContext) -> DiscoveryFinding:
        """Parse single mypy output line."""
        # Format: file:line:col: error: message [error-code]
        parts = line.split(":", 3)
        if len(parts) < 4:
            return None

        file_path = parts[0]
        line_num = int(parts[1]) if parts[1].isdigit() else 0
        message = parts[3].strip() if len(parts) > 3 else line

        return DiscoveryFinding(
            id=f"mypy-{file_path.replace('/', '-')}-{line_num}",
            type=IssueType.BUG,
            severity=Severity.HIGH,
            title=f"Mypy: {message[:80]}",
            description=f"Type checking error:\n{message}\n\nFile: {file_path}:{line_num}",
            file_path=file_path,
            line_number=line_num,
            source="mypy",
            evidence={"mypy_output": line},
            suggested_fix=None,
            estimated_effort_minutes=20,
        )

    def _run_bandit(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run bandit security linter."""
        findings = []
        if not context.changed_files:
            return findings

        py_files = [f for f in context.changed_files if f.endswith(".py")]
        if not py_files:
            return findings

        try:
            result = subprocess.run(
                ["bandit", "-f", "json", "-q"] + py_files,
                cwd=context.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.stdout:
                bandit_results = json.loads(result.stdout)
                for item in bandit_results.get("results", []):
                    findings.append(self._convert_bandit(item, context))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"Bandit failed: {e}")

        return findings

    def _convert_bandit(self, item: dict, context: DiscoveryContext) -> DiscoveryFinding:
        """Convert bandit result to DiscoveryFinding."""
        severity_map = {
            "HIGH": Severity.HIGH,
            "MEDIUM": Severity.MEDIUM,
            "LOW": Severity.LOW,
        }

        return DiscoveryFinding(
            id=f"bandit-{item.get('filename', '').replace('/', '-')}-{item.get('line_number', 0)}-{item.get('test_id', '')}",
            type=IssueType.SECURITY,
            severity=severity_map.get(item.get("issue_severity", "MEDIUM"), Severity.MEDIUM),
            title=f"Bandit: {item.get('issue_text', '')[:80]}",
            description=f"Security issue: {item.get('issue_text', '')}\n\nFile: {item.get('filename')}:{item.get('line_number')}\nTest: {item.get('test_id')}\nConfidence: {item.get('issue_confidence')}\nCode: {item.get('code', '')}",
            file_path=item.get("filename"),
            line_number=item.get("line_number"),
            source="bandit",
            evidence={
                "bandit_test_id": item.get("test_id"),
                "bandit_confidence": item.get("issue_confidence"),
                "bandit_code": item.get("code"),
            },
            suggested_fix=item.get("more_info"),
            estimated_effort_minutes=30,
        )

    def _run_radon(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run radon for complexity analysis."""
        findings = []
        config = get_config().discovery
        threshold = config.complexity_threshold

        if not context.changed_files:
            return findings

        py_files = [f for f in context.changed_files if f.endswith(".py")]
        if not py_files:
            return findings

        try:
            result = subprocess.run(
                ["radon", "cc", "-s", "-j"] + py_files,
                cwd=context.repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.stdout:
                radon_results = json.loads(result.stdout)
                for file_path, functions in radon_results.items():
                    for func in functions:
                        complexity = func.get("complexity", 0)
                        if complexity >= threshold:
                            findings.append(self._convert_radon(file_path, func, threshold, context))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"Radon failed: {e}")

        return findings

    def _convert_radon(self, file_path: str, func: dict, threshold: int, context: DiscoveryContext) -> DiscoveryFinding:
        """Convert radon result to DiscoveryFinding."""
        complexity = func.get("complexity", 0)
        severity = Severity.HIGH if complexity > threshold * 2 else Severity.MEDIUM

        return DiscoveryFinding(
            id=f"radon-{file_path.replace('/', '-')}-{func.get('lineno', 0)}-{func.get('name', '')}",
            type=IssueType.CODE_SMELL,
            severity=severity,
            title=f"High complexity: {func.get('name')} (CC={complexity})",
            description=f"Function '{func.get('name')}' has cyclomatic complexity {complexity} (threshold: {threshold})\n\nFile: {file_path}:{func.get('lineno')}\nType: {func.get('type', 'function')}",
            file_path=file_path,
            line_number=func.get("lineno"),
            source="radon",
            evidence={
                "complexity": complexity,
                "threshold": threshold,
                "function_type": func.get("type"),
                "function_name": func.get("name"),
            },
            suggested_fix="Consider refactoring into smaller functions",
            estimated_effort_minutes=30,
        )