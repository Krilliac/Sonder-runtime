# CTX-009 prefix caching evidence — 2026-09-23

## Requirement

CTX-009 requires stable instructions, schemas, project rules, and skill
catalogs to be placed in reusable prefixes with versioned cache keys and
hit/write metrics.

## Implemented contract evidence

- `sonder_runtime/application/context_manifests.py` defines `PrefixIdentity`,
  versioned `PrefixManifest`, and bounded `PrefixManifestCache`.
- Stable records are ordered by section, item identity, and content digest.
  Dynamic memory and retrieval are accepted by the integration facade but are
  excluded from stable prefix identity.
- The focused canary explicitly covers project rules, tool schemas, a skill
  catalog, project policy, versioned identity, and hit/write metrics.
- Ollama telemetry parses provider-reported prompt-cache counts without
  retaining prompt or response contents.

## Verification

The implementation and focused tests were run from source baseline
`4db5a2dd66ad616a1e2b992e48a694b55bbd49a6`:

```text
python -m pytest -q tests/test_wp4_ctx004_006_009_010.py tests/test_inference_telemetry.py tests/production/test_ollama_gateway.py
52 passed
python scripts/check_requirement_evidence.py
passed
python scripts/check_doc_links.py
passed
python scripts/check_evidence_documents.py
passed
python scripts/check_architecture.py
passed
```

The live request builder now carries the immutable `PrefixManifest`, the
per-request in-process manifest-cache decision (`PrefixCacheObservation`), and
the `ReplayManifest` through `ModelRequest` into the selected provider
boundary. This decision does not measure provider KV-cache reuse. Session
capture persists bounded section identities and content digests without
copying section text, and replay reconstructs that evidence. Focused provider
boundary and session-capture tests exercise this path; the provider double
supplies synthetic prompt-cache counts to verify telemetry shape only.

On the current implementation branch (`01ed36043a47d8adaa28d0f322eb0e880da55696`
plus this change), the focused boundary and durability checks reported:

```text
python -m pytest -q tests/test_live_agent_context.py tests/test_wp4_ctx004_006_009_010.py tests/test_session_split_capture.py tests/test_session_replay.py tests/test_remaining_session_durable_replay.py
48 passed
python -m compileall -q <affected modules>
passed
```

A bounded loopback probe used Ollama `0.34.2`, endpoint
`http://127.0.0.1:11434/api/generate`, model `sonder:latest`, and the public
fixed prompt `Reply with the single word cache.`. Response text was omitted.
The two identical `stream=false`, temperature-zero requests reported:

| Request | prompt_eval_count | prompt_eval_cached_count |
|---:|---:|---:|
| 1 | 212 | 0 |
| 2 | 212 | 211 |

This demonstrates provider-reported raw Ollama prefix reuse.

## Limitations

The runtime graph now instantiates `LiveAgentContextProducer` and passes it to
the production `AgentLaneService`. The lane request builder discovers the
scoped project rules and skill catalog, resolves a provider-owned route, and
passes the resulting request through `ProviderDispatchGateway`; the dispatch
wrapper verifies that the route was issued by the selected provider before
generation. `tests/test_live_agent_context.py::test_live_prefix_request_crosses_provider_dispatch_with_sealed_route`
covers this boundary and verifies that the generated request contains both
scoped sections, exact replay/prefix evidence, and the in-process manifest
cache decision while the provider double returns synthetic prompt-cache counts.

The test uses a deterministic provider double. The raw Ollama probe therefore
does not prove that a real production Ollama request consumes this cache or
that provider-reported KV reuse is present for these exact stable sections.
Other providers and cross-process cache coordination remain unverified. CTX-
009 remains `implemented_unverified` until a live provider invocation and
end-to-end telemetry evidence cover the full requirement.
