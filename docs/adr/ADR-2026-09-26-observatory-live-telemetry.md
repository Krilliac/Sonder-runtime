# ADR-2026-09-26: Runtime as a content-free Observatory live producer

**Status:** Accepted, pending owner review of the security posture below
**Date:** 2026-09-26
**Context:** Sonder ecosystem integration contract v1, sections 4 to 7 and 9;
[observatory-telemetry.md](../architecture/observatory-telemetry.md)

## Decision

1. Runtime serves its own live telemetry to Observatory over HTTP:
   `GET /.well-known/sonder-telemetry` (discovery),
   `GET /v1/observability/events` (SSE, or NDJSON on request) and
   `GET /v1/sonder/ecosystem` (status). No WebSocket, no relay of another
   producer's events, no outbound connection.
2. The export is content-free by construction. The vocabulary
   (`session.*`, `request.*`, `route.*`, `telemetry.dropped`) carries ids,
   bounded model labels, counts and durations. Prompts, responses,
   summaries, headers, URLs and provider payloads never enter it: the
   `dispatch_provider` observer receives only the provider label, operation,
   model name and usage counts; EventSink events cross only through a
   three-code allowlist with `summary` dropped and `detail` sanitized; and
   every event passes the `RedactingTelemetrySink` before the ring.
3. The routes require administrator authorization, identical to
   `/v1/observability/trace`. Local-open loopback mode therefore allows them,
   as it allows every admin route today.
4. Browser access uses a new route-scoped exact-match allowlist,
   `SONDER_OBSERVATORY_ORIGINS`, that grants only `GET`/`OPTIONS` on the
   three telemetry routes. The global `SONDER_CORS_ORIGINS` keeps no default
   and gains `Accept`, `Cache-Control` and `Last-Event-ID` in its preflight
   answer.
5. The ring is bounded (default 4096, 256..65536), subscribers are capped
   (default 8, 429 beyond), emission never blocks or raises, and a slow
   subscriber loses only its own oldest events.
6. `SONDER_OBSERVATORY_EXPORT=0` removes the routes (404) and composes no
   producer or observer.

## Rationale

- Observatory could previously only replay recordings or read a fixture;
  correlating Runtime turns with Sonder-Inference requests needs both
  producers live on the same host clock.
- Putting the Observatory origin on the global CORS list would, in local-open
  mode, let that browser origin call every admin route. A route-scoped list
  limits the grant to read-only telemetry.
- Reusing the trace route's admin rule keeps one authorization story for
  observability reads instead of inventing a telemetry role.

## Consequences

- New network-visible surfaces exist on the Runtime listener. They are
  loopback by default, admin-gated, and read-only. Remote use needs https (a
  TLS-terminating proxy) and the admin bearer key.
- The admin key is over-privileged for a read-only viewer. A short-lived
  read-only telemetry capability is recorded as an open question in
  observatory-telemetry.md; until it exists, token-bearing remote telemetry
  requires the admin key.
- Provider sends outside an HTTP chat or A2A turn are not exported.
- This changes the security posture described in SECURITY.md. SECURITY.md
  is edited by the provider change in the same integration; the telemetry and
  ecosystem route rows belong there as well, and the owner reviews them with
  this record before release.
