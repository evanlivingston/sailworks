"""OpenCode subagent runner for implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from ..config import get_config
from ..utils.logging import get_logger
from ..state import WorkflowState
from ..utils.git_ops import GitOps


logger = get_logger("implement.agent")


@dataclass
class ImplementationResult:
    success: bool
    commits: list[str]
    changed_files: list[str]
    error: str | None = None
    duration_seconds: float = 0.0


class AgentRunner:
    """Runs OpenCode subagents to implement fixes."""

    def __init__(self, config=None, state_manager=None):
        self.config = config or get_config()
        self.state_manager = state_manager

    def implement_fix(
        self,
        finding: Any,  # DiscoveryFinding
        worktree_path: Path,
        run_id: str,
        state: WorkflowState,
    ) -> ImplementationResult:
        """Run subagent to implement a fix."""
        import time
        start_time = time.time()

        logger.info(f"Starting implementation for {finding.id}")

        # Prepare implementation prompt
        prompt = self._build_prompt(finding, worktree_path, run_id)

        # Determine subagent tier
        tier = self.config.implementation.subagent_tier

        # Run subagent
        try:
            result = self._run_subagent(prompt, tier, worktree_path)
        except Exception as e:
            logger.error(f"Subagent failed: {e}")
            return ImplementationResult(
                success=False,
                commits=[],
                changed_files=[],
                error=str(e),
                duration_seconds=time.time() - start_time,
            )

        # Verify changes
        git_ops = GitOps(worktree_path)
        changed_files = self._get_changed_files(git_ops, worktree_path)

        if not changed_files:
            logger.warning("No changes made by subagent")
            return ImplementationResult(
                success=False,
                commits=[],
                changed_files=[],
                error="No changes made",
                duration_seconds=time.time() - start_time,
            )

        # Create commit
        commit_msg = self._build_commit_message(finding, run_id)
        commit_result = git_ops.commit(commit_msg, cwd=worktree_path, allow_empty=False)

        if not commit_result.success:
            logger.error(f"Commit failed: {commit_result.stderr}")
            return ImplementationResult(
                success=False,
                commits=[],
                changed_files=changed_files,
                error=f"Commit failed: {commit_result.stderr}",
                duration_seconds=time.time() - start_time,
            )

        # Get commit hash
        commit_hash = git_ops.rev_parse("HEAD", cwd=worktree_path).stdout.strip()

        duration = time.time() - start_time
        logger.info(f"Implementation completed in {duration:.1f}s, commit: {commit_hash[:8]}")

        return ImplementationResult(
            success=True,
            commits=[commit_hash],
            changed_files=changed_files,
            duration_seconds=duration,
        )

    def _build_prompt(self, finding: Any, worktree_path: Path, run_id: str) -> str:
        """Build implementation prompt for subagent."""
        return f"""You are an automated code agent fixing an issue in the sailworks repository.

## Issue Details
- **ID**: {finding.id}
- **Type**: {finding.type.value}
- **Severity**: {finding.severity.value}
- **Title**: {finding.title}
- **File**: {finding.file_path or 'N/A'}
- **Line**: {finding.line_number or 'N/A'}

## Description
{finding.description}

## Suggested Fix
{finding.suggested_fix or 'No specific suggestion provided'}

## Repository Context
- This is a Python/Qt sailing navigation application
- Follow the AGENTS.md workflow rules in the repository
- Preserve calculation fidelity when optimizing
- Keep commits coherent and independently understandable
- Run validation: targeted tests + black --check + git diff --check

## Your Task
1. Analyze the issue and understand the codebase context
2. Implement a minimal, focused fix
3. Run relevant tests to verify the fix works
4. Ensure code style passes (black, no whitespace errors)
5. Do NOT create documentation files - record results in the issue/PR

## Worktree
You are working in: {worktree_path}
Branch: auto/{run_id}-{finding.id[:30]}

## Validation Commands
- Run targeted tests for changed modules: `uv run pytest <test_file> -v`
- Check formatting: `uv run black --check <changed_files>`
- Check whitespace: `git diff --check`

Return a summary of what you changed and any test results.
"""

    def _run_subagent(self, prompt: str, tier: str, worktree_path: Path) -> Any:
        """Run OpenCode subagent."""
        # This is called from within an OpenCode session, so we can use the subagent tool
        # The actual invocation happens via the main orchestration script which has access to tools
        # For now, we'll use a direct approach - the main script will call this via execute tool
        raise NotImplementedError("Subagent execution handled by main orchestration")

    def _get_changed_files(self, git_ops: GitOps, worktree_path: Path) -> list[str]:
        """Get list of changed files in worktree."""
        status = git_ops.status(cwd=worktree_path)
        files = []
        for line in status.stdout.split("\n"):
            if line.strip():
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    files.append(parts[1])
        return files

    def _build_commit_message(self, finding: Any, run_id: str) -> str:
        """Build commit message from template."""
        template = self.config.implementation.commit_message_template
        if not template:
            template = """{type}: {title}

{description}

Automated fix by auto-workflow (run: {run_id})
Issue: {issue_id}"""

        return template.format(
            type=finding.type.value,
            title=finding.title,
            description=finding.description,
            run_id=run_id,
            issue_id=finding.id,
        )

    def run_validation(self, worktree_path: Path, changed_files: list[str]) -> tuple[bool, str]:
        """Run validation checks on changes."""
        git_ops = GitOps(worktree_path)

        # Run black --check
        py_files = [f for f in changed_files if f.endswith(".py")]
        if py_files:
            result = subprocess.run(
                ["uv", "run", "black", "--check"] + py_files,
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False, f"Black formatting check failed: {result.stdout}"

        # Run git diff --check
        result = git_ops._run(["diff", "--check"], cwd=worktree_path)
        if not result.success:
            return False, f"Whitespace errors: {result.stderr}"

        # Run targeted tests
        test_files = self._find_test_files(changed_files, worktree_path)
        if test_files:
            result = subprocess.run(
                ["uv", "run", "pytest"] + test_files + ["-v", "--tb=short"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                return False, f"Tests failed: {result.stdout}"

        return True, "Validation passed"

    def _find_test_files(self, changed_files: list[str], worktree_path: Path) -> list[str]:
        """Find relevant test files for changed files."""
        test_files = []
        for cf in changed_files:
            # Map source file to test file
            if cf.startswith("sailworks/"):
                test_path = cf.replace("sailworks/", "test/test_").replace(".py", ".py")
                if not test_path.startswith("test/"):
                    test_path = "test/" + test_path
                full_path = worktree_path / test_path
                if full_path.exists():
                    test_files.append(test_path)
        return test_files


# Import subprocess at module level for validation
import subprocess