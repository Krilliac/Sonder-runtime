# WP1 Three-Hundred-First Slice — runtime update formatting

## Boundary

The operator-facing rendering of the runtime source update status
(`_runtime_update_format`) and its presentation-only eligibility verdict
(`_runtime_update_eligibility`) now live in
`sonder_runtime/domain/updates/runtime_update_formatting.py` as
`format_runtime_update` and `runtime_update_eligibility`, with every line,
the running-commit insertion, the restart notice and the refusal order
unchanged. Both take the canonical update branch as an injected
`update_branch` keyword, so the domain never imports the Git adapter.

`server.py` keeps `_runtime_update_format` and `_runtime_update_eligibility`
as thin compatibility delegates that inject `git_tools.RUNTIME_UPDATE_BRANCH`
at call time. `git_tools.runtime_update` remains the authority that repeats
every check before touching a checkout; nothing about that moved.

## Evidence

- `tests/test_runtime_update_formatting_boundary.py` verifies that the root delegates render through the domain with the canonical branch, the full report layout, the running-commit, restart, cached-remote and explicit-outcome lines, and the eligibility refusal order.
- `python -m pytest -q tests/test_runtime_update_formatting_boundary.py tests/test_git_tools.py -k 'update or boundary'`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
