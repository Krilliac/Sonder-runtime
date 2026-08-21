# WP1 Two-Hundred-Eighty-Eighth Slice — vision ownership boundary audit

## Boundary

Audited the remaining model-backed `vision_analyze` path before exposing it
through native MCP. The implementation still lives in the legacy server
because it combines guarded raster loading, image digest revalidation,
loopback-only endpoint enforcement, installed-model capability discovery,
context-window policy, and a multimodal Ollama payload. The packaged
`ModelGateway` port currently models text generation and embeddings only; it
has no image-bearing request contract. Native MCP therefore does not expose
`vision_analyze` yet, avoiding a second transport path that could bypass the
local-model and prompt-in-image safety rules.

## Evidence

- Existing vision boundary and failure-mode regressions pass: **14 passed**.
- The server path rejects unsupported formats, oversized or changed inputs,
  unconfigured/non-local/non-vision models, and undersized context before the
  model request; the tests cover those guards.
- The packaged native catalog remains at **37** tools and makes no
  model-backed vision claim.

## Remaining migration work

Add a typed multimodal request/response port, a local-only Ollama vision
adapter that reuses the gateway consent/deadline contract, and an application
vision service that owns guarded image loading and provenance. Only then add a
native MCP descriptor and transport test. This remains an actionable blocker,
not a completed parity item.
