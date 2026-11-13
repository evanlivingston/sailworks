# Automated Bug/Optimization Workflow - Architecture

## Overview

This system runs on a schedule (GitHub Actions cron), analyzes the `dev` branch for bugs and optimizations, implements fixes via AI agents, creates PRs, runs OpenCodeReview (OCR) in a loop until clean, then auto-merges.

## System Components

```
.github/workflows/auto-workflow.yml          # GitHub Actions scheduler
tools/auto_workflow/
├── main.py                                  # Main orchestration entry point
├── config.py                                # Configuration management
├── state.py                                 # State persistence (JSON)
├── discover/
│   ├── __init__.py
│   ├── ocr_scanner.py                       # OCR-based issue discovery
│   ├── static_analysis.py                   # pylint, mypy, bandit, radon
│   ├── test_analyzer.py                     # Test suite analysis
│   ├── pattern_miner.py                     # TODO/FIXME, code smells
│   └── perf_regression.py                   # Performance regression detection
├── implement/
│   ├── __init__.py
│   ├── agent_runner.py                      # OpenCode subagent orchestration
│   ├── worktree_manager.py                  # Worktree creation/cleanup
│   └── patch_applier.py                     # Apply generated patches
├── review/
│   ├── __init__.py
│   ├── ocr_runner.py                        # OCR execution and parsing
│   └── fix_loop.py                          # Iterative fix loop
├── pr/
│   ├── __init__.py
│   ├── creator.py                           # PR creation via GitHub CLI
│   └── merger.py                            # Auto-merge logic
└── utils/
    ├── __init__.py
    ├── git_ops.py                           # Git operations wrapper
    ├── github_api.py                        # GitHub API wrapper
    └── logging.py                           # Structured logging
```

## Workflow Steps

### 1. Scheduled Trigger (GitHub Actions)
- Runs daily at 02:00 UTC (configurable)
- Checks out `dev` branch
- Sets up Python environment
- Runs `tools/auto_workflow/main.py`

### 2. State Initialization
- Load previous state from `tools/auto_workflow/state.json`
- Check if previous run completed or needs continuation
- Acquire lock to prevent concurrent runs

### 3. Issue Discovery (Parallel)
Run all discovery methods in parallel:
- **OCR Scanner**: Run OCR on `dev` vs `origin/dev` (or last scanned commit)
- **Static Analysis**: Run pylint, mypy, bandit, radon on changed files
- **Test Analyzer**: Run test suite, identify flaky/slow/failing tests
- **Pattern Miner**: Scan for TODO/FIXME, deprecated APIs, code smells
- **Perf Regression**: Compare benchmarks against stored baselines

### 4. Issue Selection & Prioritization
- Aggregate findings from all sources
- Deduplicate and rank by severity/impact
- Select top 1 issue (configurable)
- Create work item with context

### 5. Worktree Setup
- Run `tools/wt-new.sh <slug>` to create isolated worktree
- Branch name: `auto/<slug>`
- Switch session to worktree (via `session_move`)

### 6. AI Implementation
- Spawn OpenCode subagent (`medium` tier) with:
  - Issue description and context
  - Repository AGENTS.md rules
  - Target files to modify
  - Acceptance criteria (tests pass, OCR clean)
- Subagent implements fix, runs validation
- Collect changes as patch/commits

### 7. Validation & Pre-PR Checks
- Run targeted tests for changed modules
- Run `black --check` and `git diff --check`
- Run doc link/anchor checks if docs changed

### 8. PR Creation
- Push branch to origin
- Create PR via `gh pr create` with template
- Link to any related issues

### 9. OCR Review Loop
- Run OCR on PR (compare PR branch vs `dev`)
- Parse findings (structured JSON)
- If findings exist:
  - Categorize: blocker / major / minor / suggestion
  - Spawn fix subagent for blocker/major findings
  - Apply fixes, commit, push
  - Repeat (max 5 iterations)
- If clean: proceed to merge

### 10. Auto-Merge
- Enable auto-merge on PR (`gh pr merge --auto`)
- Wait for merge or timeout
- On merge: clean up worktree, update state
- On failure: record in state for next run

### 11. State Persistence & Cleanup
- Save state: last scanned commit, completed issues, metrics
- Release lock
- Prune old worktrees (`wt-prune.sh`)

## Configuration

```yaml
# tools/auto_workflow/config.yaml
schedule:
  cron: "0 2 * * *"  # Daily at 2 AM UTC
  
discovery:
  ocr_enabled: true
  static_analysis_enabled: true
  test_analysis_enabled: true
  pattern_mining_enabled: true
  perf_regression_enabled: true
  
  # OCR specific
  ocr_model: "auto"
  ocr_concurrency: 4
  ocr_timeout_minutes: 30
  
implementation:
  max_issues_per_run: 1
  subagent_tier: "medium"
  max_implementation_minutes: 30
  
review:
  ocr_max_iterations: 5
  ocr_blocker_threshold: "major"  # blocker, major, minor
  
merge:
  auto_merge: true
  merge_method: "squash"
  require_tests_pass: true
  require_ocr_clean: true
  
worktree:
  base_path: "~/wt"
  prune_on_start: true
  prune_on_end: true
  
state:
  file: "tools/auto_workflow/state.json"
  lock_file: "tools/auto_workflow/.lock"
```

## State Schema

```json
{
  "last_run": "2026-11-13T02:00:00Z",
  "last_scanned_commit": "abc123...",
  "completed_issues": [
    {
      "id": "auto-20261113-001",
      "type": "bug",
      "title": "Fix memory leak in weathercache",
      "pr_number": 42,
      "merged": true,
      "merged_at": "2026-11-13T02:45:00Z"
    }
  ],
  "current_run": null,
  "metrics": {
    "total_runs": 15,
    "issues_fixed": 12,
    "prs_created": 12,
    "prs_merged": 11,
    "ocr_iterations_avg": 2.3
  }
}
```

## LLM Integration Options

The implementation agent can use:
1. **OpenCode subagent** (current session's model) - via `subagent` tool
2. **Direct API calls** - Anthropic/OpenAI SDKs in Python
3. **OCR's built-in LLM** - if configured

Recommendation: Use OpenCode subagents for implementation (leverages existing session context), direct OCR for review.

## Safety & Guardrails

- **Lock file** prevents concurrent runs
- **Max 1 issue per run** by default
- **Worktree isolation** - no impact on main repo
- **OCR gate** - PR cannot merge until OCR clean
- **Test gate** - Targeted tests must pass
- **Human override** - PRs require manual approval if auto-merge fails
- **Rollback** - Failed merges leave PR open for manual handling

## Monitoring & Observability

- Structured JSON logs to `tools/auto_workflow/logs/`
- GitHub Actions workflow run history
- PR timeline shows full automation trace
- State file tracks long-term metrics
- Optional: Send summary to Slack/webhook on completion