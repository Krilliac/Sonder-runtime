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

The current runtime graph instantiates `ContextPlanningFacade`, but the
repository audit found no production caller of
`ContextPlanningFacade.assemble` or `RuntimeContextPlanningAdapter.assemble`;
those calls are confined to tests. The raw Ollama probe therefore does not
prove that a live Sonder request consumes this cache or that Sonder stable
sections reach Ollama. Other providers, cross-process cache coordination, and
production traffic remain unverified. CTX-009 remains
`implemented_unverified` until a live invocation path and end-to-end evidence
cover the full requirement.
