"""Structured logging for auto-workflow."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from contextvars import ContextVar

from ..config import get_config


# Context variables for structured logging
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
phase_var: ContextVar[str | None] = ContextVar("phase", default=None)
issue_id_var: ContextVar[str | None] = ContextVar("issue_id", default=None)


class StructuredFormatter(logging.Formatter):
    """JSON formatter with context."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add context variables
        if run_id := run_id_var.get():
            log_data["run_id"] = run_id
        if phase := phase_var.get():
            log_data["phase"] = phase
        if issue_id := issue_id_var.get():
            log_data["issue_id"] = issue_id

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "thread", "threadName",
                "exc_info", "exc_text", "stack_info", "getMessage"
            }:
                log_data[key] = value

        # Add exception info
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


def setup_logging(config=None) -> logging.Logger:
    """Configure logging for auto-workflow."""
    config = config or get_config()
    log_dir = Path(config.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Root logger
    root_logger = logging.getLogger("auto_workflow")
    root_logger.setLevel(getattr(logging, config.logging.level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, config.logging.level.upper()))
    if config.logging.json_format:
        console_handler.setFormatter(StructuredFormatter())
    else:
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
    root_logger.addHandler(console_handler)

    # File handler with rotation
    log_file = log_dir / f"auto_workflow_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=config.logging.retention_days,
        utc=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(file_handler)

    # Error file handler
    error_file = log_dir / f"auto_workflow_errors_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_file,
        when="midnight",
        interval=1,
        backupCount=config.logging.retention_days,
        utc=True,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(error_handler)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get logger for module."""
    return logging.getLogger(f"auto_workflow.{name}")


class LogContext:
    """Context manager for adding context to logs."""

    def __init__(self, run_id: str | None = None, phase: str | None = None, issue_id: str | None = None):
        self.run_id = run_id
        self.phase = phase
        self.issue_id = issue_id
        self.tokens = []

    def __enter__(self):
        if self.run_id:
            self.tokens.append(run_id_var.set(self.run_id))
        if self.phase:
            self.tokens.append(phase_var.set(self.phase))
        if self.issue_id:
            self.tokens.append(issue_id_var.set(self.issue_id))
        return self

    def __exit__(self, *args):
        for token in reversed(self.tokens):
            if token.var is run_id_var:
                run_id_var.reset(token)
            elif token.var is phase_var:
                phase_var.reset(token)
            elif token.var is issue_id_var:
                issue_id_var.reset(token)