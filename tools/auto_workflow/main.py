#!/usr/bin/env python3
"""Main orchestration script for auto-workflow."""

from __future__ import annotations

import sys
import os
import argparse
import traceback
from pathlib import Path
from datetime import datetime, timezone

# Add tools to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from auto_workflow.config import get_config, reload_config
from auto_workflow.state import StateManager, WorkflowState, generate_run_id
from auto_workflow.utils.logging import setup_logging, get_logger, LogContext
from auto_workflow.utils.git_ops import GitOps
from auto_workflow.discover import (
    DiscoveryContext,
    OCRScanner,
    StaticAnalysisScanner,
    TestAnalyzer,
    PatternMiner,
    PerfRegressionDetector,
)
from auto_workflow.implement import WorktreeManager, AgentRunner
from auto_workflow.review import FixLoop
from auto_workflow.pr import PRCreator, PRMerger


logger = get_logger("main")


class AutoWorkflow:
    """Main workflow orchestrator."""

    def __init__(self, config_path: str | None = None):
        self.config = reload_config(config_path) if config_path else get_config()
        self.state_manager = StateManager(self.config)
        self.git_ops = GitOps()
        self.worktree_manager = WorktreeManager(self.config)
        self.agent_runner = AgentRunner(self.config, self.state_manager)
        self.fix_loop = FixLoop(self.config, self.state_manager)
        self.pr_creator = PRCreator(self.config)
        self.pr_merger = PRMerger(self.config)

        # Discovery methods
        self.discovery_methods = [
            OCRScanner(self.config),
            StaticAnalysisScanner(self.config),
            TestAnalyzer(self.config),
            PatternMiner(self.config),
            PerfRegressionDetector(self.config),
        ]

    def run(self, run_id: str | None = None, max_issues: int | None = None) -> int:
        """Run the complete workflow."""
        run_id = run_id or generate_run_id()
        max_issues = max_issues or self.config.implementation.max_issues_per_run

        logger.info(f"Starting auto-workflow run: {run_id}")

        # Acquire lock
        if not self.state_manager.acquire_lock():
            logger.error("Could not acquire lock - another run in progress?")
            return 1

        try:
            with LogContext(run_id=run_id, phase="initialization"):
                # Initialize state
                state = self.state_manager.start_run(run_id)

                # Prune worktrees if configured
                if self.config.worktree.prune_on_start:
                    self.worktree_manager.prune_worktrees(apply=True)

                # Fetch latest
                self.git_ops.fetch("origin", prune=True)

                # Get base commit
                base_commit = self.git_ops.rev_parse(self.config.discovery.ocr_compare_base).stdout.strip()
                head_commit = self.git_ops.rev_parse("HEAD").stdout.strip()

                if base_commit == head_commit:
                    logger.info("No new commits since last scan")
                    self.state_manager.complete_run(state, success=True)
                    return 0

                # Get changed files
                changed_files = self.git_ops.get_changed_files(base_commit, head_commit)
                logger.info(f"Changed files: {len(changed_files)}")

                # Create discovery context
                context = DiscoveryContext(
                    repo_path=str(Path.cwd()),
                    base_commit=base_commit,
                    head_commit=head_commit,
                    changed_files=changed_files,
                    config=self.config,
                    state=state,
                    run_id=run_id,
                )

            # Phase 1: Discovery
            with LogContext(run_id=run_id, phase="discovery"):
                findings = self._run_discovery(context)
                if not findings:
                    logger.info("No issues found")
                    self.state_manager.complete_run(state, success=True)
                    return 0

                # Sort by severity and select top issues
                findings.sort(key=lambda f: (f.severity.value, -f.estimated_effort_minutes))
                selected_findings = findings[:max_issues]

                logger.info(f"Selected {len(selected_findings)} issues from {len(findings)} total findings")

            # Process each selected issue
            for finding in selected_findings:
                with LogContext(run_id=run_id, phase="implementation", issue_id=finding.id):
                    success = self._process_issue(finding, context, run_id, state)
                    if not success:
                        logger.error(f"Failed to process issue {finding.id}")
                        # Continue to next issue

            # Complete run
            self.state_manager.complete_run(state, success=True)

            # Prune worktrees on end
            if self.config.worktree.prune_on_end:
                self.worktree_manager.prune_worktrees(apply=True)

            logger.info(f"Run {run_id} completed successfully")
            return 0

        except Exception as e:
            logger.error(f"Workflow failed: {e}")
            logger.error(traceback.format_exc())
            # Try to save error state
            try:
                state = self.state_manager.load()
                self.state_manager.complete_run(state, success=False)
            except Exception:
                pass
            return 1

        finally:
            self.state_manager.release_lock()

    def _run_discovery(self, context: DiscoveryContext) -> list:
        """Run all discovery methods in parallel."""
        import concurrent.futures

        all_findings = []

        # Run discovery methods
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_method = {
                executor.submit(method.discover, context): method
                for method in self.discovery_methods
                if method.is_applicable(context)
            }

            for future in concurrent.futures.as_completed(future_to_method):
                method = future_to_method[future]
                try:
                    findings = future.result(timeout=300)
                    all_findings.extend(findings)
                    logger.info(f"{method.name}: {len(findings)} findings")
                except Exception as e:
                    logger.error(f"{method.name} failed: {e}")

        return all_findings

    def _process_issue(self, finding, context: DiscoveryContext, run_id: str, state: WorkflowState) -> bool:
        """Process a single issue through implementation, review, and merge."""
        issue_slug = f"{run_id}-{finding.id[:40]}"

        # Record issue start
        self.state_manager.record_issue_start(state, finding.id, finding.type.value, finding.title)

        # Create worktree
        try:
            worktree_info = self.worktree_manager.create_worktree(issue_slug)
            self.state_manager.record_worktree(state, str(worktree_info.path), worktree_info.branch)
        except Exception as e:
            logger.error(f"Failed to create worktree: {e}")
            return False

        # Phase 2: Implementation
        with LogContext(run_id=run_id, phase="implementation", issue_id=finding.id):
            impl_result = self.agent_runner.implement_fix(finding, worktree_info.path, run_id, state)

            if not impl_result.success:
                logger.error(f"Implementation failed: {impl_result.error}")
                return False

            # Run validation
            valid, msg = self.agent_runner.run_validation(worktree_info.path, impl_result.changed_files)
            if not valid:
                logger.error(f"Validation failed: {msg}")
                return False

        # Phase 3: PR Creation
        with LogContext(run_id=run_id, phase="pr_creation", issue_id=finding.id):
            pr_result = self.pr_creator.create_pr(
                worktree_info.path,
                worktree_info.branch,
                finding,
                run_id,
                state,
            )

            if not pr_result.success:
                logger.error(f"PR creation failed: {pr_result.error}")
                return False

            pr_number = pr_result.pr_info.number
            self.state_manager.record_pr_created(state, pr_number)

        # Phase 4: OCR Review Loop
        with LogContext(run_id=run_id, phase="review", issue_id=finding.id):
            fix_result = self.fix_loop.run_fix_loop(
                worktree_info.path,
                worktree_info.branch,
                self.config.github.base_branch,
                run_id,
                state,
            )

            if not fix_result.success:
                logger.error(f"Fix loop failed: {fix_result.error}")
                # PR remains open for manual review
                return False

        # Phase 5: Merge
        with LogContext(run_id=run_id, phase="merge", issue_id=finding.id):
            merge_success, merge_error = self.pr_merger.try_merge(
                pr_number,
                state,
                require_tests=self.config.merge.require_tests_pass,
                require_ocr_clean=self.config.merge.require_ocr_clean,
            )

            self.state_manager.record_merge_result(state, merge_success, pr_number)

            if not merge_success:
                logger.error(f"Merge failed: {merge_error}")
                return False

        logger.info(f"Successfully processed issue {finding.id} -> PR #{pr_number}")
        return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Auto-workflow orchestration")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--run-id", help="Run ID (auto-generated if not provided)")
    parser.add_argument("--max-issues", type=int, help="Max issues per run")
    parser.add_argument("--dry-run", action="store_true", help="Run discovery only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    # Set up logging
    config = reload_config(args.config) if args.config else get_config()
    if args.verbose:
        config.logging.level = "DEBUG"
    setup_logging(config)

    # Run workflow
    workflow = AutoWorkflow(args.config)

    if args.dry_run:
        logger.info("DRY RUN - Discovery only")
        # TODO: Implement dry run
        return 0

    return workflow.run(args.run_id, args.max_issues)


if __name__ == "__main__":
    sys.exit(main())