# Native MCP transport migration slice

Status: implemented on `agent/wp1-execution-status` as an explicit opt-in
surface (`python -m sonder_runtime mcp --native`).

## Scope

The native path composes the bounded JSON-RPC stdio transport with the
application-owned `ToolExecutor` port and a deterministic generated catalog.

**The current catalog is generated, not listed here.** The "Native MCP tools"
table in [`generated/runtime-reference.md`](generated/runtime-reference.md)
(source: `sonder_runtime.bootstrap.native_mcp.native_tool_registry`) is the
authority for which tools the native surface serves, next to the legacy
default catalog in the same file. This note previously enumerated the tools by
hand and drifted: it went on saying `vision_analyze` was not exposed after it
shipped, and its counts lagged both catalogs. Regenerate with
`python scripts/generate_documentation_catalogs.py --write`; the `--check`
gate fails when either catalog changes without the reference.

What the slices below established still holds:

- Filesystem/workbench aliases (`directory_create`, `file_edit`, `file_read`,
  `file_write`, `workspace_run`) normalize to canonical executor names, and
  the native boundary validates their JSON schemas before execution. Each
  call gets a fresh MCP `OperationContext`, and executor results retain
  bounded error and evidence fields.
- Read-only inspection tools are dispatched through
  `InspectionService`/`InspectionExecutorAdapter`, preserving the read-only
  policy and evidence shape rather than duplicating inspection logic.
- `image_inspect` reports bounded header metadata and a digest; it makes no
  visual-semantic or model-routing claim.
- `vision_analyze` is routed to the application's typed `VisionService`
  (local-only `VisionGateway` port, guarded filesystem input). When the
  composed application has no vision service the call returns
  `isError: true` with `DependencyUnavailable` instead of reaching a model.
- `process_list` and `process_memory_risk_inspect` keep the exact opt-in gate
  and content-free aggregate risk reporting; they do not expose command
  lines, memory bytes, module paths, or virtual addresses.
- `artifact_risk_inspect`, `verify_artifact`, and `fetch_artifact` use the
  package-owned adapters; the network path keeps the SSRF-safe opener and the
  explicit `SONDER_WEB_TOOLS` gate, and legacy token/bypass parameters are not
  part of the native schema.
- Guarded mutation tools (`file_copy`, `file_move`, `file_batch_write`,
  `file_delete`, `json_patch`, `text_patch`) use the packaged transfer,
  transactional batch, explicit-delete-confirmation, and patch adapters and do
  not accept legacy authentication tokens.

The historical server MCP catalog remains the default compatibility path until
catalog parity and complete application-service coverage are proven. The
native catalog covers far fewer tools than the legacy one (the read-only
workbench family runs through the typed tool gateway on both
surfaces, see `evidence/TOOL-READ-FAMILY-TYPED-GATEWAY-2026-09-02.md`) and
does not claim full parity, API-003, or TOOL-001 completion.

## Evidence

- `tests/test_native_mcp.py`: deterministic catalog, alias normalization,
  schema rejection, and end-to-end transport to application tool-port
  translation.
- `tests/test_mcp_stdio_transport.py`: negotiation, bounded frames,
  subscriptions, malformed input, and catalog limits.
- Focused result: **66 passed** for the artifact-acquisition/native-executor
  slice.
- `scripts/check_architecture.py`, compileall, and diff checks pass.
