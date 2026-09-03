# WP1 Three-Hundred-Ninth Slice — context pack argument normalization

## Boundary

The context-pack tool's argument helpers (`_context_pack_paths`,
`_context_pack_int`, `_context_pack_utf8_prefix`) now live in
`sonder_runtime/domain/context/pack_arguments.py` as `pack_paths`, `pack_int`
and `pack_utf8_prefix`, with every validation message, clamp and the
codepoint-safe byte clip unchanged. `server.py` keeps the three root names as
identity-preserving alias imports, so the `context_pack` tool calls the same
objects. The tool body itself did not move: it resolves and reads files
through the guarded read contract.

## Evidence

- `tests/test_context_pack_arguments_boundary.py` verifies the three alias identities, JSON and list path shapes with every rejection, integer clamping with defaults, and the codepoint-safe UTF-8 prefix.
- `python -m pytest -q tests/test_context_pack_arguments_boundary.py tests/test_context_pack.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
