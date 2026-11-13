"""OpenCodeReview runner for PR review."""

from __future__ import annotations

import json
import subprocess
import os
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from ..config import get_config
from ..utils.logging import get_logger
from ..utils.git_ops import GitOps


logger = get_logger("review.ocr")


@dataclass
class OCRFinding:
    """A single OCR finding from review."""
    file: str
    line: int
    severity: str  # blocker, major, minor, suggestion
    category: str
    message: str
    suggestion: str | None = None
    rule_id: str | None = None


@dataclass
class OCRResult:
    """Result of OCR review."""
    success: bool
    findings: list[OCRFinding]
    summary: dict
    raw_output: str
    error: str | None = None


class OCRRunner:
    """Runs OpenCodeReview on PR changes."""

    def __init__(self, config=None):
        self.config = config or get_config()

    def is_configured(self) -> bool:
        """Check if OCR is configured."""
        ocr_config = Path.home() / ".opencodereview" / "config.json"
        env_vars = ["OCR_LLM_URL", "OCR_LLM_TOKEN", "OCR_LLM_MODEL",
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"]
        return ocr_config.exists() or any(os.environ.get(v) for v in env_vars)

    def review_pr(self, pr_branch: str, base_branch: str = "dev", repo_path: Path | None = None) -> OCRResult:
        """Run OCR review on PR branch vs base branch."""
        if not self.is_configured():
            return OCRResult(
                success=False,
                findings=[],
                summary={},
                raw_output="",
                error="OCR not configured - missing LLM endpoint",
            )

        repo = repo_path or Path.cwd()
        discovery_config = self.config.discovery

        cmd = ["ocr", "review"]
        cmd.extend(["--from", base_branch])
        cmd.extend(["--to", pr_branch])

        for pattern in discovery_config.ocr_exclude_patterns:
            cmd.extend(["--exclude", pattern])

        cmd.extend(["--concurrency", str(discovery_config.ocr_concurrency)])
        cmd.extend(["--timeout-minutes", str(discovery_config.ocr_timeout_minutes)])
        cmd.append("--json")

        logger.info(f"Running OCR review: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=discovery_config.ocr_timeout_minutes * 60 + 60,
            )

            if result.returncode != 0:
                logger.error(f"OCR review failed: {result.stderr}")
                return OCRResult(
                    success=False,
                    findings=[],
                    summary={},
                    raw_output=result.stderr,
                    error=result.stderr,
                )

            ocr_data = json.loads(result.stdout)
            findings = self._parse_findings(ocr_data)
            summary = self._generate_summary(findings)

            return OCRResult(
                success=True,
                findings=findings,
                summary=summary,
                raw_output=result.stdout,
            )

        except subprocess.TimeoutExpired:
            return OCRResult(
                success=False,
                findings=[],
                summary={},
                raw_output="",
                error="OCR review timed out",
            )
        except json.JSONDecodeError as e:
            return OCRResult(
                success=False,
                findings=[],
                summary={},
                raw_output=result.stdout if 'result' in locals() else "",
                error=f"Failed to parse OCR output: {e}",
            )

    def _parse_findings(self, ocr_data: dict) -> list[OCRFinding]:
        """Parse OCR JSON output into findings."""
        findings = []

        # Handle different OCR output formats
        # Format 1: files with findings array
        if "files" in ocr_data:
            for file_path, file_data in ocr_data["files"].items():
                for finding in file_data.get("findings", []):
                    findings.append(self._parse_finding(file_path, finding))

        # Format 2: flat findings array
        elif "findings" in ocr_data:
            for finding in ocr_data["findings"]:
                file_path = finding.get("file", "")
                findings.append(self._parse_finding(file_path, finding))

        return findings

    def _parse_finding(self, file_path: str, finding: dict) -> OCRFinding:
        """Parse individual finding."""
        # Normalize severity
        severity = finding.get("severity", "minor").lower()
        severity_map = {
            "critical": "blocker",
            "high": "major",
            "major": "major",
            "medium": "minor",
            "minor": "minor",
            "low": "suggestion",
            "info": "suggestion",
            "suggestion": "suggestion",
        }
        normalized_severity = severity_map.get(severity, "minor")

        return OCRFinding(
            file=file_path,
            line=finding.get("line", 0),
            severity=normalized_severity,
            category=finding.get("category", "general"),
            message=finding.get("message", ""),
            suggestion=finding.get("suggestion"),
            rule_id=finding.get("rule_id"),
        )

    def _generate_summary(self, findings: list[OCRFinding]) -> dict:
        """Generate summary statistics."""
        summary = {
            "total": len(findings),
            "by_severity": {},
            "by_category": {},
        }

        for finding in findings:
            summary["by_severity"][finding.severity] = summary["by_severity"].get(finding.severity, 0) + 1
            summary["by_category"][finding.category] = summary["by_category"].get(finding.category, 0) + 1

        return summary

    def has_blockers(self, findings: list[OCRFinding], threshold: str = "major") -> bool:
        """Check if findings contain issues at or above threshold."""
        severity_order = {"suggestion": 0, "minor": 1, "major": 2, "blocker": 3}
        threshold_level = severity_order.get(threshold, 1)

        for finding in findings:
            if severity_order.get(finding.severity, 0) >= threshold_level:
                return True
        return False

    def filter_findings(self, findings: list[OCRFinding], auto_fix_types: list[str]) -> list[OCRFinding]:
        """Filter findings to only those that should be auto-fixed."""
        return [f for f in findings if f.severity in auto_fix_types]