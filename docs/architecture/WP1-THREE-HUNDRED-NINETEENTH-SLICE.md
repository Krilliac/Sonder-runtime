# WP1 Three-Hundred-Nineteenth Slice — model-call contracts

## Boundary

Two model-call contracts left `server.py` into two packaged modules.
`sonder_runtime/adapters/model_response_metadata.py` holds
`response_error_metadata`, which parses the allowlisted metadata back out of
an empty-response `ModelCallError` and treats every other error as opaque;
`sonder_runtime/adapters/offload_schema_argument.py` holds `parse_schema_arg`,
which normalizes an offload `schema` argument or raises a typed
configuration `ModelCallError`. Both read or raise the transport's error
class, which is defined in the adapters layer, so that layer is their home;
the metadata parser imports `usage_count` from the packaged model-usage
policy directly. `server.py` keeps `_response_error_metadata` and
`_parse_schema_arg` as identity-preserving alias imports.

## Evidence

- `tests/test_model_call_contracts_boundary.py` verifies the alias identities, metadata extraction from a real empty-response detail with every opaque case, and schema normalization with each typed refusal.
- `python -m pytest -q tests/test_model_call_contracts_boundary.py tests/test_grounded_extraction.py tests/test_server_helpers.py -k 'boundary or schema or metadata or extraction'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
