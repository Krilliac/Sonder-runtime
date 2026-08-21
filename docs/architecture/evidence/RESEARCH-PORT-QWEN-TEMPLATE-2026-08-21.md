# Research port: Qwen-style chat-template options

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

The Qwen Sharp Chat Templates project documents that reasoning effort is a
template input and must be supplied through nested `chat_template_kwargs` for
clients such as oMLX and llama.cpp. A bare top-level `reasoning_effort` can be
silently ignored by those clients. The project also recommends preserving the
caller system prompt and making thinking mode explicit.

Source: <https://huggingface.co/peculiar-ragdoll/Qwen-Sharp-Chat-Templates/blob/main/README.md>

## Implemented slice

Sonder now normalizes the internal `ModelRequest.options` boundary before an
OpenAI-compatible request is sent:

- `reasoning_effort` is moved into `chat_template_kwargs`.
- `none` and `disabled` become the explicit `off` value.
- invalid values and conflicting top-level/nested values fail closed.
- unrelated nested template options are preserved.
- no system prompt is rewritten and no provider-specific Jinja template is
  copied into Sonder.

Evidence: `tests/test_chat_template_policy.py` and the focused gateway tests.
