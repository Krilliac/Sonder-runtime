# SELFMOD-006: local commits without automatic remote push

Verified against main merge commit `9702677d4708ba0ae9176b4e35321147fa2df18c`.

The guarded deployment boundary rejects `automatic_push` and `remote_push`
before calling the legacy executor. Governance also refuses an automatic
remote-push intent. The live nightly branch path creates only a local candidate
commit; the legacy deploy path creates a local commit only when the starting Git
checkout was clean. Neither path invokes `git push`.

Evidence:

- `sonder_runtime/application/selfmod/selfmod_service.py`: guarded deploy
  rejects push flags before mutation.
- `sonder_runtime/application/selfmod/governance.py`: deployment intent cannot
  authorize automatic remote push.
- `selfmod.py`: deployment uses scoped `git add` and `git commit`; it skips the
  commit when the starting status was dirty.
- `scripts/nightly_selfmod.py`: reviewable branch mode commits within the
  candidate worktree and does not push.
- `tests/production/test_selfmod_boundary.py` and
  `tests/test_selfmod_legacy_integration.py`: push-refusal canaries.
- `tests/test_selfmod.py`: local commit and dirty-checkout preservation canaries.

Local verification at nightly PR head `792892d8`: 22 boundary, bridge, and
nightly tests passed; four dirty-checkout tests passed. The exact merged SHA's
[`ci` run](https://github.com/Krilliac/Sonder-runtime/actions/runs/35829630003)
and [`build-apps` run](https://github.com/Krilliac/Sonder-runtime/actions/runs/35829630024)
both succeeded.

Scope: Sonder-owned guarded selfmod workflows. A human's separate Git command
is outside this requirement.
