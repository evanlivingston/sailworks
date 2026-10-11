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
