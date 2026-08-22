# Research port: A2A discovery and remote-task identity — 2026-08-21

## Decision

Sonder now has a bounded A2A-shaped Agent Card projection and a remote-task
identity record. The card advertises identity, supported interface binding,
capabilities, and registered skills. The task reference binds a remote task to
its context, the exact card digest observed, its state, and a bounded
delegation chain.

## Security boundary

- Card generation is local and read-only; it never fetches or trusts a remote
  card automatically.
- A card is descriptive metadata, not an authorization grant.
- A remote task reference does not grant workspace, filesystem, model, or tool
  access.
- Authentication, URL fetching, remote transport, artifact transfer, and
  capability admission remain host adapters with explicit policy.
- Card digests make a later remote interaction able to detect discovery drift.

## Evidence

- Contract: `sonder_runtime/application/protocol/a2a.py`
- Public protocol export: `sonder_runtime/application/protocol/__init__.py`
- Tests: `tests/test_a2a_protocol_contract.py`
- Existing registration source: `sonder_runtime/application/agent_registry/workbench_review.py`

Focused tests cover card shape/digest, registration projection, task lineage,
URL validation, and unsupported-state rejection. This is not a claim of a live
A2A server, remote transport, authentication, streaming, or artifact
interoperability.

The HTTP boundary now includes an administrator-authenticated discovery
facade at `/.well-known/agent-card.json`. It is enabled only when the host
explicitly configures `SONDER_A2A_BASE_URL`; otherwise the route returns an
explicit unavailable response rather than advertising an endpoint that is not
actually deployed. The facade publishes registrations and card digest only.

Evidence: `sonder_runtime/interfaces/http/facades/a2a.py`,
`sonder_runtime/interfaces/http/serve.py`, and
`tests/test_a2a_http_facade.py`. Live HTTP coverage remains limited to the
existing handler authentication suite; no remote A2A interoperability is
claimed.

The provider-neutral `A2AJsonRpcTransport` now validates the A2A 1.0 JSON-RPC
envelope for `SendMessage`, `GetTask`, `ListTasks`, `CancelTask`, and
`GetExtendedAgentCard`, enforces bounded request/response payloads, preserves
request IDs, and delegates only to an injected application handler. It never
reads the local task store or treats a request as an authorization grant.
This is a transport seam; the handler still owns task persistence, capability
admission, and authentication context.

JSON-RPC contract evidence is in `sonder_runtime/interfaces/a2a/jsonrpc.py`
and `tests/test_a2a_jsonrpc.py`. Live HTTP and cross-implementation remote
interoperability now have an authenticated `/a2a` dispatcher seam in
`sonder_runtime/interfaces/http/facades/a2a_jsonrpc.py`; the route remains
truthfully unavailable until composition has a base URL and application-owned
job/agent services. The default handler exposes bounded `GetTask`,
`ListTasks`, `CancelTask`, and `GetExtendedAgentCard` views over existing
application ports. When the composed application also exposes `ChatService`,
`SendMessage` admits only bounded text-only user messages through a
deterministic idempotent durable job, an explicit local-owner HTTP context,
and the existing typed chat service; completed text responses now carry a
bounded MIME type and SHA-256 artifact receipt in the A2A task projection.
Multimodal, remote, and background delegation are not claimed. Route contract evidence is in
`tests/test_a2a_http_jsonrpc.py`; cross-implementation remote interoperability
remains outside this slice.

References: <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
and <https://github.com/a2aproject/A2A/blob/main/docs/topics/agent-discovery.md>.
