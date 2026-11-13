"""Base classes for issue discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from enum import Enum


class IssueType(Enum):
    BUG = "bug"
    OPTIMIZATION = "optimization"
    REFACTOR = "refactor"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TEST_FAILURE = "test_failure"
    CODE_SMELL = "code_smell"
    DEPRECATION = "deprecation"
    DOCUMENTATION = "documentation"


class Severity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class DiscoveryFinding:
    """A single finding from a discovery method."""
    id: str                    # Unique identifier for this finding
    type: IssueType
    severity: Severity
    title: str
    description: str
    file_path: str | None = None
    line_number: int | None = None
    source: str = ""           # Which discovery method found this
    evidence: dict[str, Any] = None  # Additional evidence/data
    suggested_fix: str | None = None
    estimated_effort_minutes: int = 30

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "source": self.source,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
            "estimated_effort_minutes": self.estimated_effort_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiscoveryFinding:
        return cls(
            id=data["id"],
            type=IssueType(data["type"]),
            severity=Severity(data["severity"]),
            title=data["title"],
            description=data["description"],
            file_path=data.get("file_path"),
            line_number=data.get("line_number"),
            source=data.get("source", ""),
            evidence=data.get("evidence", {}),
            suggested_fix=data.get("suggested_fix"),
            estimated_effort_minutes=data.get("estimated_effort_minutes", 30),
        )


class DiscoveryMethod(ABC):
    """Base class for all discovery methods."""

    def __init__(self, config=None, logger=None):
        self.config = config
        self.logger = logger

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this discovery method."""
        pass

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this method is enabled in config."""
        pass

    @abstractmethod
    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run discovery and return findings."""
        pass

    def is_applicable(self, context: DiscoveryContext) -> bool:
        """Check if this method should run for given context."""
        return self.enabled


@dataclass
class DiscoveryContext:
    """Context passed to discovery methods."""
    repo_path: str
    base_commit: str
    head_commit: str
    changed_files: list[str]
    config: Any
    state: Any
    run_id: str


# Import concrete implementations for easy access
from .ocr_scanner import OCRScanner
from .static_analysis import StaticAnalysisScanner
from .test_analyzer import TestAnalyzer
from .pattern_miner import PatternMiner
from .perf_regression import PerfRegressionDetector

__all__ = [
    "IssueType",
    "Severity",
    "DiscoveryFinding",
    "DiscoveryMethod",
    "DiscoveryContext",
    "OCRScanner",
    "StaticAnalysisScanner",
    "TestAnalyzer",
    "PatternMiner",
    "PerfRegressionDetector",
]