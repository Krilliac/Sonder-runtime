# WP8 API-004 — OpenAI compatibility foundation

This slice adds `application.protocol.openai_compatibility`, a dependency-light
translation boundary for the supported text-only subset of OpenAI Chat
Completions and Responses envelopes.

It normalizes both request forms into `CanonicalRequest` and both response
forms into `CanonicalResponse`, with renderers for either public envelope.
Chat roles are limited to `system`, `user`, and `assistant`; Responses input
supports string input and `input_text` parts. Multimodal, tool, and unknown
shapes are rejected rather than silently changed.

Policy and event callbacks are explicit constructor seams. The policy callback
runs before normalization, while the event callback receives bounded metadata
after successful request/response normalization. Authentication, cloud
consent, transport, streaming, and persistence remain owned by the existing
HTTP/gateway layers.

Focused verification: `tests/test_wp8_openai_compatibility.py` covers both
envelopes, usage preservation, hook order, rendering, and rejection paths.
Formal checklist remains intentionally unchanged.
