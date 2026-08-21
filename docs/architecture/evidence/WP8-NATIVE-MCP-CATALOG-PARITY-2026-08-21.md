# Native MCP catalog parity — 2026-08-21

## Result

No new native MCP capability was added at this checkpoint. The packaged
adapter/executor catalog is already fully exposed.

The exact comparison is:

- Packaged `ToolExecutorAdapter` capabilities: 31.
- Packaged `InspectionExecutorAdapter` capabilities: 9.
- Combined canonical adapter/executor capabilities: 40.
- Native MCP canonical capabilities: 40; missing: none; unexpected: none.
- Native-only supported gateway capability: `vision_analyze`.
- Native compatibility aliases: `directory_create`, `file_edit`, `file_read`,
  `file_write`, and `workspace_run`.

The parity regression is in `tests/test_native_mcp.py` and verifies the exact
40-name set, the five non-canonical aliases, and the separate vision gateway
entry. Because the catalog is at parity, adding speculative descriptor or
route code would expand the surface without a packaged adapter/executor to
back it.

## Evidence

```text
pytest -q tests/test_native_mcp.py
18 passed
```
