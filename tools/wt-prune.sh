#!/usr/bin/env bash
# Remove worktrees that are clean AND fully contained in origin/dev.
# Never deletes branches, never touches dirty or detached worktrees.
# Usage: tools/wt-prune.sh [--apply]
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

MAIN="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --path-format=absolute --git-common-dir)"
MAIN="$(dirname "$MAIN")"
cd "$MAIN"
git fetch --quiet --prune origin dev
BASE="$(git rev-parse origin/dev)"
SELF="$(git rev-parse --path-format=absolute --show-toplevel)"

git worktree list --porcelain | awk '
  /^worktree /{p=substr($0,10)}
  /^branch /{b=substr($0,15)}
  /^detached/{b=""}
  /^$/{if(p)print p"\t"b;p="";b=""}
  END{if(p)print p"\t"b}
' | while IFS=$'\t' read -r path branch; do
  [ -n "$path" ] || continue
  [ "$path" = "$SELF" ] && continue
  [ -d "$path" ] || continue
  if [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]; then
    echo "keep  dirty       $path"; continue
  fi
  if [ -z "$branch" ]; then
    echo "keep  detached    $path"; continue
  fi
  ahead=$(git rev-list --count "${BASE}..${branch}" 2>/dev/null || echo 1)
  if [ "$ahead" != "0" ]; then
    echo "keep  unmerged($ahead) $path"; continue
  fi
  if [ "$APPLY" = "1" ]; then
    git worktree remove "$path" && echo "REMOVED           $path"
  else
    echo "would remove      $path  [$branch]"
  fi
done
