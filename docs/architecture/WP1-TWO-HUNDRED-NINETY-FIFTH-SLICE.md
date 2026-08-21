# WP1 Two-Hundred-Ninety-Fifth Slice — transactional archive extraction executor

## Boundary

The remaining legacy `archive_extract` tool is now reachable through the
packaged `ToolExecutorAdapter`. The adapter delegates to the existing
stdlib-only archive implementation, preserving prevalidation, path and link
rejection, bounded expansion, staging cleanup, and no-replace promotion. The
executor derives `developer_authorized` only from the request context; legacy
tokens and approvals are not accepted by this typed seam.

This slice deliberately does not change `native_mcp.py`, the application
composition root, or the web-provider adapters. Native catalog exposure and
full legacy-surface retirement remain separate follow-up work.

## Evidence

- `tests/test_archive_extract_executor.py`: successful ZIP extraction and
  existing-destination no-replacement behavior through `ToolExecutorAdapter`.
- Existing archive policy coverage remains in `tests/test_archive_tools.py`.
- Protected files are unchanged by this slice: `native_mcp.py`,
  `bootstrap/app.py`, and the web-provider lane.
