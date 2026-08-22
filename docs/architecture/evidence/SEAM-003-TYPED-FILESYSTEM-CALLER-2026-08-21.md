# SEAM-003 typed filesystem caller migration

## Bounded slice

`ToolExecutorAdapter` now routes its `read_file` branch through
`GuardedFileSystemAdapter`, a concrete provider for the application
`FileSystem` port.  The provider is intentionally read-only in this slice.

The request carries the existing `file_ops.read_file` policy inputs explicitly:
`max_bytes`, `extra_roots`, `bypass`, and `developer_authorized`.  The provider
delegates containment, sensitive-path classification, and bounded decoding to
the existing guarded adapter; it does not duplicate or weaken those checks.

## Security invariants

- The caller still receives a failed result for a protected root-level
  `server.py` read (`PermissionError`), rather than content.
- The provider does not grant authorization from `OperationContext`; the
  legacy flags remain explicit request inputs with their prior defaults.
- No write, delete, move, terminal, HTTP, MCP, session, memory, training,
  data, agent, evaluation, update, operations, model, compaction, or selfmod
  paths were changed.

## Verification

- `python -m pytest -q tests/test_seam003_typed_filesystem_caller.py
  tests/test_filesystem_port_wp3.py tests/test_legacy_tool_executor.py`
- `python -m compileall -q sonder_runtime tests`
- `python scripts/check_architecture.py`
- `python scripts/check_evidence_documents.py`
- `git diff --check`
