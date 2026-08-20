# WP1 Eighty-Third Slice: workflow state-path seam

`sonder_runtime.adapters.filesystem.workflow_store` now resolves its mutable
state home through `sonder_runtime.platform.paths` instead of importing the
root `sonder_paths` module directly.

The workflow persistence contract is unchanged: explicit
`SONDER_WORKFLOWS` overrides remain workspace-confined, default state remains
under the per-user Sonder home, legacy workspace files are copied once, and
atomic writes plus containment checks are unchanged.

## Evidence

- `tests/test_workflows.py::test_workflow_state_home_uses_canonical_platform_paths`
- workflow persistence, containment, concurrency, and server regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
