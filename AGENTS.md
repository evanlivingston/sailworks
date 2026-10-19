# Fork development

Our fork is the working product. The parent upstream (`origin`) is retained
for history only; we do not prepare upstream contributions, track upstream
PRs, or maintain upstream branches.

## Repository and branch roles

- `origin` is our fork: `https://github.com/evanlivingston/gweatherrouting.git`.
- `dev` is our integration branch and the version we use.
- `feature/<topic>` branches start from `dev` for new development and merge
  back into `dev` after appropriate validation.
- The parent repo has no configured remote; its history is reachable through
  our fork.

Verify actual remotes and branches before using them.

## Default workflow for every task

1. Start independent work from `dev`, using an isolated worktree when another
   task has uncommitted changes.
2. Implement the complete local behavior and run checks appropriate to the
   change. Preserve calculation fidelity when optimizing; report measured
   performance and any changed approximation or source-selection behavior.
3. Keep commits coherent and independently understandable. Separate unrelated
   features, bug fixes, and refactors.
4. When PR creation is authorized, open a regular PR in our fork for human
   review. Report validation results honestly. Use the fork PR template.
5. After the user approves merging, integrate tested work into `dev`. Start
   subsequent independent features from `dev`.

Bug fixes found during fork review return to `dev` and relevant active
feature branches as focused follow-up commits; avoid rewriting shared history.

## Rendering performance work

- The target architecture is decided in `docs/adr-001-grib-rendering-architecture.md`.
  Read it first, then the issue it points at. The implementation of that ADR is the
  work program in issues #62-#70; work one issue at a time.
- `docs/README.md` is the documentation index. Read it before browsing `docs/`.
  It separates the current working set from `docs/history/`, which holds
  completed-work records and experiment evidence: read those for why a decision
  was made, never as a plan.
- `docs/rendering-performance-plan.md` is the canonical roadmap and
  `docs/rendering-implementation-plan.md` holds the detailed R0-R5 sequence;
  older rendering plans/reports remain historical specifications and evidence.
- Gate changes with one command from the runner built in #63. Do not re-run the
  full test suite, and do not re-measure a baseline the change does not touch.
  Recorded evidence under `docs/profiles/rendering/` is the reference.
- Before an experiment, record scope/non-goals, baseline/workload, measurement
  and fidelity gates, time/attempt budget, and stop condition. Stop and report
  when the budget is exhausted or scope must expand; do not silently extend it.
- Update evidence and adopt/revise/reject decisions before claiming completion.
  Correctness tests, synthetic mechanism results, and real-workload performance
  acceptance are separate. Never call grab/readback timing presentation FPS.
- At each experiment/milestone gate, reevaluate the bottleneck and record
  continue/narrow/pivot/stop. Do not start provisional later milestones by default.

## Review in our fork

- Open PRs in `evanlivingston/gweatherrouting` for the user's review before
  merging into `dev`. Use `dev` as the base.
- Leave PRs unmerged until the user approves merging.
- Follow the user's authorization for commits, pushes, PR creation, and
  merges.

## Token-efficient validation and merging

- Delegate test runs and validation commands to the `test-runner` subagent
  defined in `.opencode/agents/test-runner.md`. Keep test selection, diagnosis,
  and fixes in the primary agent; give the runner only the absolute working
  directory, exact commands, required environment, and any timeout or log path.
- Use the runner's configured lightweight model without overriding it or
  passing the main conversation. Ask for concise counts and relevant failure
  excerpts. If the runner or its model is unavailable, report the blocker
  rather than silently switching to a heavier agent or running tests directly.
- Validate with targeted tests covering changed modules, plus `black --check`
  and `git diff --check` on changed files only.
- Run the full suite only on explicit request or when shared sampling/routing
  code changes.
- Do not re-run validation for a merge if the branch is unchanged since its
  last recorded results; merge directly.
- Do not replicate CI checks locally when they already fail on the base branch;
  rely on `gh pr checks` status instead.
- Confirm scope changes before expanding an open PR; summarize validation as
  counts without pasting full output.
