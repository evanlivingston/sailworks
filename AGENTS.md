# Fork development and upstream contributions

Our fork is a working product that can advance faster than the parent project's
review process. Keep local development moving while making changes easy to
contribute upstream in small, focused PRs. Upstream approval is not a prerequisite
for integrating tested work into our development version.

## Repository and branch roles

- `origin` is the parent: `https://github.com/dakk/gweatherrouting.git`.
- `fork` is our fork: `https://github.com/evanlivingston/gweatherrouting.git`.
- `master` tracks the parent's `master`; keep fork-specific work off this branch.
- `dev` is our integration branch and the version we use. It may include features
  whose upstream PRs are pending, have not been submitted, or are fork-only.
- `feature/<topic>` branches start from `dev` for new development and merge back
  into `dev` after appropriate validation.
- `upstream/<topic>` branches start from the parent's `master` and contain only
  the changes needed for one focused upstream contribution.

Verify actual remotes and branches before using them; do not assume `origin`
means our fork or that the parent's default branch is `main`.

## Default workflow for every task

Develop and validate locally first. Fork review and upstream contribution are
separate delivery tracks; upstream discussions, review latency, or rejection
must not block authorized development or approved integration into `dev`.

1. Start independent work from `dev`, using an isolated worktree when another
   task has uncommitted changes. Base dependent work on its prerequisite feature
   branch when necessary, and record that dependency.
2. Implement the complete local behavior and run checks appropriate to the
   change. Preserve calculation fidelity when optimizing; report measured
   performance and any changed approximation or source-selection behavior.
3. Keep commits usable on their own. Separate reusable code from fork policy,
   local profiling artifacts, and unrelated work. Do not make broken commits
   merely to achieve smaller upstream patches.
4. When PR creation is authorized, open a regular PR in our fork for human
   review. Report validation results honestly, distinguishing new failures from
   failures reproduced on the base branch. Use the fork PR template.
5. After the user approves merging, integrate tested work into `dev` without
   waiting for upstream. Start subsequent independent features from `dev`.
6. Keep possible upstream contributions recorded in `UPSTREAM.md`. Preparing or
   submitting them is a separate task: fetch parent `master`, extract one
   focused contribution, and validate it in its own worktree. Do not freeze
   local development while waiting for that contribution to be accepted.

For each substantial fork PR, update its `UPSTREAM.md` entry with the review
link, functional commits, dependencies, validation, and intended upstream scope.
Classify upstream work as a candidate, queued on a dependency, submitted, landed,
or fork-only. This is lightweight contribution tracking, not a requirement to
start upstream discussions for every local change. Apply upstream contribution
requirements when preparing the upstream submission, including AI disclosure.

Bug fixes found during fork or upstream review must return to `dev` and relevant
active feature branches after validation. Use focused follow-up commits; avoid
rewriting shared history or requiring new local features to wait for upstream
review. Reconcile code after upstream squash merges before dropping or replaying
local changes. Keep fork-only changes documented when upstream declines them.

## Review in our fork first

- Open regular PRs in `evanlivingston/gweatherrouting` for the user's review
  before preparing new upstream submissions. Use `dev` as the base for
  independent features; use a feature branch as the base for a dependent PR
  when this keeps its review diff focused, and document the merge order.
- Leave PRs unmerged until the user approves merging. Fork review or merging
  does not authorize creating or updating an upstream PR.
- After fork review, prepare focused `upstream/<topic>` branches from freshly
  fetched parent `master` and validate them independently. Split the reviewed
  fork changes into smaller upstream contributions as needed; keep policy,
  local benchmark artifacts, the fork PR template, and unrelated work out
  of those branches.
- Keep review fixes in the fork feature branch and any dependent branch. If a
  dependent PR targets that branch, retarget it to `dev` after its prerequisite
  merges and verify its resulting diff and checks before merging it.

## Transition from the existing development branch

`dev` was created from `charts/fluid-map-and-downloads`, preserving its complete
development history. Use `dev` for the working version. The old chart branch is
retained for history; do not reset, rewrite, or repurpose branches that back PRs.
If the working tree is dirty, prefer an isolated worktree for unrelated branch
work. See `UPSTREAM.md` for the contribution branches and their upstream PRs.

## Structure work for both destinations

1. Inspect the working tree and preserve existing user changes before editing or
   switching branches. Continue the requested task without unrelated cleanup.
2. Keep commits coherent and independently understandable. Separate unrelated
   features, bug fixes, refactors, formatting, and fork-specific configuration.
   Avoid mixing multiple upstream candidates in one commit. Do not split a
   change so finely that its commits are broken or untestable.
3. Integrate validated features into `dev` without waiting for upstream review.
   Keep dependencies explicit so a later contribution can be extracted safely.
4. Prepare each upstream PR from a freshly fetched parent `master`, using
   selected commits or a focused adaptation of the development changes. Review
   the entire diff against the parent and validate that branch independently;
   success on `dev` alone is insufficient.
5. Exclude unrelated pending work and fork-only policy or configuration from
   upstream PRs. This `AGENTS.md` describes our fork's workflow and should stay
   out of parent-project PRs unless explicitly requested.
6. Apply review fixes on the relevant upstream PR branch, then bring those fixes
   into `dev` and any active dependent work. Keep PR branches focused throughout
   review; do not keep adding new product features to them.

## Dependencies and synchronization

- If a contribution depends on another pending PR, continue developing locally.
  Queue the dependent upstream contribution until its prerequisite lands, unless
  maintainers explicitly welcome stacked PRs. Document the dependency.
- Periodically fetch the parent and merge its updates into `dev`, resolving
  conflicts and running appropriate checks. Avoid rebasing or force-pushing the
  shared integration branch as routine maintenance.
- Account for upstream squash merges or modified implementations when syncing.
  Compare the resulting code and reconcile differences rather than assuming
  matching commits or blindly replaying already accepted changes.
- In PR descriptions, state the concrete problem, resulting behavior, relevant
  dependencies, and validation. Follow `CONTRIBUTING.md` for parent-project
  contribution and check requirements, using the verified default branch where
  that document's `main`/`master` examples disagree.
- Follow the user's authorization for commits, pushes, PR creation, and merges.
  These workflow conventions do not independently authorize publication or
  merging upstream PRs.
