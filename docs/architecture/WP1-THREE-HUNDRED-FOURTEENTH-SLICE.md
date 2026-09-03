# WP1 Three-Hundred-Fourteenth Slice — empty model response detail

## Boundary

The description of an empty model response (`_empty_model_response_detail`)
now lives in `sonder_runtime/domain/model_response_detail.py` as
`empty_model_response_detail`, with the shape-only metadata (thinking length,
tool-call count, evaluation count and a normalized done reason) unchanged; it
imports `usage_count` from the packaged model-usage policy directly.
`server.py` keeps the root name as an identity-preserving alias import, so
the chat transport's empty-response path calls the same object.

## Evidence

- `tests/test_model_response_detail_boundary.py` verifies the alias identity, the shape-only metadata line, that reasoning text never appears, and the degraded bare message.
- `python -m pytest -q tests/test_model_response_detail_boundary.py tests/test_server_helpers.py -k 'boundary or empty or response'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
