# Evidence roadmap rerun — 2026-08-21

The previously interrupted durable-session/provider evidence batch was rerun
to completion from the branch `agent/port-research-findings`.

Exact command:

```text
python -m pytest -q --basetemp C:\Users\Nathan\Documents\Codex\pytest-evidence-roadmap-current tests/test_wp4_compact001_005.py tests/test_crosscutting_extensions.py tests/test_remaining_selfmod_governance.py tests/test_remaining_session_durable_replay.py tests/test_session_repository.py tests/test_composition_job_registry.py tests/test_crosscutting_provider_lifecycle.py tests/test_provider_lifecycle.py tests/test_api003_subprocess_provider.py tests/test_api003_legacy_declaration.py tests/test_mcp_stdio_transport.py tests/test_process_termination_adapter.py tests/production/test_composition_root.py tests/production/test_architecture.py
```

Result:

```text
156 passed, 1 warning in 614.68s (0:10:14)
```

The warning is pytest's inability to write the repository-local
`.pytest_cache` under the host's permission policy; the isolated basetemp
test artifacts were usable and no test failed. The batch covers compaction,
durable session replay/repository behavior, extension isolation, selfmod
governance, job composition, provider lifecycle, MCP subprocess lifecycle,
process termination, composition-root wiring, and architecture enforcement.

This is execution evidence only; the formal master checklist and requirement
ledger remain conservative and are not promoted to `verified` by this rerun.
