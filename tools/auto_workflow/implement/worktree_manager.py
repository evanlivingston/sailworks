"""Worktree management for isolated development."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from ..config import get_config
from ..utils.logging import get_logger
from ..utils.git_ops import GitOps


logger = get_logger("implement.worktree")


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    slug: str


class WorktreeManager:
    """Manages git worktrees for automated development."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.base_path = Path(os.path.expanduser(self.config.worktree.base_path))
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.git_ops = GitOps()

    def create_worktree(self, slug: str, base_branch: str = "origin/dev") -> WorktreeInfo:
        """Create a new worktree for automated work."""
        # Sanitize slug
        safe_slug = slug.replace("/", "-").replace(" ", "-")[:50]
        branch_name = f"auto/{safe_slug}"
        worktree_path = self.base_path / safe_slug

        logger.info(f"Creating worktree: {worktree_path} (branch: {branch_name})")

        # Fetch latest
        self.git_ops.fetch("origin", prune=True)

        # Check if branch already exists locally
        if self.git_ops.branch_exists(branch_name):
            logger.warning(f"Branch {branch_name} already exists, removing old worktree")
            self.remove_worktree(safe_slug)

        # Create worktree using existing script
        script_path = Path(__file__).parent.parent.parent / "tools" / "wt-new.sh"
        if script_path.exists():
            result = subprocess.run(
                [str(script_path), safe_slug],
                capture_output=True,
                text=True,
                cwd=worktree_path.parent.parent if worktree_path.parent.name == "wt" else Path.cwd(),
            )
            if result.returncode != 0:
                # Script might fail if not in right directory, try manual
                logger.warning(f"wt-new.sh failed: {result.stderr}, trying manual")
                return self._create_worktree_manual(safe_slug, branch_name, base_branch)
        else:
            return self._create_worktree_manual(safe_slug, branch_name, base_branch)

        # Verify worktree was created
        if not worktree_path.exists():
            raise RuntimeError(f"Worktree not created at {worktree_path}")

        # Verify branch
        git_ops = GitOps(worktree_path)
        current_branch = git_ops.current_branch(cwd=worktree_path).stdout.strip()
        if current_branch != branch_name:
            logger.warning(f"Expected branch {branch_name}, got {current_branch}")

        return WorktreeInfo(path=worktree_path, branch=branch_name, slug=safe_slug)

    def _create_worktree_manual(self, slug: str, branch_name: str, base_branch: str) -> WorktreeInfo:
        """Create worktree manually without script."""
        worktree_path = self.base_path / slug

        result = self.git_ops.worktree_add(worktree_path, branch_name, base_branch)
        if not result.success:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        return WorktreeInfo(path=worktree_path, branch=branch_name, slug=slug)

    def remove_worktree(self, slug: str, force: bool = False) -> bool:
        """Remove a worktree."""
        worktree_path = self.base_path / slug
        if not worktree_path.exists():
            logger.warning(f"Worktree not found: {worktree_path}")
            return True

        logger.info(f"Removing worktree: {worktree_path}")
        result = self.git_ops.worktree_remove(worktree_path, force=force)
        return result.success

    def prune_worktrees(self, apply: bool = False) -> list[str]:
        """Prune clean, merged worktrees."""
        script_path = Path(__file__).parent.parent.parent / "tools" / "wt-prune.sh"
        if script_path.exists():
            result = subprocess.run(
                [str(script_path), "--apply" if apply else ""],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n") if result.stdout else []
        return []

    def list_worktrees(self) -> list[WorktreeInfo]:
        """List all worktrees."""
        result = self.git_ops.worktree_list()
        worktrees = []

        if result.success:
            # Parse porcelain output
            current_path = None
            current_branch = None
            for line in result.stdout.split("\n"):
                if line.startswith("worktree "):
                    current_path = Path(line[9:])
                elif line.startswith("branch "):
                    current_branch = line[7:]
                    if current_branch.startswith("refs/heads/"):
                        current_branch = current_branch[11:]
                elif line == "" and current_path:
                    slug = current_path.name
                    worktrees.append(WorktreeInfo(
                        path=current_path,
                        branch=current_branch or "",
                        slug=slug,
                    ))
                    current_path = None
                    current_branch = None

        return worktrees

    def get_worktree_path(self, slug: str) -> Optional[Path]:
        """Get worktree path by slug."""
        worktree_path = self.base_path / slug
        return worktree_path if worktree_path.exists() else None