# WP1 Two-Hundred-Seventy-Eighth Slice — REPL cloud-policy seam cleanup

## Boundary

Updated the REPL cloud-tier tests to inject the packaged
`_cloud_allowed_policy` seam used by `server.available_tiers()`. This removes
the last REPL dependence on monkeypatching the retired zero-argument
`cloud_allowed()` compatibility wrapper while preserving disabled/ enabled
tier listing, model switching, and route recommendation behavior.

## Evidence

- Cloud/model-listing/route subset passes: **13 passed**.
- Full `tests/test_repl_input.py` passes: **79 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

This is a REPL test/injection seam cleanup; it does not claim MCP parity,
epoch-2 bridge retirement, or formal checklist acceptance.
