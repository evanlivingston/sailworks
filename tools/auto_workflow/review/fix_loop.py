"""Iterative fix loop for OCR findings."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from ..config import get_config
from ..utils.logging import get_logger
from ..implement import AgentRunner, WorktreeManager
from .ocr_runner import OCRRunner, OCRResult, OCRFinding


logger = get_logger("review.fix_loop")


@dataclass
class FixLoopResult:
    success: bool
    final_ocr_result: OCRResult | None
    iterations: int
    total_fixes: int
    error: str | None = None


class FixLoop:
    """Runs iterative fix loop until OCR is clean."""

    def __init__(self, config=None, state_manager=None):
        self.config = config or get_config()
        self.state_manager = state_manager
        self.ocr_runner = OCRRunner(config)
        self.worktree_manager = WorktreeManager(config)
        self.agent_runner = AgentRunner(config, state_manager)

    def run_fix_loop(
        self,
        worktree_path: Path,
        branch_name: str,
        base_branch: str,
        run_id: str,
        state: Any,
    ) -> FixLoopResult:
        """Run fix loop until OCR passes or max iterations reached."""
        max_iterations = self.config.review.ocr_max_iterations
        auto_fix_types = self.config.review.ocr_auto_fix_types
        blocker_threshold = self.config.review.ocr_blocker_threshold

        logger.info(f"Starting fix loop (max {max_iterations} iterations)")

        total_fixes = 0

        for iteration in range(1, max_iterations + 1):
            logger.info(f"Fix loop iteration {iteration}/{max_iterations}")

            # Run OCR review
            ocr_result = self.ocr_runner.review_pr(branch_name, base_branch, worktree_path)

            if not ocr_result.success:
                logger.error(f"OCR review failed: {ocr_result.error}")
                return FixLoopResult(
                    success=False,
                    final_ocr_result=ocr_result,
                    iterations=iteration,
                    total_fixes=total_fixes,
                    error=ocr_result.error,
                )

            # Record iteration in state
            if self.state_manager:
                self.state_manager.record_ocr_iteration(state, iteration, len(ocr_result.findings))

            # Check if clean
            if not ocr_result.findings:
                logger.info("OCR review clean - no findings!")
                return FixLoopResult(
                    success=True,
                    final_ocr_result=ocr_result,
                    iterations=iteration,
                    total_fixes=total_fixes,
                )

            # Check for blockers
            if self.ocr_runner.has_blockers(ocr_result.findings, blocker_threshold):
                logger.warning(f"Blockers found (threshold: {blocker_threshold})")

            # Filter findings to auto-fix
            fixable_findings = self.ocr_runner.filter_findings(ocr_result.findings, auto_fix_types)

            if not fixable_findings:
                logger.info("No auto-fixable findings, stopping loop")
                return FixLoopResult(
                    success=False,
                    final_ocr_result=ocr_result,
                    iterations=iteration,
                    total_fixes=total_fixes,
                    error="No auto-fixable findings remaining",
                )

            logger.info(f"Found {len(fixable_findings)} auto-fixable findings")

            # Fix each finding
            for finding in fixable_findings:
                # Convert OCRFinding to DiscoveryFinding for agent
                discovery_finding = self._convert_finding(finding)

                # Run agent to fix
                impl_result = self.agent_runner.implement_fix(
                    discovery_finding, worktree_path, run_id, state
                )

                if impl_result.success:
                    total_fixes += 1
                    logger.info(f"Fixed {finding.file}:{finding.line} - {finding.message[:50]}")
                else:
                    logger.error(f"Failed to fix {finding.file}:{finding.line}: {impl_result.error}")

            # Push fixes
            self._push_changes(worktree_path)

            # Small delay to let GitHub update
            time.sleep(5)

        # Max iterations reached
        final_ocr = self.ocr_runner.review_pr(branch_name, base_branch, worktree_path)
        logger.warning(f"Max iterations ({max_iterations}) reached")

        return FixLoopResult(
            success=not final_ocr.findings if final_ocr.success else False,
            final_ocr_result=final_ocr,
            iterations=max_iterations,
            total_fixes=total_fixes,
            error="Max iterations reached" if final_ocr.findings else None,
        )

    def _convert_finding(self, ocr_finding: OCRFinding) -> Any:
        """Convert OCRFinding to DiscoveryFinding for agent."""
        from ..discover import DiscoveryFinding, IssueType, Severity

        severity_map = {
            "blocker": Severity.CRITICAL,
            "major": Severity.HIGH,
            "minor": Severity.MEDIUM,
            "suggestion": Severity.LOW,
        }

        category_map = {
            "bug": IssueType.BUG,
            "security": IssueType.SECURITY,
            "performance": IssueType.PERFORMANCE,
            "style": IssueType.CODE_SMELL,
            "best_practice": IssueType.REFACTOR,
            "optimization": IssueType.OPTIMIZATION,
            "deprecation": IssueType.DEPRECATION,
            "documentation": IssueType.DOCUMENTATION,
            "test": IssueType.TEST_FAILURE,
        }

        return DiscoveryFinding(
            id=f"ocr-fix-{ocr_finding.file.replace('/', '-')}-{ocr_finding.line}-{ocr_finding.rule_id or 'unknown'}",
            type=category_map.get(ocr_finding.category.lower(), IssueType.CODE_SMELL),
            severity=severity_map.get(ocr_finding.severity, Severity.MEDIUM),
            title=f"OCR Fix: {ocr_finding.message[:80]}",
            description=f"{ocr_finding.message}\n\nFile: {ocr_finding.file}:{ocr_finding.line}\nCategory: {ocr_finding.category}\nRule: {ocr_finding.rule_id or 'N/A'}",
            file_path=ocr_finding.file,
            line_number=ocr_finding.line,
            source="ocr_review",
            evidence={
                "ocr_category": ocr_finding.category,
                "ocr_severity": ocr_finding.severity,
                "ocr_rule_id": ocr_finding.rule_id,
                "ocr_suggestion": ocr_finding.suggestion,
            },
            suggested_fix=ocr_finding.suggestion,
            estimated_effort_minutes=30,
        )

    def _push_changes(self, worktree_path: Path) -> None:
        """Push changes to remote."""
        from ..utils.git_ops import GitOps
        git_ops = GitOps(worktree_path)
        result = git_ops.push("origin", force=True)
        if not result.success:
            logger.error(f"Push failed: {result.stderr}")
            raise RuntimeError(f"Failed to push changes: {result.stderr}")