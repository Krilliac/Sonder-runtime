# WP1 Three-Hundred-Second Slice — MCP runtime formatting

## Boundary

The operator-facing rendering of the MCP runtime status
(`format_mcp_runtime`) and the content-free refresh-error reducer
(`_safe_mcp_error`) now live in `sonder_runtime/domain/mcp_runtime_formatting.py`
as `format_mcp_runtime` and `safe_mcp_error`, with every status, provenance,
action, refresh and notification line unchanged. The renderer takes the
provenance recovery action as an injected `recovery_action` callable, so the
domain never imports the reloadable-MCP adapter.

`server.py` keeps `format_mcp_runtime(data=None)` as a thin compatibility
delegate that still collects `mcp_runtime_data()` when no data is passed and
injects `_safe_mcp_recovery_action` at call time, so the existing
`mcp_runtime_data` and `format_mcp_runtime` monkeypatch seams keep working.
`_safe_mcp_error` remains an identity-preserving alias.
`_safe_mcp_recovery_action` deliberately did not move: it calls the
reloadable-MCP adapter.

## Evidence

- `tests/test_mcp_runtime_formatting_boundary.py` verifies the alias identity, the content-free error reduction, the full status block for a populated and an empty state, the provenance, action and refresh-failure lines through an injected action, and the root wrapper's live-data and recovery-action wiring.
- `python -m pytest -q tests/test_mcp_runtime_formatting_boundary.py tests/test_reloadable_mcp.py tests/test_server_helpers.py -k 'mcp or boundary'`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
