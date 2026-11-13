"""State persistence for auto-workflow."""

from __future__ import annotations

import json
import fcntl
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass, asdict, field
from contextlib import contextmanager

from .config import get_config


@dataclass
class CompletedIssue:
    id: str
    type: str  # bug, optimization, refactor, etc.
    title: str
    pr_number: int
    merged: bool
    merged_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ocr_iterations: int = 0
    implementation_minutes: float = 0.0
    review_minutes: float = 0.0


@dataclass
class CurrentRun:
    run_id: str
    started_at: str
    issue_id: str | None = None
    issue_type: str | None = None
    issue_title: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    phase: str = "discovery"  # discovery, implementation, review, merge, complete
    ocr_iteration: int = 0
    findings_count: int = 0


@dataclass
class Metrics:
    total_runs: int = 0
    issues_fixed: int = 0
    prs_created: int = 0
    prs_merged: int = 0
    prs_failed: int = 0
    ocr_iterations_total: int = 0
    total_implementation_minutes: float = 0.0
    total_review_minutes: float = 0.0
    last_successful_run: str | None = None
    last_failed_run: str | None = None


@dataclass
class WorkflowState:
    last_run: str | None = None
    last_scanned_commit: str | None = None
    completed_issues: list[CompletedIssue] = field(default_factory=list)
    current_run: CurrentRun | None = None
    metrics: Metrics = field(default_factory=Metrics)


class StateManager:
    """Manages workflow state with file locking."""

    def __init__(self, config=None):
        self.config = config or get_config()
        self.state_file = Path(self.config.state.file)
        self.lock_file = Path(self.config.state.lock_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = None

    def load(self) -> WorkflowState:
        """Load state from file."""
        if not self.state_file.exists():
            return WorkflowState()

        try:
            with open(self.state_file) as f:
                data = json.load(f)
            return self._deserialize(data)
        except (json.JSONDecodeError, KeyError) as e:
            # Corrupted state - backup and start fresh
            backup = self.state_file.with_suffix(f".bak.{int(time.time())}")
            self.state_file.rename(backup)
            return WorkflowState()

    def save(self, state: WorkflowState) -> None:
        """Save state to file atomically."""
        # Atomic write via temp file
        temp_file = self.state_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            json.dump(self._serialize(state), f, indent=2)
        temp_file.replace(self.state_file)

    @contextmanager
    def lock(self, blocking: bool = True, timeout: float | None = None):
        """Acquire file lock for exclusive access."""
        if timeout is None:
            timeout = self.config.state.lock_timeout_minutes * 60

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = open(self.lock_file, "w")

        try:
            if blocking:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except BlockingIOError:
            yield False
        finally:
            if self._lock_fd:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
                self._lock_fd = None

    def acquire_lock(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Try to acquire lock, return success."""
        if timeout is None:
            timeout = self.config.state.lock_timeout_minutes * 60

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = open(self.lock_file, "w")

        try:
            if blocking:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def release_lock(self) -> None:
        """Release the lock."""
        if self._lock_fd:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
            self._lock_fd = None

    def _serialize(self, state: WorkflowState) -> dict:
        """Serialize state to dictionary."""
        return {
            "last_run": state.last_run,
            "last_scanned_commit": state.last_scanned_commit,
            "completed_issues": [asdict(issue) for issue in state.completed_issues],
            "current_run": asdict(state.current_run) if state.current_run else None,
            "metrics": asdict(state.metrics),
        }

    def _deserialize(self, data: dict) -> WorkflowState:
        """Deserialize state from dictionary."""
        completed = [CompletedIssue(**item) for item in data.get("completed_issues", [])]
        current = data.get("current_run")
        current_run = CurrentRun(**current) if current else None
        metrics = Metrics(**data.get("metrics", {}))
        return WorkflowState(
            last_run=data.get("last_run"),
            last_scanned_commit=data.get("last_scanned_commit"),
            completed_issues=completed,
            current_run=current_run,
            metrics=metrics,
        )

    def start_run(self, run_id: str) -> WorkflowState:
        """Initialize a new run."""
        state = self.load()
        state.last_run = datetime.now(timezone.utc).isoformat()
        state.current_run = CurrentRun(
            run_id=run_id,
            started_at=state.last_run,
            phase="discovery",
        )
        state.metrics.total_runs += 1
        self.save(state)
        return state

    def complete_run(self, state: WorkflowState, success: bool = True) -> None:
        """Mark run as complete."""
        if state.current_run:
            state.current_run.phase = "complete"
        if success:
            state.metrics.last_successful_run = datetime.now(timezone.utc).isoformat()
        else:
            state.metrics.last_failed_run = datetime.now(timezone.utc).isoformat()
        state.current_run = None
        self.save(state)

    def record_issue_start(self, state: WorkflowState, issue_id: str, issue_type: str, title: str) -> None:
        """Record start of issue work."""
        if state.current_run:
            state.current_run.issue_id = issue_id
            state.current_run.issue_type = issue_type
            state.current_run.issue_title = title
            state.current_run.phase = "implementation"
        self.save(state)

    def record_worktree(self, state: WorkflowState, worktree_path: str, branch_name: str) -> None:
        """Record worktree info."""
        if state.current_run:
            state.current_run.worktree_path = worktree_path
            state.current_run.branch_name = branch_name
        self.save(state)

    def record_pr_created(self, state: WorkflowState, pr_number: int) -> None:
        """Record PR creation."""
        if state.current_run:
            state.current_run.pr_number = pr_number
            state.current_run.phase = "review"
        state.metrics.prs_created += 1
        self.save(state)

    def record_ocr_iteration(self, state: WorkflowState, iteration: int, findings: int) -> None:
        """Record OCR review iteration."""
        if state.current_run:
            state.current_run.ocr_iteration = iteration
            state.current_run.findings_count = findings
            state.current_run.phase = "review"
        state.metrics.ocr_iterations_total += 1
        self.save(state)

    def record_merge_result(self, state: WorkflowState, merged: bool, pr_number: int | None = None) -> None:
        """Record merge result."""
        if merged:
            state.metrics.prs_merged += 1
            if state.current_run and state.current_run.issue_id:
                issue = CompletedIssue(
                    id=state.current_run.issue_id,
                    type=state.current_run.issue_type or "unknown",
                    title=state.current_run.issue_title or "Unknown",
                    pr_number=pr_number or state.current_run.pr_number or 0,
                    merged=True,
                    merged_at=datetime.now(timezone.utc).isoformat(),
                    ocr_iterations=state.current_run.ocr_iteration,
                )
                state.completed_issues.append(issue)
                state.metrics.issues_fixed += 1
        else:
            state.metrics.prs_failed += 1
        self.save(state)

    def cleanup_old_state(self) -> None:
        """Remove completed issues older than max_state_age_days."""
        state = self.load()
        cutoff = datetime.now(timezone.utc).timestamp() - (self.config.state.max_state_age_days * 86400)
        state.completed_issues = [
            issue for issue in state.completed_issues
            if datetime.fromisoformat(issue.created_at.replace("Z", "+00:00")).timestamp() > cutoff
        ]
        self.save(state)


def generate_run_id() -> str:
    """Generate unique run ID."""
    return f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"