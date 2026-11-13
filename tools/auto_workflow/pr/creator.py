"""PR creation for auto-workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from ..config import get_config
from ..utils.logging import get_logger
from ..utils.github_api import GitHubAPI, PRInfo
from ..utils.git_ops import GitOps
from ..state import WorkflowState


logger = get_logger("pr.creator")


@dataclass
class PRCreationResult:
    success: bool
    pr_info: PRInfo | None = None
    error: str | None = None


class PRCreator:
    """Creates pull requests for automated fixes."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.github = GitHubAPI(self.config.github.repo)
        self.git_ops = GitOps()

    def create_pr(
        self,
        worktree_path: Path,
        branch_name: str,
        finding: Any,  # DiscoveryFinding
        run_id: str,
        state: WorkflowState,
    ) -> PRCreationResult:
        """Create a PR for the implemented fix."""
        logger.info(f"Creating PR for branch {branch_name}")

        # Push branch first
        push_result = self.git_ops.push("origin", branch_name, force=True)
        if not push_result.success:
            return PRCreationResult(
                success=False,
                error=f"Failed to push branch: {push_result.stderr}",
            )

        # Generate PR title and body
        title = self._generate_title(finding)
        body = self._generate_body(finding, run_id, worktree_path)

        # Create PR
        try:
            pr_info = self.github.create_pr(
                title=title,
                body=body,
                head=branch_name,
                base=self.config.github.base_branch,
                labels=self.config.github.pr_labels,
                assignees=self.config.github.pr_assignees,
                reviewers=self.config.github.pr_reviewers,
                draft=False,
            )

            logger.info(f"Created PR #{pr_info.number}: {pr_info.url}")
            return PRCreationResult(success=True, pr_info=pr_info)

        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return PRCreationResult(success=False, error=str(e))

    def _generate_title(self, finding: Any) -> str:
        """Generate PR title."""
        type_prefix = {
            "bug": "fix",
            "optimization": "perf",
            "refactor": "refactor",
            "security": "security",
            "performance": "perf",
            "test_failure": "test",
            "code_smell": "cleanup",
            "deprecation": "deprecation",
            "documentation": "docs",
        }.get(finding.type.value, "fix")

        return f"[{type_prefix}] {finding.title}"

    def _generate_body(self, finding: Any, run_id: str, worktree_path: Path) -> str:
        """Generate PR body with context."""
        # Get commit info
        git_ops = GitOps(worktree_path)
        commits = git_ops.log("--oneline", f"origin/{self.config.github.base_branch}..HEAD", limit=5).stdout

        body = f"""## Automated Fix by auto-workflow

**Run ID**: {run_id}
**Issue ID**: {finding.id}
**Type**: {finding.type.value}
**Severity**: {finding.severity.value}

## Description
{finding.description}

## Changes
{commits or "No commits found"}

## Testing
- Targeted tests for changed modules
- `black --check` formatting validation
- `git diff --check` whitespace validation

## OCR Review
This PR will be reviewed by OpenCodeReview. Any findings will be automatically addressed in follow-up commits.

---
*This PR was created automatically by the auto-workflow system.*
"""
        return body

    def update_pr_description(self, pr_number: int, additional_info: str) -> bool:
        """Update PR description with additional info."""
        try:
            pr_info = self.github.get_pr(pr_number)
            new_body = pr_info.body + "\n\n---\n\n" + additional_info if pr_info.body else additional_info
            self.github.update_pr(pr_number, body=new_body)
            return True
        except Exception as e:
            logger.error(f"Failed to update PR: {e}")
            return False