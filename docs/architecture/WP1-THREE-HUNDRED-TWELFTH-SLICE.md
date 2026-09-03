# WP1 Three-Hundred-Twelfth Slice — thinking controls

## Boundary

The thinking controls for hosted and local reasoning models now live in
`sonder_runtime/domain/thinking_controls.py`: the per-model hosted policy
(`apply_cloud_thinking_policy`), the narrow `think=false` allow-list
(`cloud_can_disable_thinking`), the recognizer for Ollama's refusal of the
optional `think` switch (`THINK_OPTION_UNSUPPORTED_RE`,
`think_option_unsupported`), `LOCAL_THINKING_MIN_NUM_PREDICT` and
`with_local_thinking_budget`, all unchanged. The hosted policy takes the
prediction-budget raiser as an injected `ensure_prediction_budget` callable.

`server.py` keeps the regex, the two predicates, the constant and the local
budget helper as identity-preserving alias imports and keeps
`_apply_cloud_thinking_policy` as a thin delegate injecting
`_ensure_cloud_prediction_budget` at call time. `_ensure_cloud_prediction_budget`
itself stays in `server.py`: `tests/test_cloud_thinking_budget.py` pins that
delegate's module, and the thinking-capability caches beside it are process
state.

## Evidence

- `tests/test_thinking_controls_boundary.py` verifies the alias identities and the pinned server delegate, the narrow refusal recognizer, the hosted `think=false` allow-list, the copy-on-write local budget, every hosted per-model branch through an injected budget, and the root delegate's budget seam.
- `python -m pytest -q tests/test_thinking_controls_boundary.py tests/test_cloud_thinking_budget.py tests/test_ensemble_answer.py tests/test_fanout_admission_boundary.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
