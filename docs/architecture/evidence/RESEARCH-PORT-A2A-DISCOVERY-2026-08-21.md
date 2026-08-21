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

References: <https://github.com/a2aproject/A2A/blob/main/docs/specification.md>
and <https://github.com/a2aproject/A2A/blob/main/docs/topics/agent-discovery.md>.
