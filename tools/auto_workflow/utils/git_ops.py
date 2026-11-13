"""Git operations wrapper for auto-workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


@dataclass
class GitResult:
    success: bool
    stdout: str
    stderr: str
    returncode: int


class GitOps:
    """Wrapper for git operations with error handling."""

    def __init__(self, repo_path: Path | None = None):
        self.repo_path = repo_path or Path.cwd()

    def _run(self, args: list[str], cwd: Path | None = None, check: bool = False) -> GitResult:
        """Run git command."""
        cmd = ["git"] + args
        result = subprocess.run(
            cmd,
            cwd=cwd or self.repo_path,
            capture_output=True,
            text=True,
        )
        git_result = GitResult(
            success=result.returncode == 0,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )
        if check and not git_result.success:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return git_result

    def fetch(self, remote: str = "origin", prune: bool = True) -> GitResult:
        """Fetch from remote."""
        args = ["fetch", remote]
        if prune:
            args.append("--prune")
        return self._run(args, check=True)

    def rev_parse(self, ref: str, cwd: Path | None = None) -> GitResult:
        """Resolve ref to commit hash."""
        return self._run(["rev-parse", ref], cwd=cwd, check=True)

    def current_branch(self, cwd: Path | None = None) -> GitResult:
        """Get current branch name."""
        return self._run(["branch", "--show-current"], cwd=cwd)

    def status(self, cwd: Path | None = None, porcelain: bool = True) -> GitResult:
        """Get working tree status."""
        args = ["status"]
        if porcelain:
            args.append("--porcelain")
        return self._run(args, cwd=cwd)

    def diff(self, *args: str, cwd: Path | None = None) -> GitResult:
        """Show diff."""
        return self._run(["diff"] + list(args), cwd=cwd)

    def add(self, paths: list[str], cwd: Path | None = None) -> GitResult:
        """Stage files."""
        return self._run(["add"] + paths, cwd=cwd, check=True)

    def commit(self, message: str, cwd: Path | None = None, allow_empty: bool = False) -> GitResult:
        """Create commit."""
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        return self._run(args, cwd=cwd, check=True)

    def push(self, remote: str = "origin", branch: str | None = None, force: bool = False) -> GitResult:
        """Push to remote."""
        args = ["push", remote]
        if force:
            args.append("--force-with-lease")
        if branch:
            args.append(branch)
        return self._run(args, check=True)

    def checkout(self, branch: str, create: bool = False, cwd: Path | None = None) -> GitResult:
        """Checkout branch."""
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        return self._run(args, cwd=cwd, check=True)

    def branch_exists(self, branch: str, remote: bool = False) -> bool:
        """Check if branch exists."""
        args = ["branch"]
        if remote:
            args.append("-r")
        args.extend(["--list", branch])
        result = self._run(args)
        return bool(result.stdout.strip())

    def merge_base(self, branch1: str, branch2: str) -> GitResult:
        """Find merge base."""
        return self._run(["merge-base", branch1, branch2])

    def log(self, *args: str, limit: int = 10, cwd: Path | None = None) -> GitResult:
        """Show commit log."""
        cmd = ["log", f"--max-count={limit}", "--oneline"] + list(args)
        return self._run(cmd, cwd=cwd)

    def worktree_add(self, path: Path, branch: str, base: str = "origin/dev") -> GitResult:
        """Add worktree."""
        return self._run(["worktree", "add", "-b", branch, str(path), base], check=True)

    def worktree_remove(self, path: Path, force: bool = False) -> GitResult:
        """Remove worktree."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        return self._run(args)

    def worktree_list(self) -> GitResult:
        """List worktrees."""
        return self._run(["worktree", "list", "--porcelain"])

    def get_changed_files(self, base: str = "origin/dev", head: str = "HEAD") -> list[str]:
        """Get list of changed files between base and head."""
        result = self._run(["diff", "--name-only", f"{base}..{head}"])
        if result.success:
            return [f for f in result.stdout.split("\n") if f]
        return []

    def get_commit_message(self, commit: str = "HEAD") -> GitResult:
        """Get commit message."""
        return self._run(["log", "-1", "--pretty=%B", commit])

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Check if ancestor is ancestor of descendant."""
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant])
        return result.success

    def stash(self, cwd: Path | None = None) -> GitResult:
        """Stash changes."""
        return self._run(["stash", "push", "-u"], cwd=cwd)

    def stash_pop(self, cwd: Path | None = None) -> GitResult:
        """Pop stash."""
        return self._run(["stash", "pop"], cwd=cwd)

    def clean(self, cwd: Path | None = None, force: bool = True) -> GitResult:
        """Clean untracked files."""
        args = ["clean"]
        if force:
            args.append("-f")
        args.append("-d")
        return self._run(args, cwd=cwd)

    def reset_hard(self, commit: str = "HEAD", cwd: Path | None = None) -> GitResult:
        """Hard reset to commit."""
        return self._run(["reset", "--hard", commit], cwd=cwd, check=True)