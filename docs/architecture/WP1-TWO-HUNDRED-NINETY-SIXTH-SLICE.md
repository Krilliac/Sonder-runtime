# WP1 Two-Hundred-Ninety-Sixth Slice — fanout prompt-echo redaction

## Boundary

The pure prompt-echo redaction that guards durable fanout receipts
(`_fanout_redact_prompt_echo`) now lives in
`sonder_runtime/domain/fanout_redaction.py` as `redact_prompt_echo`, with the
seed-sampled span search, the fixed comparison budget, the labelled-credential
short-span rule and both redaction markers unchanged. `server.py` keeps
`_fanout_redact_prompt_echo` as an identity-preserving alias import, so
`_fanout_safe_error` and the fanout worker call the same object.

`_fanout_safe_error` deliberately did not move: it constructs the adapter's
`ModelCallError`, which the domain layer may not import.

## Evidence

- `tests/test_fanout_redaction_boundary.py` verifies the root alias identity, a full echo, a partial quoted span, a short labelled-credential span, and the empty-input cases.
- `python -m pytest -q tests/test_fanout_redaction_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
