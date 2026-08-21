# Research port: export-neutral agent trace projection — 2026-08-21

## Decision

Sonder now exposes a bounded `sonder.trace-span.v1` projection from the local
observability sink. The projection is shaped so a future host adapter can map
it to OpenTelemetry GenAI agent, workflow, plan, and tool spans, but it does
not import an OTel SDK, persist telemetry, contact a collector, or claim
delivery.

## Safety and ownership

- Input is limited to `LocalObservabilitySink.recent_events()`, which has
  already applied field redaction and size limits.
- Raw `fields` are ignored by the projection even if a caller supplies them.
- Correlation values are hashed into bounded trace/span identifiers.
- The projection emits only low-cardinality attributes, sequence, bounded
  monotonic observation time, duration, and status.
- `operations.db` and `activity_tracker` remain the authoritative durable and
  response-level evidence stores.

## Evidence

| Claim | Evidence |
|---|---|
| Stable bounded projection and malformed-input rejection | `sonder_runtime/application/observability/trace_projection.py`; `tests/test_trace_projection.py` |
| Sink integration consumes only sanitized retained events | `sonder_runtime/adapters/local_observability.py`; `tests/test_local_observability.py` |
| No exporter or network path was introduced | `docs/architecture/adr/ADR-009-local-observability.md`; `tests/production/test_architecture.py` |

Focused verification: `14 passed` for the trace/local-observability tests;
architecture and diff checks pass. This is implementation evidence, not a
claim that a production OpenTelemetry exporter or cross-process trace
propagation is complete.

Reference: OpenTelemetry GenAI agent span conventions:
<https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md>
