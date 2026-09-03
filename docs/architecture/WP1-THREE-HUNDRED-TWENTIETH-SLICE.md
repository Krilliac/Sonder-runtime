# WP1 Three-Hundred-Twentieth Slice — agent call signatures

## Boundary

The stable signature for equivalent host-scoped agent tool calls
(`_agent_call_signature`) now lives in
`sonder_runtime/adapters/agent_call_signature.py` as `call_signature`, with
the archive-create input resolution, the per-tool path keys and the real
filesystem resolution unchanged. It resolves paths through the filesystem
and calls the packaged archive adapter, so the adapters layer is its home.
The path-confinement tables stay with the dispatcher and are injected:
`server.py` keeps `_agent_call_signature` as a thin delegate passing
`_PROJECT_SCOPED_PATH_TOOLS` and `_project_scoped_path_key` at call time.

## Evidence

- `tests/test_agent_call_signature_boundary.py` verifies that the root delegate matches the packaged signature, that equivalent path spellings collapse, the tool-specific path keys and non-dict arguments, and archive-create input resolution against the root.
- `python -m pytest -q tests/test_agent_call_signature_boundary.py tests/test_content_digest.py tests/test_log_inspect.py tests/test_project_detect.py tests/test_symbol_index.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
