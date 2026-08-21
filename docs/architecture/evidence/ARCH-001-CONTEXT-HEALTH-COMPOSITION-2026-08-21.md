# ARCH-001 context-health composition slice — 2026-08-21

## Scope

This is one bounded extraction from the remaining `server.py` composition
blockers. The read-only `context_health_data` route family remains exposed by
the legacy MCP/command surface, but its snapshot contract now lives in
`sonder_runtime.application.context_health.ContextHealthService`.

The application service has no server or database import. The server composes
four explicit ports: session/project identity, bounded memory persistence,
context policy, and token/meter metrics. The compatibility wrapper retains the
existing selectors, database implementation, response keys, formatting route,
and monkeypatch seams.

## Evidence

- `sonder_runtime/application/context_health.py`
- `tests/test_context_health_application.py`
- `tests/test_context_health_composition.py`
- `tests/test_server_helpers.py::test_context_health_reports_session_and_memory`
- `tests/test_server_helpers.py::test_context_health_formats_console_meter`

Focused verification:

```text
python -m pytest -q tests/test_context_health_application.py tests/test_health_formatting.py tests/test_wp1_repl_facade.py tests/test_server_helpers.py -k "context_health or health_formatting or facade"
16 passed, 216 deselected
```

The architecture checker and compile check are run separately because this
slice does not alter the broader server boundary or any excluded web,
selfmod, or extension paths. This evidence remains `implemented_unverified`
until the broader server composition audit and formal checklist promotion are
complete.
