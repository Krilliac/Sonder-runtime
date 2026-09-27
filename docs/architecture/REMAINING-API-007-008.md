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

## HTTP host

The served runtime exposes the contract over HTTP (see
`docs/wiki/05-http-api-and-lifecycle.md`):

- The application graph composes one deny-by-default
  `ProtocolApplicationFacade` from the served typed tool catalog
  (`bootstrap/app.py`), so the schema digest changes exactly when that
  catalog does.
- `interfaces/http/facades/client_protocol.py` is the hosting interface. It
  supplies the authorization decisions: a request-scoped view may reconnect
  only when the HTTP layer authenticated the request, and only the host may
  open a stream. It owns one stream, `control.<instance>`, whose events are
  `control.snapshot` records of the process-global permission mode.
  `<instance>` is a random id minted each time the host is built (process
  start, or a replaced application graph), and the schema route lists the
  current id under `streams`. The host publishes when it observes the mode
  differ from the last published value: on a mode change or read through
  the API and before every reconnect. A change made inside the server
  process by another path (for example the `permission_mode` tool during a
  served chat) is therefore recorded when a client next looks, not when it
  happened. A change made in another process, such as a separately running
  REPL, is not seen until the server restarts, because `permission_modes`
  reads the persisted mode file once per process. A full stream is folded
  into a snapshot instead of refusing the event.
- `GET /v1/client/schema` returns `encode_client_schema()`;
  `POST /v1/client/reconnect` runs `decode_reconnect_request()`, the
  facade's authorized `reconnect()`, and `encode_reconnect_response()`.
  Both require an authorized caller; a malformed body is a 400.

Limits: the stream is in memory and single-process, and its sequence starts
again at 1 whenever the host is built. A cursor kept from an earlier host
names a stream id that no longer exists, so it is `rejected` as an unknown
stream and the client must refetch the schema, take the new stream id and
resume from watermark 0; it is never resumed silently against the new
stream's numbering. Session, job, and work-run events are not published into protocol
streams, and the Flutter app does not call these routes yet.
`tests/test_client_protocol_http.py` covers the routes end to end.

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
