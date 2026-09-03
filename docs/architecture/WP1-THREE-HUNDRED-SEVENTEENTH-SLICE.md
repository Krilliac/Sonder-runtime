# WP1 Three-Hundred-Seventeenth Slice — context compaction plan formatting

## Boundary

The renderer for a context compaction plan (`format_context_compaction_plan`)
now lives in `sonder_runtime/domain/context/compaction_plan_formatting.py`
under the same name, with the context lines and the prioritized action list
unchanged. `server.py` keeps `format_context_compaction_plan` as an
identity-preserving alias import, so the compaction-plan tool and command
call the same object. `context_compaction_plan_data` deliberately did not
move: it reads live context health.

## Evidence

- `tests/test_compaction_plan_formatting_boundary.py` verifies the alias identity, a fully populated plan line by line, and the empty-plan defaults.
- `python -m pytest -q tests/test_compaction_plan_formatting_boundary.py tests/test_server_helpers.py -k 'boundary or compaction'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
