# WP1 Three-Hundred-Forty-First Slice — agent observation quality

## Boundary

Moved `_agent_observation_ok` from server.py into
`sonder_runtime/domain/agent_observation_quality.py` as `observation_ok`.

Pure string classification: returns False when observation text signals a
failed tool step (ERROR: prefix, ok: false YAML, : fail suffix,
validation_failed prefix, [fail] tag). Stdlib only.

The root `server._agent_observation_ok` is an identity-preserving alias.
Monkeypatch surface at `tests/test_agent_evidence_quality_boundary.py:45`
confirmed working via alias resolution.

The error-signal baseline entry for `startswith("ERROR:")` was updated to
follow the code to its new path and scope.

## Evidence

- `tests/test_agent_observation_quality_boundary.py` verifies identity alias,
  ok/error classification for each failure pattern.
- `python -m pytest -q tests/test_agent_observation_quality_boundary.py tests/test_agent_evidence_quality_boundary.py` — 14 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_error_signals.py` — silent, exit 0 (baseline entry moved, not regenerated)
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/agent_observation_quality.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
