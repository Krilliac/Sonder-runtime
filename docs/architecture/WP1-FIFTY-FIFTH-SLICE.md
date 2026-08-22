# WP1 Fifty-Fifth Slice — Ollama Endpoint Caller Boundary

## Change

`sonder_runtime.adapters.ollama.gateway.OllamaGateway` now reads the active
Ollama endpoint through the canonical packaged `ollama_endpoint` adapter rather
than reading `server.BASE`. The gateway still imports `server` for model-tier
resolution and legacy generation construction, so the root `server` allowance
remains explicit and unchanged.

## Contract preserved

The gateway continues to pass the endpoint through `_enforce_local_endpoint`
before constructing or invoking a model generator. Only the source of the
endpoint value moved; model selection, prompt construction, timeout handling,
consent checks, and error mapping remain unchanged.

## Evidence

- Focused Ollama gateway regression tests pass.
- The focused test sets a loopback `server.BASE` and a remote packaged endpoint,
  proving the migrated caller does not read the legacy value.
- Compilation, architecture, requirement-evidence, and staged/working diff
  checks pass.

## Boundary status

This slice removes one concrete `server` data dependency from a package adapter;
it does not remove the root allowance. The remaining gateway dependency is the
model-routing/generator surface and requires a separate contract extraction.
