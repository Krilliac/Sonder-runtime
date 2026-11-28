# ADR-002: Ollama remains the external inference service

**Status:** accepted

## Context

Ollama owns model storage, loading, and token inference. Embedding it
would couple Sonder's release cadence to inference engine internals.

## Decision

Ollama stays a separate loopback service (superseded for local inference by
Sonder Inference under RET-05). Sonder reaches inference only through
the ModelGateway port; remote inference endpoints require the unified
consent gate (`SONDER_ALLOW_REMOTE_INFERENCE` / `[inference].allow_remote`;
`SONDER_ALLOW_REMOTE_OLLAMA` is retained as a deprecated alias with a warning).

## Consequences

Readiness treats Ollama as a required dependency (degraded/unready on
outage, never false success). Model acquisition remains an explicit,
separately reported operation — the update manager never redistributes
weights (SPEC-4).
