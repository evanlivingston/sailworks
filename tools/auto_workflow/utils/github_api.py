"""GitHub API wrapper using gh CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


@dataclass
class PRInfo:
    number: int
    title: str
    url: str
    state: str
    head_ref: str
    base_ref: str
    mergeable: str | None = None
    merged: bool = False
    merged_at: str | None = None


@dataclass
class WorkflowRun:
    id: int
    name: str
    status: str
    conclusion: str | None
    html_url: str


class GitHubAPI:
    """Wrapper for GitHub operations via gh CLI."""

    def __init__(self, repo: str | None = None):
        self.repo = repo or "evanlivingston/sailworks"

    def _run_gh(self, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
        """Run gh command."""
        cmd = ["gh"] + args
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "dev",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        reviewers: list[str] | None = None,
        draft: bool = False,
    ) -> PRInfo:
        """Create a pull request."""
        args = [
            "pr", "create",
            "--repo", self.repo,
            "--title", title,
            "--body", body,
            "--head", head,
            "--base", base,
        ]
        if labels:
            args.extend(["--label", ",".join(labels)])
        if assignees:
            args.extend(["--assignee", ",".join(assignees)])
        if reviewers:
            args.extend(["--reviewer", ",".join(reviewers)])
        if draft:
            args.append("--draft")

        result = self._run_gh(args, check=True)
        # Parse PR number from output (URL)
        import re
        match = re.search(r"/pull/(\d+)", result.stdout)
        pr_number = int(match.group(1)) if match else 0

        return self.get_pr(pr_number)

    def get_pr(self, pr_number: int) -> PRInfo:
        """Get PR information."""
        result = self._run_gh([
            "pr", "view", str(pr_number),
            "--repo", self.repo,
            "--json", "number,title,url,state,headRefName,baseRefName,mergeable,merged,mergedAt"
        ], check=True)
        data = json.loads(result.stdout)
        return PRInfo(
            number=data["number"],
            title=data["title"],
            url=data["url"],
            state=data["state"],
            head_ref=data["headRefName"],
            base_ref=data["baseRefName"],
            mergeable=data.get("mergeable"),
            merged=data.get("merged", False),
            merged_at=data.get("mergedAt"),
        )

    def update_pr(self, pr_number: int, title: str | None = None, body: str | None = None) -> None:
        """Update PR title/body."""
        args = ["pr", "edit", str(pr_number), "--repo", self.repo]
        if title:
            args.extend(["--title", title])
        if body:
            args.extend(["--body", body])
        self._run_gh(args, check=True)

    def merge_pr(
        self,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
        auto: bool = False,
    ) -> bool:
        """Merge a pull request."""
        args = ["pr", "merge", str(pr_number), "--repo", self.repo]
        if method:
            args.extend(["--"+method])
        if delete_branch:
            args.append("--delete-branch")
        if auto:
            args.append("--auto")

        result = self._run_gh(args)
        return result.returncode == 0

    def enable_auto_merge(
        self,
        pr_number: int,
        method: str = "squash",
    ) -> bool:
        """Enable auto-merge on PR."""
        result = self._run_gh([
            "pr", "merge", str(pr_number),
            "--repo", self.repo,
            "--auto",
            "--"+method,
        ])
        return result.returncode == 0

    def get_pr_checks(self, pr_number: int) -> list[dict]:
        """Get PR check runs status."""
        result = self._run_gh([
            "pr", "checks", str(pr_number),
            "--repo", self.repo,
            "--json", "name,status,conclusion,detailsUrl"
        ])
        if result.returncode == 0:
            return json.loads(result.stdout)
        return []

    def wait_for_checks(
        self,
        pr_number: int,
        timeout_seconds: int = 300,
        poll_interval: int = 10,
    ) -> bool:
        """Wait for PR checks to complete."""
        import time
        start = time.time()
        while time.time() - start < timeout_seconds:
            checks = self.get_pr_checks(pr_number)
            if not checks:
                time.sleep(poll_interval)
                continue

            all_complete = all(c["status"] == "completed" for c in checks)
            if all_complete:
                all_success = all(c["conclusion"] == "success" for c in checks)
                return all_success

            time.sleep(poll_interval)
        return False

    def add_pr_comment(self, pr_number: int, body: str) -> bool:
        """Add comment to PR."""
        result = self._run_gh([
            "pr", "comment", str(pr_number),
            "--repo", self.repo,
            "--body", body,
        ])
        return result.returncode == 0

    def get_workflow_runs(self, branch: str | None = None, limit: int = 10) -> list[WorkflowRun]:
        """Get recent workflow runs."""
        args = [
            "run", "list",
            "--repo", self.repo,
            "--limit", str(limit),
            "--json", "id,name,status,conclusion,url",
        ]
        if branch:
            args.extend(["--branch", branch])
        result = self._run_gh(args, check=True)
        data = json.loads(result.stdout)
        return [WorkflowRun(**item) for item in data]

    def trigger_workflow(self, workflow_file: str, ref: str = "dev") -> bool:
        """Trigger a workflow dispatch."""
        result = self._run_gh([
            "workflow", "run", workflow_file,
            "--repo", self.repo,
            "--ref", ref,
        ])
        return result.returncode == 0

    def get_file_content(self, path: str, ref: str = "dev") -> str | None:
        """Get file content from repository."""
        result = self._run_gh([
            "api", f"/repos/{self.repo}/contents/{path}",
            "--jq", ".content",
            "-F", f"ref={ref}",
        ])
        if result.returncode == 0:
            import base64
            return base64.b64decode(result.stdout.strip()).decode("utf-8")
        return None

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> int | None:
        """Create a GitHub issue."""
        args = [
            "issue", "create",
            "--repo", self.repo,
            "--title", title,
            "--body", body,
        ]
        if labels:
            args.extend(["--label", ",".join(labels)])
        if assignees:
            args.extend(["--assignee", ",".join(assignees)])

        result = self._run_gh(args, check=True)
        import re
        match = re.search(r"/issues/(\d+)", result.stdout)
        return int(match.group(1)) if match else None