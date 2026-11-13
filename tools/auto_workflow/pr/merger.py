"""PR merge automation for auto-workflow."""

from __future__ import annotations

import time
from typing import Optional

from ..config import get_config
from ..utils.logging import get_logger
from ..utils.github_api import GitHubAPI
from ..state import WorkflowState


logger = get_logger("pr.merger")


class PRMerger:
    """Handles auto-merging of PRs."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.github = GitHubAPI(self.config.github.repo)

    def try_merge(
        self,
        pr_number: int,
        state: WorkflowState,
        require_tests: bool = True,
        require_ocr_clean: bool = True,
    ) -> tuple[bool, str | None]:
        """Attempt to merge PR, return (success, error_message)."""
        logger.info(f"Attempting to merge PR #{pr_number}")

        # Check PR status
        pr_info = self.github.get_pr(pr_number)
        if pr_info.state != "open":
            return False, f"PR is not open (state: {pr_info.state})"

        if pr_info.merged:
            return True, None

        # Enable auto-merge
        if self.config.merge.auto_merge:
            logger.info(f"Enabling auto-merge for PR #{pr_number}")
            success = self.github.enable_auto_merge(pr_number, self.config.merge.merge_method)
            if not success:
                logger.warning("Failed to enable auto-merge, trying manual merge")

        # Wait for merge
        timeout = self.config.merge.merge_timeout_minutes * 60
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Refresh PR info
            pr_info = self.github.get_pr(pr_number)

            if pr_info.merged:
                logger.info(f"PR #{pr_number} merged successfully!")
                return True, None

            if pr_info.state == "closed":
                return False, "PR was closed without merging"

            # Check mergeable status
            if pr_info.mergeable == "CONFLICTING":
                return False, "PR has merge conflicts"

            # Check checks if required
            if require_tests:
                checks = self.github.get_pr_checks(pr_number)
                if checks:
                    all_complete = all(c["status"] == "completed" for c in checks)
                    if all_complete:
                        all_success = all(c["conclusion"] == "success" for c in checks)
                        if not all_success:
                            failed = [c for c in checks if c["conclusion"] != "success"]
                            return False, f"Checks failed: {[c['name'] for c in failed]}"

            time.sleep(10)

        return False, f"Merge timeout after {self.config.merge.merge_timeout_minutes} minutes"

    def manual_merge(self, pr_number: int) -> tuple[bool, str | None]:
        """Manually merge PR (if auto-merge not enabled)."""
        pr_info = self.github.get_pr(pr_number)
        if pr_info.merged:
            return True, None

        success = self.github.merge_pr(
            pr_number,
            method=self.config.merge.merge_method,
            delete_branch=True,
        )

        if success:
            # Verify merged
            time.sleep(2)
            pr_info = self.github.get_pr(pr_number)
            if pr_info.merged:
                return True, None
            return False, "Merge reported success but PR not merged"

        return False, "Merge command failed"