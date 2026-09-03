# WP1 Three-Hundred-Twenty-Sixth Slice — fanout receipts

## Boundary

The serializable fanout receipt (`_fanout_receipt`) now lives in
`sonder_runtime/adapters/fanout_receipt.py` as `build_receipt`, with the
counts, usage, plan and execution skips, live cooldown derivation and sorted
answer and failure rows unchanged. It reads the packaged fanout store and
the packaged `fanout_limits`, so the adapters layer is its home. The
admission record is injected as `admission`; `server.py` keeps
`_fanout_receipt` as a thin delegate passing `_fanout_admission` at call
time, so the existing receipt and classifier monkeypatch seams keep working.

## Evidence

- `tests/test_fanout_receipt_boundary.py` verifies that the root delegate matches the packaged receipt and returns None for a missing run, and the full receipt shape: counts, skips with live cooldowns, usage, the injected admission, sorted rows and no prompt.
- `python -m pytest -q tests/test_fanout_receipt_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
