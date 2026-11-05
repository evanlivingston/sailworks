#!/usr/bin/env bash
# Create a fresh, isolated worktree forked from the latest origin/dev.
# Usage: tools/wt-new.sh <slug>
set -euo pipefail

SLUG="${1:?usage: wt-new.sh <slug>}"
ROOT="${HOME}/wt"

MAIN="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --path-format=absolute --git-common-dir)"
MAIN="$(dirname "$MAIN")"
cd "$MAIN"

git fetch --quiet --prune origin dev

BRANCH="task/${SLUG}"
WT="${ROOT}/${SLUG}"

git show-ref --verify --quiet "refs/heads/${BRANCH}" && { echo "error: branch ${BRANCH} exists" >&2; exit 1; }
[ -e "$WT" ] && { echo "error: path ${WT} exists" >&2; exit 1; }

mkdir -p "$ROOT"
git worktree add --quiet -b "$BRANCH" "$WT" origin/dev
echo "$WT"
