"""Configuration management for auto-workflow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import yaml
from dataclasses import dataclass, field


@dataclass
class ScheduleConfig:
    cron: str = "0 2 * * *"
    timezone: str = "UTC"
    enabled: bool = True


@dataclass
class DiscoveryConfig:
    ocr_enabled: bool = True
    static_analysis_enabled: bool = True
    test_analysis_enabled: bool = True
    pattern_mining_enabled: bool = True
    perf_regression_enabled: bool = True

    ocr_model: str = "auto"
    ocr_concurrency: int = 4
    ocr_timeout_minutes: int = 30
    ocr_compare_base: str = "origin/dev"
    ocr_exclude_patterns: list[str] = field(default_factory=list)

    pylint_enabled: bool = True
    mypy_enabled: bool = True
    bandit_enabled: bool = True
    radon_enabled: bool = True
    complexity_threshold: int = 15

    test_timeout_seconds: int = 120
    flaky_test_runs: int = 3
    slow_test_threshold_seconds: int = 10

    todo_patterns: list[str] = field(default_factory=list)
    deprecated_patterns: list[str] = field(default_factory=list)

    benchmark_baseline_dir: str = "docs/profiles/rendering"
    regression_threshold_percent: int = 10


@dataclass
class ImplementationConfig:
    max_issues_per_run: int = 1
    subagent_tier: str = "medium"
    max_implementation_minutes: int = 30
    worktree_slug_prefix: str = "auto"
    commit_message_template: str = ""


@dataclass
class ReviewConfig:
    ocr_max_iterations: int = 5
    ocr_blocker_threshold: str = "major"
    ocr_auto_fix_types: list[str] = field(default_factory=list)
    ocr_skip_types: list[str] = field(default_factory=list)
    fix_subagent_tier: str = "medium"
    max_fix_minutes_per_iteration: int = 15


@dataclass
class MergeConfig:
    auto_merge: bool = True
    merge_method: str = "squash"
    require_tests_pass: bool = True
    require_ocr_clean: bool = True
    require_ci_pass: bool = False
    merge_timeout_minutes: int = 10


@dataclass
class WorktreeConfig:
    base_path: str = "~/wt"
    prune_on_start: bool = True
    prune_on_end: bool = True
    keep_merged_worktrees_hours: int = 24


@dataclass
class StateConfig:
    file: str = "tools/auto_workflow/state.json"
    lock_file: str = "tools/auto_workflow/.lock"
    lock_timeout_minutes: int = 5
    max_state_age_days: int = 30


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "tools/auto_workflow/logs"
    retention_days: int = 7
    json_format: bool = True


@dataclass
class GitHubConfig:
    repo: str = "evanlivingston/sailworks"
    base_branch: str = "dev"
    pr_labels: list[str] = field(default_factory=list)
    pr_assignees: list[str] = field(default_factory=list)
    pr_reviewers: list[str] = field(default_factory=list)


@dataclass
class Config:
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    implementation: ImplementationConfig = field(default_factory=ImplementationConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    state: StateConfig = field(default_factory=StateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)

    @classmethod
    def load(cls, config_path: str | None = None) -> Config:
        """Load configuration from YAML file with optional local override."""
        # Default config path
        if config_path is None:
            config_path = Path(__file__).parent / "config.yaml"

        config_data = {}
        # Load base config
        if Path(config_path).exists():
            with open(config_path) as f:
                config_data = yaml.safe_load(f) or {}

        # Load local override
        local_path = Path(config_path).with_name("config.local.yaml")
        if local_path.exists():
            with open(local_path) as f:
                local_data = yaml.safe_load(f) or {}
                config_data = cls._deep_merge(config_data, local_data)

        # Environment variable overrides
        config_data = cls._apply_env_overrides(config_data)

        return cls._from_dict(config_data)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Deep merge two dictionaries."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _apply_env_overrides(config: dict) -> dict:
        """Apply environment variable overrides."""
        env_mappings = {
            "AUTO_WORKFLOW_MAX_ISSUES": ("implementation", "max_issues_per_run", int),
            "AUTO_WORKFLOW_SUBAGENT_TIER": ("implementation", "subagent_tier", str),
            "AUTO_WORKFLOW_OCR_ENABLED": ("discovery", "ocr_enabled", lambda x: x.lower() == "true"),
            "AUTO_WORKFLOW_AUTO_MERGE": ("merge", "auto_merge", lambda x: x.lower() == "true"),
            "AUTO_WORKFLOW_GITHUB_REPO": ("github", "repo", str),
            "AUTO_WORKFLOW_BASE_BRANCH": ("github", "base_branch", str),
            "AUTO_WORKFLOW_LOG_LEVEL": ("logging", "level", str),
        }

        for env_var, (section, key, converter) in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                if section not in config:
                    config[section] = {}
                config[section][key] = converter(value)

        return config

    @classmethod
    def _from_dict(cls, data: dict) -> Config:
        """Create Config from dictionary."""
        return cls(
            schedule=ScheduleConfig(**data.get("schedule", {})),
            discovery=DiscoveryConfig(**data.get("discovery", {})),
            implementation=ImplementationConfig(**data.get("implementation", {})),
            review=ReviewConfig(**data.get("review", {})),
            merge=MergeConfig(**data.get("merge", {})),
            worktree=WorktreeConfig(**data.get("worktree", {})),
            state=StateConfig(**data.get("state", {})),
            logging=LoggingConfig(**data.get("logging", {})),
            github=GitHubConfig(**data.get("github", {})),
        )


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get global configuration instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config(config_path: str | None = None) -> Config:
    """Reload configuration from file."""
    global _config
    _config = Config.load(config_path)
    return _config