# ADR-003: SQLite stores remain separated by domain

**Status:** accepted

## Context

memory.db, autopilot.db, fleet.db, operations.db, and updates.db have
different owners, write patterns, and recovery semantics.

## Decision

Keep separate database files, each with exactly one repository owner and
its own `schema_migrations` ledger. No distributed transactions across
files; cross-store operations use explicit saga/transition records
(e.g. model promotion, update plans).

## Consequences

Backups snapshot all stores consistently via the SQLite online backup
API. A corrupted store can be restored alone. `sonder_migrations` treats
each store independently; future schema in any store blocks startup.
