# Legacy model-step boundary

This slice closes the remaining explicit session gap around the legacy
generation callable. `run_model_step` is an application-owned compatibility
boundary. It defers request admission until the existing provider dispatch
scope actually sends a request, records the exact `ModelRequest` options and
history, and completes or fails the same request without changing the legacy
exception that the caller receives.

`server._offload_impl` accepts an optional `session` identity and uses the
boundary for both its direct and learning paths. `agent` and
`workbench_agent` accept the same optional identity; every agent decision and
format-repair call gets a distinct turn in the shared session, while the first
call records the user task once. Tier escalation carries that identity through
the attempts. Provider payload capture remains owned by `_post` and
`dispatch_provider`, so effective retries and failover stay visible under the
same request ID without recording endpoint URLs or headers.

The compatibility defaults remain unchanged: an omitted session is transient
for these legacy paths, and `session="none"` explicitly opts out. A cache-only
or learning hit has no provider attempt and is captured retrospectively as a
request/response pair. A provider or model failure appends only an allowlisted
domain code (`model.failed`) and re-raises the original `ModelCallError` (or
other legacy exception). A capture write failure remains an integrity failure
and cannot trigger a retry.

## Evidence

The focused compatibility suite uses SQLite repositories and offline/fake
generators:

```text
D:\sonder-runtime\venv\Scripts\python.exe -m pytest -q tests/test_legacy_model_steps.py
D:\sonder-runtime\venv\Scripts\python.exe -m pytest -q tests/test_provider_attempt_capture.py tests/test_agent_tools.py tests/test_standalone_agent_lanes.py tests/test_server_helpers.py
D:\sonder-runtime\venv\Scripts\python.exe -m pytest -q tests/test_offload_schema.py tests/test_model_error_formatting_adapter.py tests/test_server_sessions.py tests/test_session_capture_once.py tests/test_lazy_legacy_model_boundary.py tests/test_wp1_legacy_root_boundary.py tests/test_wp1_root_server_boundary.py
D:\sonder-runtime\venv\Scripts\python.exe -m pytest -q tests/test_native_mcp.py tests/test_mcp_stdio_transport.py tests/test_reloadable_mcp.py tests/test_wp8_mcp_compatibility.py tests/test_api003_official_mcp_sdk.py tests/test_mcp_dependency.py tests/test_mcp_primitives.py tests/test_mcp_runtime_formatting_boundary.py tests/test_mcp_task_handler.py
D:\sonder-runtime\venv\Scripts\python.exe -m compileall -q server.py sonder_runtime/application/session/model_steps.py tests/test_legacy_model_steps.py
```

Observed results on the isolated branch: 7 focused tests, 373 provider/agent
regressions, 79 server/session regressions, 67 native MCP regressions, 21
additional MCP regressions, and a clean compile/diff check. These are offline
checks; no live model, remote worker, or installed runtime was changed.

## Limits

This is an explicit-session compatibility slice. It does not create sessions
for unscoped legacy calls, migrate direct `_make_generate` consumers outside
`_offload_impl`/agent paths, add automatic restart/resume, or prove provider
power-loss and distributed replication behavior. The durable request snapshot
stores redacted provenance metadata only; the legacy server does not synthesize
new provenance bindings for prompts that did not already carry one. The
independent typed interactive-lane service remains the stronger path for
parent controls, tool receipts, and child-agent continuation.
