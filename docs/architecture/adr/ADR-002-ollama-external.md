# ADR-002: Ollama remains the external inference service

**Status:** accepted

## Context

Ollama owns model storage, loading, and token inference. Embedding it
would couple Sonder's release cadence to inference engine internals.

## Decision

Ollama stays a separate loopback service. Sonder reaches it only through
the ModelGateway port; remote Ollama endpoints require the independent
consent gate (`SONDER_ALLOW_REMOTE_OLLAMA` / `[ollama].allow_remote`).

## Consequences

Readiness treats Ollama as a required dependency (degraded/unready on
outage, never false success). Model acquisition remains an explicit,
separately reported operation — the update manager never redistributes
weights (SPEC-4).
