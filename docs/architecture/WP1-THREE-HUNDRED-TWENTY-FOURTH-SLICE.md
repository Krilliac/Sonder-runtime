# WP1 Three-Hundred-Twenty-Fourth Slice — bounded hosted generation

## Boundary

The hard-bounded wrapper for hosted agent generation
(`_bounded_cloud_agent_generate`) and its ceilings (`_CLOUD_AGENT_NUM_PREDICT`,
`_CLOUD_AGENT_OUTPUT_BUDGET`) now live in
`sonder_runtime/adapters/bounded_cloud_generation.py` as
`bounded_cloud_generate`, `CLOUD_AGENT_NUM_PREDICT` and
`CLOUD_AGENT_OUTPUT_BUDGET`, with the usage accounting, the failure charging
rule, the shared budget state and the wrapper attributes unchanged. It raises
and catches the transport's `ModelCallError`, which is defined in the
adapters layer, so that layer is its home; it imports the packaged usage
count and rough token estimate directly. `server.py` keeps all three root
names as identity-preserving alias imports. `_CLOUD_AGENT_WRITE_CHUNK_HINT`
stays with the agent loop's local budget constants.

## Evidence

- `tests/test_bounded_cloud_generation_boundary.py` verifies the alias identities and ceilings, usage charged from the larger of reported and estimated counts, refusal once the allowance is spent, failure charging with and without an attempt, and a shared budget state.
- `python -m pytest -q tests/test_bounded_cloud_generation_boundary.py tests/test_server_helpers.py -k 'boundary or bounded or budget'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
