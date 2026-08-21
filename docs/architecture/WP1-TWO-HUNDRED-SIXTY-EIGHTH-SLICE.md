# WP1 Two-Hundred-Sixty-Eighth Slice — agent decision error migration

## Boundary

Rewired `_agent_generate_decision` and `_agent_turn` decision/finalization
failure paths to use the packaged runtime model-error adapter directly. The
root formatter remains a compatibility delegate for other callers.

## Evidence

- AST regression tests prove the agent decision and turn functions contain no
  call to the root `_format_model_call_error` wrapper.
- Agent tools, verification, integration, and model-retry regressions pass:
  **205 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Remaining agent/reporting and cloud-policy seams, MCP parity, and epoch-2
bridge retirement remain outside this slice. Formal checklist completion is
not claimed.
