# ADR-001: Modular monolith, no microservice split

**Status:** accepted

## Context

Sonder is a single-user, single-host orchestration runtime with local
SQLite state and a separately managed Ollama process. The maintainability
problem is coupling inside a flat module namespace, not deployment
topology.

## Decision

Restructure into explicit domain / application / adapters / platform /
bootstrap packages inside one deployable process. No network
distribution, no message broker, no service mesh.

## Consequences

Boundaries are enforced by imports and CI (`scripts/check_architecture.py`),
not by network contracts. Failure modes stay in-process; operational
surface stays one systemd unit plus Ollama.
