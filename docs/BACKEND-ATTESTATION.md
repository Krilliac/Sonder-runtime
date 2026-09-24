# Backend probe diagnostics

`scripts/backend_attest.py` probes one OpenAI-compatible endpoint and stores
reason codes in the private state directory. Its records are **always
synthetic diagnostics**, even when the script calls a live endpoint. They do
not qualify a model for identity-bound capability routing. The evidence file
contains no provider response text or API key.

Preview the request without a model call or state write:

```powershell
python scripts/backend_attest.py --base-url http://127.0.0.1:8080 --model my-model --dry-run
```

Omit `--dry-run` to check nonempty chat output, a small JSON response, and
local pre-cancelled request handling. That cancellation check does not prove
that an in-flight provider effect stopped. For additional diagnostic
JSON-shape checks, provide an absolute host metadata file:

```powershell
python scripts/backend_attest.py --base-url http://127.0.0.1:8080 --model my-model --identity-file C:\path\to\backend-identity.json --protocol-probes
```

The optional file must be ordinary JSON with exactly these fields:

```json
{
  "backend": "openai-compatible", "model": "my-model",
  "model_digest": "<64 lowercase hex digits>", "quantization": "Q4_K_M",
  "backend_version": "<deployed version>",
  "tokenizer_digest": "<64 lowercase hex digits>",
  "template_digest": "<64 lowercase hex digits>",
  "context_tokens": 32768, "hardware": "<host-observed hardware identity>"
}
```

The protocol mode rereads the file around requests and requires the provider
to report the configured model name. Neither check verifies that the declared
model digest, tokenizer, template, context window, quantization or hardware
actually belongs to the responding endpoint. Output therefore reports
`identity_declared: true` and `identity_bound: false`; the stored record
remains `synthetic: true`. Native tool calls, fallback tool execution,
sequential tool calls, continuation and in-flight cancellation are `unknown`
because this mode has no real granted tool dispatch or corresponding host
receipts. Other unprobed capabilities also remain unknown. A zero exit code
means only that attempted diagnostic cases did not fail.

A non-loopback endpoint requires both HTTPS and `--allow-cloud`. The fixed
probe prompts contain no project data. If the endpoint needs an API key, set
`SONDER_OPENAI_API_KEY` in the process environment and keep it out of command
arguments and logs.

For production identity-bound routing, an independent host observer must bind
the full concrete deployment identity to the actual serving endpoint and
measure each required capability through the supported runtime path. Only
that separately established, recent, non-synthetic evidence can be passed to
`ModelGatewayFacade(..., recent_evidence=store,
identity_for=current_route_identity)` with READY provider health, or through
`build_runtime(..., route_evidence=store,
route_identity_for=current_route_identity, route_bindings=bindings,
route_health=health)`. The identity resolver must reobserve the deployment on
each admission. An unknown, stale, failed or synthetic case refuses the
optional route, including any record written by this diagnostic script.
