# Clean up merged branches and worktrees

Use `scripts/cleanup_merged_branches.py` to audit merged local branches,
associated Git worktrees, and matching remote branches without relying on
shell interpolation or recursive filesystem deletion. Dry-run is the default.

## Safety contract

The tool requires an explicit, exact repository root. A branch is eligible
only when its local tip and, when present, its freshly observed `origin` tip
are ancestors of `origin/main`. It refuses:

- protected names (`main`, `master`, `develop`, `development`, and
  `release/*`, plus caller-supplied patterns);
- the branch in the repository's current worktree;
- dirty, locked, missing, or prunable worktrees; and
- local or remote tips not contained in remote main.

Apply mode refreshes the selected remote before evaluating. It requires every
target as an explicit `--branch`; there is no bulk apply from discovery. It
uses ordinary remote deletion, `git worktree remove`, and `git branch -d` with
exact argument boundaries. It never uses force, constructs shell commands, or
recursively deletes a directory.

## Audit

Refresh remote-tracking state yourself when you want the freshest dry-run,
then inspect all local branches:

```bash
git -C /path/to/Sonder-runtime fetch --prune origin
python scripts/cleanup_merged_branches.py \
  --repo /path/to/Sonder-runtime --json
```

Limit the report without changing anything:

```bash
python scripts/cleanup_merged_branches.py \
  --repo /path/to/Sonder-runtime \
  --branch codex/completed-feature
```

For an additional GitHub PR-state check, add `--require-merged-pr`. This uses
the existing `gh` installation and authenticated session only when requested;
if either is unavailable, the branch is refused. Remote-main ancestry remains
mandatory even when GitHub reports the PR merged.

## Apply

Review the dry-run first, then repeat the exact branch with `--apply`:

```bash
python scripts/cleanup_merged_branches.py \
  --repo /path/to/Sonder-runtime \
  --branch codex/completed-feature \
  --require-merged-pr \
  --apply --json
```

For an eligible target with a remote branch, the order is remote branch,
worktree, local branch, then worktree metadata pruning. A failure stops that
target at the failed stage and is reported without exposing Git or credential
output. Re-run dry-run before retrying a partial cleanup.
