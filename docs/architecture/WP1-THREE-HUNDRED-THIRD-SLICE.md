# WP1 Three-Hundred-Third Slice — fanout limits and admission

## Boundary

The fanout receipt limits decoder (`_fanout_limits`) and the immutable
admission record (`_fanout_admission`) now live in
`sonder_runtime/domain/fanout_admission.py` as `fanout_limits` and
`fanout_admission`, with every clamp, the conservative snapshot default, the
cloud and local-thinking budget floors, the serial-local scheduling bound
and the privacy disclosure unchanged. The admission record takes the cloud
and thinking classifiers and the local thinking floor as injected keywords,
so the domain never reaches the routing cache.

`server.py` keeps `_fanout_limits` as an identity-preserving alias import and
`_fanout_admission` as a thin compatibility delegate that injects
`_is_cloud_model_name`, `_known_thinking_model` and
`LOCAL_THINKING_MIN_NUM_PREDICT` at call time, so the existing monkeypatch
seams keep working. `_known_thinking_model` deliberately did not move: it
reads the thinking-capability cache under its lock.

## Evidence

- `tests/test_fanout_admission_boundary.py` verifies the alias identity, limit clamps and defaults, the admission record through injected classifiers (snapshot authority, budget floors, scheduling bound, cost and privacy blocks), the local-only disclosure, and the root wrapper's live-classifier wiring.
- `python -m pytest -q tests/test_fanout_admission_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
