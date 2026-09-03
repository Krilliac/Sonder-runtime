# WP1 Two-Hundred-Ninety-Eighth Slice — improvement report formatting

## Boundary

The pure renderer that turns the improvement report dictionary into the
operator-facing text block (`format_improvement_report`) now lives in
`sonder_runtime/domain/improvement_report_formatting.py` under the same
name, with the readiness, learning provenance, memory, context, autonomy,
MCP and capped issue sections unchanged. `server.py` keeps
`format_improvement_report` as an identity-preserving alias import, so
`improvement_report` and the `/improve` surfaces call the same object.

`improvement_report_data` deliberately did not move: it reads the database,
the context ledger and the MCP runtime, which belong to the application and
adapter boundaries.

## Evidence

- `tests/test_improvement_report_formatting_boundary.py` verifies the root alias identity, the full section order, the unmeasured and unavailable branches, the eight-issue cap, and the empty-report defaults.
- `python -m pytest -q tests/test_improvement_report_formatting_boundary.py tests/test_server_helpers.py -k improvement`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
