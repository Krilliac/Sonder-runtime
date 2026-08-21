# API-003 legacy declaration wiring — 2026-08-21

This focused slice adds an explicit typed declaration to the composed legacy
MCP runtime. `build_legacy_server_mcp_runtime()` now attaches the immutable
`LegacyMcpContract(name="legacy-server", version="1.0", capabilities=("tools",))`
metadata to the adapter; an adapter created directly without composition
remains visibly undeclared.

Evidence: `tests/test_api003_legacy_declaration.py` verifies the declaration
type, identity-preserving factory wiring, and the unconfigured direct-adapter
case. The native stdio transport remains MCP 2.0-only and unchanged by this
slice; no claim is made that API-003 is fully proven. The master checklist and
ledger remain unchanged.

Focused command:

```text
python -m pytest -q tests/test_api003_legacy_declaration.py tests/test_wp1_legacy_root_boundary.py tests/test_wp8_mcp_compatibility.py
```
