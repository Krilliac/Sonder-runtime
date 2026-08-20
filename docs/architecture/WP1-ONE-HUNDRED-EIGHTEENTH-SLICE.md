# WP1 one-hundred-eighteenth slice — inline-thinking policy ownership

## Scope

The pure `_strip_inline_thinking` helper in `server.py` enforced the
fail-closed boundary that prevents leading model reasoning tags from reaching
public output, history, or later prompts. This slice moves its implementation
to `sonder_runtime.domain.thinking_policy.strip_inline_thinking` and leaves
the server with an identity-preserving compatibility alias.

No metrics, `unsafe_lab`, autopilot, process-probe, serve-temperature, or
model-capability files were changed.

## Verification

- `pytest -q tests/test_thinking_policy.py tests/test_server_helpers.py` — 222 passed.
- `python -m compileall -q sonder_runtime/domain/thinking_policy.py server.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.
