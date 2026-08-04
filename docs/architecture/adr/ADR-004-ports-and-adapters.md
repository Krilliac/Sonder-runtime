# ADR-004: Application ports and adapters

**Status:** accepted

## Context

Model transport, persistence, filesystem, process execution, and events
were reached directly from business logic, making isolation and testing
expensive.

## Decision

Application use cases depend on Protocol ports
(`sonder_runtime/application/ports/`): ModelGateway, repositories +
UnitOfWork, ToolExecutor, EventSink, Clock, ProcessProbe. Adapters
implement them; the strangler migration wraps legacy root modules as
adapters first (`adapters/legacy/services.py`), then moves
implementations.

## Consequences

Ports raise the domain error taxonomy (ADR: errors.py), never driver
exceptions. CI rejects domain/application imports of adapters, sqlite3,
subprocess, network modules, and environment reads.
