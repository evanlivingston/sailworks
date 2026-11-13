# Auto-Workflow: Automated Bug Fixing and Optimization

An automated system that runs on a schedule, analyzes the codebase for bugs and optimizations, implements fixes via AI agents, creates PRs, runs OpenCodeReview in a loop until clean, and auto-merges.

## Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Scheduled Run  │────▶│  Discovery Phase │────▶│  Select Top N   │
│  (GitHub Actions)│     │  (OCR, Static,   │     │  Issues         │
└─────────────────┘     │   Tests, Patterns)│     └────────┬────────┘
                        └──────────────────┘              │
                                                         ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Auto-Merge    │◀────│  OCR Review Loop │◀────│  Implementation │
│   (when clean)  │     │  (fix → review)  │     │  (AI Agent)     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

## Features

- **Scheduled execution** via GitHub Actions (daily at 2 AM UTC)
- **Multi-source discovery**: OCR, static analysis (pylint/mypy/bandit/radon), test analysis, pattern mining, performance regression
- **Isolated worktrees** using existing `wt-new.sh` script
- **AI implementation** via OpenCode subagents
- **Iterative OCR review** until findings are resolved
- **Auto-merge** when all checks pass
- **State persistence** for resumability and metrics
- **Comprehensive logging** in JSON format

## Quick Start

### 1. Configure OpenCodeReview

OCR requires an LLM endpoint. Set up one of:

```bash
# Option 1: Anthropic API (recommended)
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL="claude-3-5-sonnet-20241022"

# Option 2: Custom endpoint
export OCR_LLM_URL="https://api.example.com/v1"
export OCR_LLM_TOKEN="your-token"
export OCR_LLM_MODEL="model-name"

# Option 3: Config file
mkdir -p ~/.opencodereview
cat > ~/.opencodereview/config.json << 'EOF'
{
  "llm": {
    "provider": "anthropic",
    "model": "claude-3-5-sonnet-20241022",
    "api_key": "sk-ant-..."
  }
}
EOF
```

### 2. Configure GitHub Actions Secrets

Add to repository secrets:
- `ANTHROPIC_API_KEY` - For OCR reviews
- `GITHUB_TOKEN` - Automatically provided

### 3. Run Manually

```bash
# Dry run (discovery only)
python tools/auto_workflow/main.py --dry-run

# Single run with custom ID
python tools/auto_workflow/main.py --run-id "manual-test-1" --max-issues 1

# Verbose output
python tools/auto_workflow/main.py -v
```

### 4. Enable Scheduled Runs

The workflow runs automatically via GitHub Actions. To customize schedule, edit `.github/workflows/auto-workflow.yml`:

```yaml
on:
  schedule:
    - cron: '0 2 * * *'  # Daily at 2 AM UTC
```

## Configuration

### Main Config: `tools/auto_workflow/config.yaml`

Key settings:

```yaml
schedule:
  cron: "0 2 * * *"
  enabled: true

discovery:
  ocr_enabled: true
  static_analysis_enabled: true
  test_analysis_enabled: true
  pattern_mining_enabled: true
  perf_regression_enabled: true

implementation:
  max_issues_per_run: 1
  subagent_tier: "medium"

review:
  ocr_max_iterations: 5
  ocr_blocker_threshold: "major"
  ocr_auto_fix_types: ["blocker", "major"]

merge:
  auto_merge: true
  require_tests_pass: true
  require_ocr_clean: true
```

### Local Override: `tools/auto_workflow/config.local.yaml`

Copy `config.local.yaml.example` and modify for local development:

