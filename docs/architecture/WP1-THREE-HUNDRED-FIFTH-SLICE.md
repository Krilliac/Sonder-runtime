# WP1 Three-Hundred-Fifth Slice — agent claim review

## Boundary

The claim-review policy for an agent's negative existence claims now lives
in `sonder_runtime/domain/agents/claim_review.py`: the negative-claim grammar
(`NEGATIVE_CLAIM_RE`), the anchor regexes, `CLAIM_REVIEW_TOOLS`,
`task_exact_anchors`, `claim_review_tools`, `claim_review_vocabulary` and
`exact_negative_action`, all unchanged. The three functions that consult the
hosted denial take it as an injected `cloud_tool_policy_error` callable.

`server.py` keeps the six constants and `_agent_task_exact_anchors` as
identity-preserving alias imports, and keeps `_agent_claim_review_tools`,
`_agent_claim_review_vocabulary` and `_agent_exact_negative_action` as thin
delegates that inject `_cloud_agent_tool_policy_error` at call time.
`_cloud_agent_tool_policy_error` deliberately did not move: its literal
`ERROR: HOST POLICY` returns are recorded in the shrink-only error-signal
baseline under their current scope, and relocating them would register as a
new scope.

## Evidence

- `tests/test_agent_claim_review_boundary.py` verifies the seven alias identities, the negative-claim grammar, anchor extraction and de-duplication, the vocabulary derived from an injected hosted policy (and the root delegate against the live one), and the exact-search action's demand, stand-down, hosted and root-delegate cases.
- `python -m pytest -q tests/test_agent_claim_review_boundary.py tests/test_claim_review_hosted_vocabulary.py tests/test_agent_tools.py tests/test_agent_verification_gate.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
