# WP1 Three-Hundred-Forty-Ninth Slice — identifier resolution

## Boundary

`_resolve_session` and `_resolve_project` in `server.py` shared identical
logic: strip, default on empty, return `None` on the literal `"none"`.
That logic moved into `sonder_runtime/domain/identifier_resolution.py` as
`resolve_identifier(value, default)`, parameterized so both callers bind
their own default (`DEFAULT_SESSION`, `DEFAULT_PROJECT`).

The root names remain as compatibility delegates that call
`_resolve_identifier_impl(value, DEFAULT_...)`. Both are monkeypatched in
tests (`test_ask_memory_*.py`, `test_remember_*.py`, `test_loop.py`); the
delegates preserve those seams.

## Evidence

- `tests/test_identifier_resolution_boundary.py` verifies delegate wiring
  (`server._resolve_session("")` returns `DEFAULT_SESSION`), empty/None/
  whitespace defaults, `"none"` normalization (case-insensitive), value
  passthrough, and whitespace stripping.
- `python -m pytest -q tests/test_identifier_resolution_boundary.py` — 8 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime tests` — silent, exit 0
- `git diff --check` — silent, exit 0
