# WP1 Three-Hundred-Twenty-Seventh Slice — agent work coverage

## Boundary

The family that decides whether an agent's validators and verifiers
covered the work it changed now lives in
`sonder_runtime/adapters/agent_work_coverage.py`: `NO_OP_COMMAND_FLAGS`,
`BUILD_DRIVERS`, `normalized_path`, `path_within`, `explicit_command_paths`,
`paths_covered_by_targets`, `build_command_examines`, `verification_covers`,
`mutation_records` and `validation_covers`, with every table, predicate and
tool-specific branch unchanged. It resolves paths through the packaged
filesystem adapter, parses patches through the packaged text-patch adapter
and reads argv and batch operations from the packaged activity-command
policy, so the adapters layer is its home. `server.py` keeps all ten root
names as identity-preserving alias imports, so the agent loop's validation
and verification gates and every existing monkeypatch seam keep working.

## Evidence

- `tests/test_agent_work_coverage_boundary.py` verifies the ten alias identities, path normalization and containment, explicit command paths, build-command examination, verifier scope narrowing, mutation records per tool, and validator coverage of changed disk state.
- `python -m pytest -q tests/test_agent_work_coverage_boundary.py tests/test_agent_verification_gate.py tests/test_verification_examines_work.py tests/test_agent_tools.py tests/test_harness_root_confinement.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
