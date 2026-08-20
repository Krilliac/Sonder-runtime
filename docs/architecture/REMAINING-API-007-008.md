# Remaining API-007/008 — client parity and runtime schema generation

## Scope

This slice closes the isolated contract gap identified by the requirement
audit:

- **API-007:** a provider-neutral reconnect/resume contract for the Flutter
  and other clients, using the same durable stream state as desktop, web, and
  CLI consumers;
- **API-008:** runtime-derived client and SDK schema projections with a
  freshness digest.

The implementation is intentionally under
`sonder_runtime/application/protocol/client_schema.py`. It has no Flutter,
HTTP, provider, socket, or SDK dependency. The Flutter adapter can serialize
`ClientSchema.as_dict()`, send the advertised digest and `ResumeCursor` values,
and render the returned `ResumeBatch` without owning stream semantics.

## Contract

`build_client_schema()` consumes the existing `GeneratedCatalogs` runtime
bundle. Its digest covers the normalized client projection, SDK projection,
source catalog digest, and the snapshot-plus-event stream contract. Any
command, event, tool, or stream-contract change therefore changes the digest.
`check_schema_freshness()` distinguishes current, stale, and malformed client
metadata; malformed or absent digests never pass as current.

`ClientParityContract.reconnect()` applies this order:

1. compare the client digest with the runtime digest;
2. require schema refresh before replay when stale or malformed;
3. resume each known stream from its non-negative watermark with an explicit
   batch bound;
4. return a snapshot-bearing replay when retained history requires one;
5. return explicit `request_snapshot` or `rejected` outcomes for gaps and
   unknown/invalid streams.

This keeps reconnect safe for intermittent mobile connectivity and makes
provider neutrality explicit: no provider name, transport implementation, or
Flutter type enters the application contract.

## Evidence

`tests/test_remaining_client_schema.py` proves:

- deterministic runtime-derived client/SDK catalogs and SHA-256 identity;
- freshness changes for catalog or stream-contract changes;
- current, stale, and invalid digest handling;
- bounded watermark replay with `has_more` continuation;
- schema refresh before replay;
- snapshot-plus-event mobile reconnect parity;
- explicit unknown-stream rejection.

Focused tests and repository architecture/evidence/compile/diff gates were run
for this slice. The formal master-spec checkboxes remain intentionally
unchanged; this document is evidence, not formal checklist credit.
