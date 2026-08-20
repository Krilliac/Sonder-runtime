# WP1 One-Hundred-Thirty-Ninth Slice — Startup Capability Ownership

## Boundary

`bootstrap/capabilities.py` was still the implementation owner for the
process-frozen startup capability policy. That ownership now lives in the
canonical packaged adapter `sonder_runtime.adapters.runtime_capabilities`.
The bootstrap module remains a compatibility re-export, preserving class and
function identity for existing callers. The SPEC-5 composition roots now
depend directly on the packaged boundary.

## Preserved invariants

- `RuntimeCapabilities` remains a frozen dataclass with independent flags.
- `freeze()` still accepts exactly one process-wide initialization.
- `current()` still rejects reads before initialization.
- Test reset remains available without exposing mutable production state.
- No server, gateway, repository, workflow, event, tool, preference,
  evaluation, inspection, or UnitOfWork files were changed.

## Evidence

- `python -m pytest tests/test_spec5_capabilities.py -q` — pass.
- `python -m compileall -q sonder_runtime/adapters/runtime_capabilities.py sonder_runtime/bootstrap/capabilities.py sonder_runtime/bootstrap/main.py sonder_runtime/bootstrap/container.py` — pass.
- `python scripts/check_architecture.py` — pass with zero violations.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.
