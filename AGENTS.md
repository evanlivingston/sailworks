# Fork development

Our fork is the working product. The upstream parent is kept for history only;
we do not prepare upstream contributions or track upstream PRs.

## Branches

- `origin` is our fork: `https://github.com/evanlivingston/gweatherrouting.git`.
- `dev` is the integration branch. Feature branches start from `dev` and merge
  back into `dev` after validation.
- Verify actual remotes and branches before using them.

## Workflow

1. Start from `dev`. Use an isolated worktree when another task has uncommitted
   changes.
2. Implement the complete local behavior. Preserve calculation fidelity when
   optimizing; report measured performance and any changed approximation or
   source-selection behavior.
3. Keep commits coherent and independently understandable. Separate unrelated
   features, bug fixes and refactors.
4. Open a PR in our fork for human review when the user authorizes it, using
   the fork PR template. Leave it unmerged until the user approves the merge.
   Follow the user's authorization for commits, pushes and merges. Confirm
   scope changes before expanding an open PR.
5. A bug fix found in review returns to `dev` and the affected feature branch
   as a focused follow-up commit. Do not rewrite shared history.

## Context economy

- Keep work proportional to the request. Read and validate only what the
  change requires. Do not add tooling, process or adjacent cleanup without
  approval. Keep progress updates and final reports brief.
- The issue body is the spec. It states the gap, scope, non-goals, acceptance
  gate and validation command. Read a repository document when the issue body
  points at one, and read the ADR section your task touches; do not survey the
  rest.
- Do not create a documentation file for a task. Record the result in the
  issue body or a PR comment. Update a canonical document only when a
  decision changed it.
- Read `docs/README.md` only to locate a document, not to survey it.
- Keep the files you touch small enough to read in one pass. Split a document
  rather than growing it past roughly 400 lines. Frozen archives under
  `docs/history/` are exempt: they are history, read only on demand.
- Read the `AGENTS.md` files that cover the directories you touch; they load
  when you read those directories.

## Validation

- Delegate test runs and validation commands to the `test-runner` subagent.
  Give it the absolute working directory, the exact commands, the required
  environment and any timeout or log path. Keep test selection, diagnosis and
  fixes here.
- Use the runner's configured model; do not override it. If it or its model is
  unavailable, report the blocker rather than running tests directly.
- Validate with targeted tests covering the changed modules, plus
  `black --check` and `git diff --check` on changed files only.
- When you change documentation, run `python tools/check_doc_links.py` and
  `python tools/check_doc_anchors.py`.
- Run the full suite only on explicit request or when shared sampling/routing
  code changes.
- Do not re-run validation for a merge if the branch is unchanged since its
  last recorded results.
- Summarize validation as counts. Do not paste full output.

## Scope-specific rules

- Renderer, viewport, GPU or rendering-gate work anywhere in the repo: read
  `gweatherrouting/rendering/AGENTS.md` and follow it. The Qt viewport lives in
  `gweatherrouting/qt/`, so read that file even when the change is not under
  `gweatherrouting/rendering/`.
- Qt frontend work: read `gweatherrouting/qt/AGENTS.md` and follow it.
