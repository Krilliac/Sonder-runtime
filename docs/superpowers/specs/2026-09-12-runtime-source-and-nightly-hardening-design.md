# Runtime Source and Nightly Hardening Design

**Date:** 2026-09-12

**Scope:** Reconcile the current Sonder source topology, repair the nightly
self-improvement path-boundary failure, and remove a one-shot CI workflow that
fails after its feature branch is merged.

## Context

GitHub `origin/main` is the authoritative source at `cd40b944`. The recent
Ollama probe, REPL, and Spanda branches are already represented in that tree.
The large `codex/control-state-rehearsal` worktree is a stale composite whose
tree would remove current features, so it must remain preserved rather than
merged wholesale. The dirty `codex/sol-web-app-surface` worktree is also
preserved and is outside this change.

The live MCP process and the nightly script use different source roots. The
nightly process can load `D:\sonder-runtime`, while its inherited
`SONDER_EMOTION_VECTORS` and `SONDER_SYSTEM_PROFILE` values point at the
separate `D:\sonder-wt\tier-provider-dispatch` deployment copy. The existing
containment checks correctly reject those cross-root paths. The packaged
`system_profile` implementation also resolves its default workspace to the
package directory instead of the checkout root, so the production launcher’s
checkout-level profile override is rejected even when it belongs to the
deployment.

The `restore-reloadable-mcp` workflow added with Spanda is a one-shot recovery
mechanism. It hard-codes the deleted `feat/spanda-rsc` branch and fails on a
push to `main` after the PR is merged. Its helper script has no remaining
consumer once the workflow is removed.

## Goals

1. Make `sonder_runtime.platform.system_profile.workspace_root()` identify the
   repository checkout, matching the established filesystem-root contract.
2. Make `scripts/nightly_self_improve.py` rebind workspace-local profile and
   emotion-vector paths to its own checkout, preserving valid in-checkout
   overrides and replacing cross-checkout or symlink-escaping values with the
   checkout defaults.
3. Remove the obsolete one-shot restore workflow and helper script, while
   retaining the actual `reloadable_mcp.py` implementation and Spanda feature.
4. Add behavior tests that reproduce the stale-environment and profile-root
   failures before the implementation changes.
5. Validate the branch with focused tests, repository architecture gates, and
   the CI-equivalent suite before publishing it.

## Non-goals and safety boundaries

- Do not merge the 96-commit `codex/control-state-rehearsal` branch or any
  stale August snapshot; no equivalent code is lost by leaving those worktrees
  intact.
- Do not delete or force-reset any existing worktree, branch, deployment copy,
  database, or lock file.
- Do not enable cloud or remote inference.
- Do not switch the running MCP process from
  `D:\sonder-wt\tier-provider-dispatch` during this slice. A production
  cutover requires a separate staged deployment, source-identity check, and
  runtime smoke test after this branch is verified.

## Design

### Checkout-root ownership

The canonical profile module will derive its root from
`Path(__file__).resolve().parents[2]`, returning the directory containing the
repository’s root compatibility files and `server.py`. This preserves the
existing monkeypatchable function seam and makes both the default profile and
an explicit `SONDER_SYSTEM_PROFILE=<checkout>\system_profile.md` override
valid. The packaged profile copy remains tracked for packaging compatibility;
the source-checkout default is the root profile used by the legacy surface.

### Nightly path binding

`nightly_self_improve` will bind the two mutable, workspace-owned files before
importing `sonder_paths` or `server`. The helper will:

1. resolve the supplied checkout root;
2. interpret empty or relative values under that root;
3. resolve existing path components to detect links/reparse-style escapes;
4. retain an override only when it remains inside the checkout; and
5. otherwise select the root-level default file for that variable.

The helper will return the names of variables it had to rebind so the nightly
log can report a bounded, path-free diagnostic. It will not copy files or
change the process-wide user environment. This keeps independent checkouts
isolated while retaining explicit in-checkout customization.

### CI cleanup

The one-shot workflow and its only-purpose helper will be removed in one
reviewable commit. The actual reloadable MCP implementation remains in the
runtime tree. A repository policy test will assert that the retired workflow
and helper are absent, preventing the deleted-branch recovery job from being
reintroduced accidentally.

### Error handling

Malformed, missing, or escaping workspace overrides fail closed to the
checkout default in the nightly launcher. The lower-level profile and emotion
vector modules continue to reject explicit paths outside their workspace; the
launcher repair does not weaken those guards. An inability to resolve a path
is treated as escaping and uses the default, rather than allowing an
unverified path to cross the boundary.

## Verification

The red-green cycles cover:

- profile workspace ownership and acceptance of a checkout-level profile;
- rehoming stale absolute nightly overrides and preserving a valid in-root
  override;
- absence of the obsolete CI workflow/helper;
- the existing profile, emotion-vector, logging, and nightly model-selection
  tests.

After the focused cycles, run the repository’s architecture, requirement
evidence, error-signal, and history-privacy gates, followed by the full
`pytest -q -n auto --dist load --durations=25` suite. The branch is publishable
only if each command reaches its terminal result with exit code zero and the
diff contains no unrelated worktree or deployment mutations.
