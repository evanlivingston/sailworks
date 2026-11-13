"""Code pattern mining for TODO/FIXME, deprecated patterns, code smells."""

from __future__ import annotations

import re
import ast
from pathlib import Path
from typing import Any

from . import DiscoveryMethod, DiscoveryFinding, IssueType, Severity, DiscoveryContext
from ..config import get_config
from ..utils.logging import get_logger


logger = get_logger("discover.patterns")


class PatternMiner(DiscoveryMethod):
    """Mines code patterns for issues."""

    @property
    def name(self) -> str:
        return "pattern_miner"

    @property
    def enabled(self) -> bool:
        return get_config().discovery.pattern_mining_enabled

    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run pattern mining on changed files."""
        findings = []
        config = get_config().discovery

        for file_path in context.changed_files:
            full_path = Path(context.repo_path) / file_path
            if not full_path.exists():
                continue

            if file_path.endswith(".py"):
                findings.extend(self._mine_python_file(file_path, full_path, config))
            elif file_path.endswith((".js", ".ts", ".jsx", ".tsx")):
                findings.extend(self._mine_js_file(file_path, full_path, config))

        logger.info(f"Pattern miner found {len(findings)} issues")
        return findings

    def _mine_python_file(self, rel_path: str, full_path: Path, config) -> list[DiscoveryFinding]:
        """Mine patterns in Python file."""
        findings = []

        try:
            content = full_path.read_text(encoding="utf-8")
            lines = content.split("\n")

            # TODO/FIXME patterns
            findings.extend(self._find_todo_patterns(rel_path, lines, config))

            # Deprecated patterns
            findings.extend(self._find_deprecated_patterns(rel_path, lines, config))

            # Code smells via AST
            findings.extend(self._find_ast_smells(rel_path, content, config))

        except Exception as e:
            logger.warning(f"Failed to mine {rel_path}: {e}")

        return findings

    def _find_todo_patterns(self, rel_path: str, lines: list[str], config) -> list[DiscoveryFinding]:
        """Find TODO/FIXME comments."""
        findings = []
        patterns = config.todo_patterns

        for i, line in enumerate(lines, 1):
            for pattern in patterns:
                # Match pattern in comments
                if re.search(rf"#.*\b{pattern}\b", line, re.IGNORECASE):
                    # Extract the comment
                    comment = line.strip()
                    findings.append(DiscoveryFinding(
                        id=f"todo-{rel_path.replace('/', '-')}-{i}-{pattern.lower()}",
                        type=IssueType.CODE_SMELL if pattern != "BUG" else IssueType.BUG,
                        severity=Severity.LOW if pattern in ["TODO", "XXX"] else Severity.MEDIUM,
                        title=f"{pattern} in {rel_path}:{i}",
                        description=f"Found {pattern} comment:\n{comment}\n\nFile: {rel_path}:{i}",
                        file_path=rel_path,
                        line_number=i,
                        source="pattern_miner",
                        evidence={"pattern": pattern, "comment": comment},
                        suggested_fix=f"Address the {pattern.lower()} or create a ticket",
                        estimated_effort_minutes=15,
                    ))

        return findings

    def _find_deprecated_patterns(self, rel_path: str, lines: list[str], config) -> list[DiscoveryFinding]:
        """Find deprecated/legacy patterns."""
        findings = []
        patterns = config.deprecated_patterns

        for i, line in enumerate(lines, 1):
            for pattern in patterns:
                if re.search(rf"\b{pattern}\b", line, re.IGNORECASE):
                    findings.append(DiscoveryFinding(
                        id=f"deprecated-{rel_path.replace('/', '-')}-{i}-{pattern.lower()}",
                        type=IssueType.DEPRECATION,
                        severity=Severity.LOW,
                        title=f"Deprecated pattern '{pattern}' in {rel_path}:{i}",
                        description=f"Found deprecated/legacy pattern:\n{line.strip()}\n\nFile: {rel_path}:{i}",
                        file_path=rel_path,
                        line_number=i,
                        source="pattern_miner",
                        evidence={"pattern": pattern, "line": line.strip()},
                        suggested_fix=f"Replace deprecated {pattern} with modern alternative",
                        estimated_effort_minutes=20,
                    ))

        return findings

    def _find_ast_smells(self, rel_path: str, content: str, config) -> list[DiscoveryFinding]:
        """Find code smells using AST analysis."""
        findings = []

        try:
            tree = ast.parse(content)

            for node in ast.walk(tree):
                # Long functions
                if isinstance(node, ast.FunctionDef):
                    if node.end_lineno and node.lineno:
                        length = node.end_lineno - node.lineno
                        if length > 50:
                            findings.append(DiscoveryFinding(
                                id=f"long-func-{rel_path.replace('/', '-')}-{node.lineno}-{node.name}",
                                type=IssueType.CODE_SMELL,
                                severity=Severity.LOW,
                                title=f"Long function: {node.name} ({length} lines)",
                                description=f"Function '{node.name}' is {length} lines long (recommended: <50)\n\nFile: {rel_path}:{node.lineno}",
                                file_path=rel_path,
                                line_number=node.lineno,
                                source="pattern_miner",
                                evidence={"function_name": node.name, "length": length},
                                suggested_fix="Consider breaking into smaller functions",
                                estimated_effort_minutes=30,
                            ))

                # Too many arguments
                if isinstance(node, ast.FunctionDef):
                    arg_count = len(node.args.args) + len(node.args.kwonlyargs)
                    if node.args.vararg:
                        arg_count += 1
                    if node.args.kwarg:
                        arg_count += 1
                    if arg_count > 7:
                        findings.append(DiscoveryFinding(
                            id=f"many-args-{rel_path.replace('/', '-')}-{node.lineno}-{node.name}",
                            type=IssueType.CODE_SMELL,
                            severity=Severity.LOW,
                            title=f"Too many arguments: {node.name} ({arg_count} args)",
                            description=f"Function '{node.name}' has {arg_count} parameters (recommended: <7)\n\nFile: {rel_path}:{node.lineno}",
                            file_path=rel_path,
                            line_number=node.lineno,
                            source="pattern_miner",
                            evidence={"function_name": node.name, "arg_count": arg_count},
                            suggested_fix="Use dataclass or config object for parameters",
                            estimated_effort_minutes=20,
                        ))

                # Bare except
                if isinstance(node, ast.ExceptHandler):
                    if node.type is None:
                        findings.append(DiscoveryFinding(
                            id=f"bare-except-{rel_path.replace('/', '-')}-{node.lineno}",
                            type=IssueType.BUG,
                            severity=Severity.MEDIUM,
                            title=f"Bare except clause",
                            description=f"Bare 'except:' catches all exceptions including SystemExit/KeyboardInterrupt\n\nFile: {rel_path}:{node.lineno}",
                            file_path=rel_path,
                            line_number=node.lineno,
                            source="pattern_miner",
                            evidence={},
                            suggested_fix="Use 'except Exception:' or specific exception types",
                            estimated_effort_minutes=10,
                        ))

        except SyntaxError:
            pass  # Skip files with syntax errors

        return findings

    def _mine_js_file(self, rel_path: str, full_path: Path, config) -> list[DiscoveryFinding]:
        """Mine patterns in JavaScript/TypeScript file."""
        findings = []

        try:
            content = full_path.read_text(encoding="utf-8")
            lines = content.split("\n")

            # TODO/FIXME
            findings.extend(self._find_todo_patterns(rel_path, lines, config))

            # Deprecated
            findings.extend(self._find_deprecated_patterns(rel_path, lines, config))

            # console.log in production code
            for i, line in enumerate(lines, 1):
                if re.search(r"console\.(log|debug|info|warn|error)\s*\(", line):
                    findings.append(DiscoveryFinding(
                        id=f"console-{rel_path.replace('/', '-')}-{i}",
                        type=IssueType.CODE_SMELL,
                        severity=Severity.LOW,
                        title=f"Console logging in {rel_path}:{i}",
                        description=f"Found console logging:\n{line.strip()}\n\nFile: {rel_path}:{i}",
                        file_path=rel_path,
                        line_number=i,
                        source="pattern_miner",
                        evidence={"line": line.strip()},
                        suggested_fix="Remove or replace with proper logging",
                        estimated_effort_minutes=5,
                    ))

        except Exception as e:
            logger.warning(f"Failed to mine {rel_path}: {e}")

        return findings