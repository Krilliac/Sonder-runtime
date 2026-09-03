# WP1 Three-Hundred-Thirteenth Slice — fanout receipt safety

## Boundary

The receipt-safety checks for durable fanout rows (`_fanout_safe_answer`,
`_fanout_snapshot_allows`) now live in `sonder_runtime/domain/fanout_receipts.py`
as `safe_answer` and `snapshot_allows`, with the marker-based credential
scrubber, its prompt-echo pre-pass (imported from the packaged
`fanout_redaction`) and the scope rules unchanged. The snapshot check takes
the cloud classifier as an injected `is_cloud_model_name` callable.

`server.py` keeps `_fanout_safe_answer` as an identity-preserving alias
import and `_fanout_snapshot_allows` as a thin delegate injecting
`_is_cloud_model_name` at call time, so the existing routing-classifier
monkeypatch seam keeps working for the fanout worker.

## Evidence

- `tests/test_fanout_receipts_boundary.py` verifies the alias identity, credential scrubbing after prompt-echo removal, the snapshot and scope rules through an injected classifier, and the root delegate's classifier seam.
- `python -m pytest -q tests/test_fanout_receipts_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
