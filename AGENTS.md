# Fork development

Our fork is the working product. The parent upstream (`origin`) is retained
for history only; we do not prepare upstream contributions, track upstream
PRs, or maintain upstream branches.

## Repository and branch roles

- `origin` is the parent: `https://github.com/dakk/gweatherrouting.git`.
  History only.
- `fork` is our fork: `https://github.com/evanlivingston/gweatherrouting.git`.
- `dev` is our integration branch and the version we use.
- `feature/<topic>` branches start from `dev` for new development and merge
  back into `dev` after appropriate validation.

Verify actual remotes and branches before using them; do not assume `origin`
means our fork.

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

## Review in our fork

- Open PRs in `evanlivingston/gweatherrouting` for the user's review before
  merging into `dev`. Use `dev` as the base.
- Leave PRs unmerged until the user approves merging.
- Follow the user's authorization for commits, pushes, PR creation, and
  merges.

## Token-efficient validation and merging

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
