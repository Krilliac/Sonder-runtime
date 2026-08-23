# Defensive boundary audit — 2026-08-22

This pass covered tool authorization, filesystem/archive containment, secret
redaction, prompt provenance, remote Ollama transport, hosted account/session
isolation, replay/idempotency, and fail-closed behavior. It made narrow fixes;
it did not broaden permissions or replace the orchestration design.

## Enforced contracts

- Tool calls cannot declare fewer effects than their registry descriptor,
  cannot exceed the request scope, and must match an allow rule for every
  effect. Multi-effect sets are evaluated deterministically.
- Remote Ollama workers accept only credential-free HTTP(S) origins with an
  explicit port and no path, query, or fragment. Non-loopback workers require
  explicit consent and verified HTTPS. Typed TOML worker configuration remains
  authoritative when the legacy runtime is imported lazily.
- Model POSTs are single-attempt at the worker-pool layer. Only explicitly
  idempotent control reads may fail over after a transport error.
- Worker diagnostics expose a stable exception class, TLS verification mode,
  and replay posture; they do not retain free-form provider error text.
- Account identities and passwords are bounded before hashing or persistence.
  Malformed authentication output cannot enter shared session, cache, receipt,
  task, feed, or fanout namespaces through an `unknown` fallback identity.
- Serialized prompt context may replay only the untrusted label produced at
  ingestion. It cannot self-assert user confirmation or independent
  verification by recomputing a public checksum. Context packets and event
  item-digest bindings are revalidated, and provenance metadata is bounded.
- Archive sources are checked by content digest as well as size, timestamps,
  device, and inode before promotion. Same-size mutation with a restored mtime
  therefore rolls back the staged extraction.
- Structured log redaction covers authorization and bearer values, assignments,
  URL userinfo, private keys, cookie headers, and credential-bearing query
  parameters. Worker health never depends on redaction succeeding because it
  stores no free-form error detail.

## Adversarial coverage

Focused tests exercise effect under-declaration, multi-effect partial allows,
scope escalation, malformed worker URLs, ambiguous POST transport failure,
typed-worker authority, malformed hosted identities, oversized credentials,
forged provenance trust and item digests, same-metadata archive mutation, and
cookie/query secret leakage.

## Residual risks

- Remote-worker confidentiality still depends on the operator-controlled TLS
  endpoint, system trust store, DNS, proxy access controls, and worker host.
- A digest detects archive mutation but cannot make a caller-writable source
  immutable. Extraction remains transactional and refuses promotion on any
  detected change.
- Prompt provenance digests provide internal consistency, not third-party
  cryptographic attestation. Elevated trust must come from an in-process,
  independently authorized promotion path rather than serialized content.
