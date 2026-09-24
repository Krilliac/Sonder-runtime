# Backend route attestation

`scripts/backend_attest.py` probes one OpenAI-compatible model route for chat,
structured JSON, and cancellation behavior. It stores capability results and
reason codes in the private state directory; provider responses and API keys
are not written to the evidence file.

Preview a local route without a model call or state write:

```powershell
python scripts/backend_attest.py --base-url http://127.0.0.1:8080 --model my-model --dry-run
```

Run the bounded probe by omitting `--dry-run`. A non-loopback endpoint requires
both HTTPS and `--allow-cloud`; the probe prompts are fixed and contain no
project data. Set `SONDER_OPENAI_API_KEY` in the process environment when the
endpoint requires it. Keep the key out of command arguments and logs.

The recorded backend is `openai-compatible`. A route profile must use that
backend label and the same model name to consume this evidence. A failed,
missing, synthetic, or stale record keeps the optional route ineligible.

Identity-bound routing needs an additional host-owned observation of the exact
backend deployment. Supply `--identity-file` when probing a live endpoint. It
must be an ordinary absolute JSON file with exactly these fields:

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

The host must derive these values from the running deployment and re-observe
them at every route admission; a tag, endpoint URL, profile metadata, or model
name alone cannot certify identity. A change to any field, an expired probe,
or a failed/unknown required capability refuses the route. For example, a
host can opt in with `ModelGatewayFacade(..., recent_evidence=store,
identity_for=current_route_identity)` after publishing READY provider health.
`generate`, `generate_for_role`, `embed` and `route` then enforce the evidence
before an inference effect. An in-flight deployment change also refuses the
result, even when the new deployment has its own passing probe. The public
`facade.gateway` reference remains gated in identity-bound mode. Default
gateway construction remains unchanged.
The canonical `build_runtime(..., route_evidence=store,
route_identity_for=current_route_identity, route_bindings=bindings,
route_health=health)` uses that facade as `Runtime.model_gateway`, so normal
runtime callers also pass through the gate. Opt-in composition requires all
roles to bind the same configured concrete transport; a mixed provider tier
dispatcher has no single provider whose conformance would cover every tier.
Keep `current_route_identity`
host-owned and re-read the running model digest, template, backend and hardware
on each invocation; a cached file of old metadata cannot detect deployment
changes. A synthetic probe or unbound identity refuses the call.
Only the chat, JSON-format and pre-cancelled request cases in this script
run through the OpenAI-compatible gateway. Tool protocol, true in-flight
cancellation, long-context, cache, resume and vision capabilities stay unknown
until an actual host-owned live protocol provider supplies those observations.
