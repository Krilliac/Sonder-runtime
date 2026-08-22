# WP1 Two-Hundred-Eighty-Fourth Slice — native MCP image metadata parity

## Boundary

Added `image_inspect` to the native MCP catalog and typed executor. It uses
the existing bounded filesystem workbench implementation to inspect PNG, GIF,
BMP, JPEG, PPM, and SVG headers, dimensions, size, and digest without loading
pixels into a model or making a vision claim.

## Evidence

- Native MCP, typed executor, workbench, and stdio regressions pass:
  **49 passed, 1 skipped**.
- SVG metadata, dimensions, and digest evidence were verified through the
  typed executor in a guarded temporary workspace.
- The native catalog now reports **32** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

`vision_analyze` remains a separate local-model/privacy migration surface.
Full MCP parity, remaining legacy tool families, epoch-2 bridge retirement,
and formal checklist acceptance remain incomplete.
