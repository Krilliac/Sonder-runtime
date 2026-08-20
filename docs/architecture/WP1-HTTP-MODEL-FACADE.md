# WP1 HTTP model-request facade

This slice extracts the bounded model-request route family from the HTTP
adapter without changing the server-backed control or administration routes.

## Scope

`sonder_runtime/interfaces/http/facades/model_request.py` owns:

- classification of `/v1/chat/completions` and `/v1/responses`;
- bounded message, field, and option admission;
- OpenAI-compatible normalization through the existing
  `OpenAICompatibility` contract;
- translation to the existing `ModelRequest` / `ModelGateway` contract;
- provider-neutral response rendering; and
- injected policy and event hooks.

`serve.py` remains responsible for authentication, CORS, request framing,
model catalog/selection policy, lifecycle admission, history/session policy,
stream writes, and all server-backed slash, work, fanout, login, registration,
and admin routes.  For the Responses envelope, the adapter translates the
normalized text messages into the established model execution path and emits
the Responses-shaped result; Responses streaming is explicitly rejected until
an equivalent bounded event-stream contract exists.

The facade has no import of the legacy `server` root and no `importlib`
bypass.  No formal checklist checkbox is changed by this slice.

Evidence: `tests/test_wp1_http_model_facade.py` plus the architecture,
requirement-evidence, compile, and diff gates.
