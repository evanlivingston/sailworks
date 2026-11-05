---
description: Create a fresh worktree from the latest origin/dev and move this session into it
---

Set up an isolated worktree for `$1` and move this session into it.

1. Run `tools/wt-new.sh $1` from the repository root.
2. Capture the absolute path it prints.
3. Call the `session_move` tool with that path as `directory`.
4. Report the new working directory and branch name.

Never commit in the main coordination tree. If `$1` is empty, ask for a slug before doing anything.
