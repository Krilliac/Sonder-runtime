# WP1 Sixty-Ninth Slice: Runtime-policy path boundary

## Status

Implemented locally; focused runtime-policy verification passed.

## Scope

The packaged runtime-policy adapter now consumes its state-file location from
`sonder_runtime.platform.paths` instead of importing the root `sonder_paths`
module directly. The platform boundary remains the compatibility-backed owner
of path resolution, so `SONDER_RUNTIME_POLICY` overrides and historical
`SONDER_HOME` semantics are unchanged.

This is one caller migration only. The root implementation and platform
compatibility boundary remain in place for callers not covered by this slice.

## Path contract

`runtime_policy.policy_path()` returns the explicit `SONDER_RUNTIME_POLICY`
override when set; otherwise it resolves `state_path("runtime_policy.json")`
through `sonder_runtime.platform.paths`.

## Verification

- `python -m pytest -q tests/test_runtime_policy.py` — passed.
- `python -m compileall -q sonder_runtime server.py` — passed.
- `python scripts/check_architecture.py` — passed.
- `python scripts/check_requirement_evidence.py` — passed.
- `git diff --cached --check` and `git diff --check` — passed.