```bash
cp tools/auto_workflow/config.local.yaml.example tools/auto_workflow/config.local.yaml
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `AUTO_WORKFLOW_MAX_ISSUES` | Max issues per run |
| `AUTO_WORKFLOW_SUBAGENT_TIER` | Subagent tier (fast/medium/heavy) |
| `AUTO_WORKFLOW_OCR_ENABLED` | Enable/disable OCR |
| `AUTO_WORKFLOW_AUTO_MERGE` | Enable/disable auto-merge |
| `AUTO_WORKFLOW_LOG_LEVEL` | Log level (DEBUG/INFO/WARNING) |

## Discovery Methods

### 1. OCR Scanner (`ocr_scanner.py`)
Runs OpenCodeReview on recent changes to find bugs, security issues, performance problems.

### 2. Static Analysis (`static_analysis.py`)
- **pylint**: Code quality, errors, conventions
- **mypy**: Type checking errors
- **bandit**: Security vulnerabilities
- **radon**: Cyclomatic complexity

### 3. Test Analyzer (`test_analyzer.py`)
- Failing tests
- Slow tests (>10s by default)
- Flaky tests (re-runs failed tests)

### 4. Pattern Miner (`pattern_miner.py`)
- TODO/FIXME/HACK/XXX/BUG comments
- Deprecated/legacy patterns
- Code smells (long functions, many args, bare except)
- Console.log in JS/TS

### 5. Performance Regression (`perf_regression.py`)
Runs benchmark scripts and compares with stored baselines.

## Implementation Flow

1. **Worktree Creation**: Isolated environment via `wt-new.sh`
2. **Agent Prompting**: Detailed prompt with issue context, repo rules, validation commands
3. **Subagent Execution**: Runs OpenCode subagent (medium tier by default)
4. **Validation**: Black formatting, git diff check, targeted tests
5. **Commit**: Structured commit message with issue reference

## Review Loop

1. **OCR Review**: Run OCR on PR branch vs `dev`
2. **Findings Analysis**: Categorize by severity (blocker/major/minor/suggestion)
3. **Auto-Fix**: For blocker/major findings, spawn fix subagent
4. **Iterate**: Push fixes, re-run OCR (max 5 iterations)
5. **Complete**: When clean or max iterations reached

## Merge Criteria

PR auto-merges when:
- ✅ OCR review clean (no blocker/major findings)
- ✅ Targeted tests pass
- ✅ No merge conflicts
- ✅ Auto-merge enabled

## State Management

State persisted to `tools/auto_workflow/state.json`:
- Last run timestamp
- Last scanned commit
- Completed issues with PR numbers
- Metrics (runs, fixes, merges, OCR iterations)
- Current run progress (for resumability)

## Monitoring

- **GitHub Actions**: Workflow run history
- **PR Timeline**: Full automation trace in PR comments
- **Logs**: Structured JSON logs in `tools/auto_workflow/logs/`
- **State File**: Long-term metrics in `state.json`

## Safety & Guardrails

- **Lock file** prevents concurrent runs
- **Max 1 issue per run** by default
- **Worktree isolation** - no impact on main repo
- **OCR gate** - PR cannot merge until OCR clean
- **Test gate** - Targeted tests must pass
- **Human override** - Failed merges leave PR open
- **Rollback** - Worktrees cleaned up after merge

## Troubleshooting

### OCR Not Configured
```
Warning: OCR not configured - missing LLM endpoint
```
Set up Anthropic API key or OCR config as shown in Quick Start.

### Worktree Already Exists
```
error: branch task/auto-xxx exists
```
Run `tools/wt-prune.sh --apply` to clean old worktrees.

### Lock Timeout
```
Could not acquire lock - another run in progress?
```
Check for stuck GitHub Actions runs. Lock file at `tools/auto_workflow/.lock`.

### Subagent Fails
Check logs for subagent output. Common issues:
- Missing dependencies: `uv sync --group dev`
- Test failures: Run tests manually to debug
- Timeout: Increase `max_implementation_minutes`

## Architecture

See `ARCHITECTURE.md` for detailed system design.

## Extending

### Add New Discovery Method
1. Create `tools/auto_workflow/discover/new_method.py` extending `DiscoveryMethod`
2. Add to `discovery_methods` list in `main.py`

### Customize Implementation Prompt
Modify `_build_prompt()` in `agent_runner.py`.

### Add Validation Step
Extend `run_validation()` in `agent_runner.py`.

## License

Part of sailworks project. See root LICENSE.