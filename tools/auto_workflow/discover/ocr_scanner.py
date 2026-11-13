"""OCR-based issue discovery using OpenCodeReview."""

from __future__ import annotations

import json
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from . import DiscoveryMethod, DiscoveryFinding, IssueType, Severity, DiscoveryContext
from ..config import get_config
from ..utils.logging import get_logger


logger = get_logger("discover.ocr")


@dataclass
class OCRFinding:
    """Parsed OCR finding."""
    file: str
    line: int
    severity: str
    category: str
    message: str
    suggestion: str | None = None
    rule_id: str | None = None


class OCRScanner(DiscoveryMethod):
    """Discovers issues by running OpenCodeReview on code changes."""

    @property
    def name(self) -> str:
        return "ocr_scanner"

    @property
    def enabled(self) -> bool:
        return get_config().discovery.ocr_enabled

    def discover(self, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Run OCR and convert findings to DiscoveryFinding objects."""
        if not self._is_ocr_configured():
            logger.warning("OCR not configured, skipping OCR discovery")
            return []

        findings = []
        try:
            # Run OCR on the changes
            ocr_results = self._run_ocr(context)
            findings = self._parse_ocr_results(ocr_results, context)
        except Exception as e:
            logger.error(f"OCR discovery failed: {e}")

        logger.info(f"OCR scanner found {len(findings)} issues")
        return findings

    def _is_ocr_configured(self) -> bool:
        """Check if OCR is configured with LLM endpoint."""
        # Check for OCR config file or environment variables
        ocr_config = Path.home() / ".opencodereview" / "config.json"
        env_vars = ["OCR_LLM_URL", "OCR_LLM_TOKEN", "OCR_LLM_MODEL",
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"]
        return ocr_config.exists() or any(os.environ.get(v) for v in env_vars)

    def _run_ocr(self, context: DiscoveryContext) -> dict:
        """Run OCR and return parsed JSON results."""
        config = get_config()
        discovery_config = config.discovery

        # Build OCR command
        cmd = ["ocr", "review"]

        # Compare against base branch
        cmd.extend(["--from", discovery_config.ocr_compare_base])
        cmd.extend(["--to", context.head_commit])

        # Exclude patterns
        for pattern in discovery_config.ocr_exclude_patterns:
            cmd.extend(["--exclude", pattern])

        # Concurrency and timeout
        cmd.extend(["--concurrency", str(discovery_config.ocr_concurrency)])
        cmd.extend(["--timeout-minutes", str(discovery_config.ocr_timeout_minutes)])

        # Output as JSON
        cmd.append("--json")

        logger.info(f"Running OCR: {' '.join(cmd)}")

        # Run OCR from repo root
        result = subprocess.run(
            cmd,
            cwd=context.repo_path,
            capture_output=True,
            text=True,
            timeout=discovery_config.ocr_timeout_minutes * 60 + 60,
        )

        if result.returncode != 0:
            logger.error(f"OCR failed: {result.stderr}")
            # Check if it's an auth/config error
            if "LLM endpoint" in result.stderr or "not configured" in result.stderr:
                logger.warning("OCR not configured properly, skipping")
            return {}

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OCR output: {e}")
            return {}

    def _parse_ocr_results(self, ocr_data: dict, context: DiscoveryContext) -> list[DiscoveryFinding]:
        """Parse OCR JSON output into DiscoveryFinding objects."""
        findings = []

        # OCR returns structured findings - adapt based on actual format
        # This is based on typical OCR output structure
        file_findings = ocr_data.get("files", {})
        for file_path, file_data in file_findings.items():
            for finding in file_data.get("findings", []):
                ocr_finding = self._parse_finding(file_path, finding)
                if ocr_finding:
                    discovery_finding = self._convert_to_discovery(ocr_finding, context)
                    findings.append(discovery_finding)

        return findings

    def _parse_finding(self, file_path: str, finding: dict) -> OCRFinding | None:
        """Parse individual OCR finding."""
        try:
            return OCRFinding(
                file=file_path,
                line=finding.get("line", 0),
                severity=finding.get("severity", "medium").lower(),
                category=finding.get("category", "general"),
                message=finding.get("message", ""),
                suggestion=finding.get("suggestion"),
                rule_id=finding.get("rule_id"),
            )
        except Exception as e:
            logger.warning(f"Failed to parse finding: {e}")
            return None

    def _convert_to_discovery(self, ocr_finding: OCRFinding, context: DiscoveryContext) -> DiscoveryFinding:
        """Convert OCR finding to DiscoveryFinding."""
        # Map OCR severity to our severity
        severity_map = {
            "critical": Severity.CRITICAL,
            "high": Severity.HIGH,
            "major": Severity.HIGH,
            "medium": Severity.MEDIUM,
            "minor": Severity.LOW,
            "low": Severity.LOW,
            "info": Severity.INFO,
            "suggestion": Severity.INFO,
        }

        # Map OCR category to issue type
        category_map = {
            "bug": IssueType.BUG,
            "security": IssueType.SECURITY,
            "performance": IssueType.PERFORMANCE,
            "style": IssueType.CODE_SMELL,
            "best_practice": IssueType.REFACTOR,
            "optimization": IssueType.OPTIMIZATION,
            "deprecation": IssueType.DEPRECATION,
            "documentation": IssueType.DOCUMENTATION,
            "test": IssueType.TEST_FAILURE,
        }

        issue_type = category_map.get(ocr_finding.category.lower(), IssueType.CODE_SMELL)
        severity = severity_map.get(ocr_finding.severity.lower(), Severity.MEDIUM)

        # Generate unique ID
        finding_id = f"ocr-{ocr_finding.file.replace('/', '-')}-{ocr_finding.line}-{ocr_finding.rule_id or 'unknown'}"

        return DiscoveryFinding(
            id=finding_id,
            type=issue_type,
            severity=severity,
            title=f"OCR: {ocr_finding.message[:80]}",
            description=f"{ocr_finding.message}\n\nFile: {ocr_finding.file}:{ocr_finding.line}\nCategory: {ocr_finding.category}\nRule: {ocr_finding.rule_id or 'N/A'}",
            file_path=ocr_finding.file,
            line_number=ocr_finding.line,
            source="ocr_scanner",
            evidence={
                "ocr_category": ocr_finding.category,
                "ocr_severity": ocr_finding.severity,
                "ocr_rule_id": ocr_finding.rule_id,
                "ocr_suggestion": ocr_finding.suggestion,
            },
            suggested_fix=ocr_finding.suggestion,
            estimated_effort_minutes=self._estimate_effort(issue_type, severity),
        )

    def _estimate_effort(self, issue_type: IssueType, severity: Severity) -> int:
        """Estimate effort in minutes based on type and severity."""
        base_effort = {
            IssueType.BUG: 30,
            IssueType.OPTIMIZATION: 45,
            IssueType.REFACTOR: 60,
            IssueType.SECURITY: 30,
            IssueType.PERFORMANCE: 45,
            IssueType.TEST_FAILURE: 20,
            IssueType.CODE_SMELL: 15,
            IssueType.DEPRECATION: 30,
            IssueType.DOCUMENTATION: 10,
        }

        severity_multiplier = {
            Severity.CRITICAL: 1.5,
            Severity.HIGH: 1.2,
            Severity.MEDIUM: 1.0,
            Severity.LOW: 0.8,
            Severity.INFO: 0.5,
        }

        return int(base_effort.get(issue_type, 30) * severity_multiplier.get(severity, 1.0))