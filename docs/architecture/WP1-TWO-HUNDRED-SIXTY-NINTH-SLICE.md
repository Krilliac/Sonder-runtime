# WP1 Two-Hundred-Sixty-Ninth Slice — complete model-error caller migration

## Boundary

Migrated the final four production call sites of the root
`_format_model_call_error` helper: direct fanout authorization failures and
natural-language routing failures in `sonder()`. The packaged runtime adapter
now owns endpoint classification and rendering across all canonical paths; the
root helper remains only as a compatibility surface.

## Evidence

- A global AST regression test proves `server.py` contains no production call
  to `_format_model_call_error`.
- Model-error, fanout, ensemble, REPL, and server-helper regressions pass:
  **326 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

This completes the model-error caller seam only. Cloud-policy callers, MCP
parity, epoch-2 bridge retirement, and formal checklist acceptance remain
outside this slice.
