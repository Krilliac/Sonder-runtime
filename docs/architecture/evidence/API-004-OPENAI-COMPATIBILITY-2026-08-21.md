# API-004 OpenAI compatibility boundary evidence

The production-facing model route facade uses the typed
`OpenAICompatibility.complete_request` seam for `/v1/chat/completions` and
`/v1/responses`. Requests are normalized once, policy is evaluated before the
provider hook, and normalized request/response events remain injectable. The
facade maps the provider-neutral `ModelRequest`/`ModelResponse` contracts back
to the selected OpenAI envelope without importing or resolving the legacy
`server` module.

Focused verification:

```text
python -m pytest tests/test_wp8_openai_compatibility.py tests/test_wp1_http_model_facade.py -q
```

The tests cover text-only Chat Completions and Responses normalization,
response rendering, policy-before-event ordering, operation matching, bounded
HTTP admission, and exactly-once policy invocation through the production
facade.
