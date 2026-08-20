"""Application-owned inventory of per-domain persistence boundaries.

The concrete SQLite adapters remain the only owners of SQL and connections.
This module records the boundary they must implement: one source-of-truth
database, one repository owner, one migration ledger, and one transaction
scope per domain.  Cross-database business transactions are deliberately not
representable as an allowed policy; coordination is through workflows and
durable outbox events.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class DomainOwnershipError(ValueError):
    """Raised when a persistence ownership declaration is unsafe or ambiguous."""


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainOwnershipError(f"{field} must be non-empty")
    return value.strip()


def _tuple_of_text(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise DomainOwnershipError(f"{field} must be a non-empty tuple")
    normalized = tuple(_required_text(value, field) for value in values)
    if len(set(normalized)) != len(normalized):
        raise DomainOwnershipError(f"{field} must not contain duplicates")
    return normalized


@dataclass(frozen=True)
class TransactionBoundary:
    """Atomicity scope for one domain's repository operation."""

    database: str
    atomic_operations: tuple[str, ...]
    cross_database: bool = False
    coordination: str = "application workflow or durable event"

    def __post_init__(self) -> None:
        database = _required_text(self.database, "database")
        operations = _tuple_of_text(self.atomic_operations, "atomic_operations")
        coordination = _required_text(self.coordination, "coordination")
        if self.cross_database:
            raise DomainOwnershipError(
                "cross_database transaction claims are forbidden; "
                "coordinate through an application workflow or durable event"
            )
        object.__setattr__(self, "database", database)
        object.__setattr__(self, "atomic_operations", operations)
        object.__setattr__(self, "coordination", coordination)


@dataclass(frozen=True)
class MigrationOutboxIntegration:
    """Metadata joining a domain's migration ledger and transactional outbox."""

    migration_store: str
    migration_ledger: str = "schema_migrations"
    outbox_table: str = "outbox_events"
    migration_scope: str = "one migration per database transaction"
    state_and_outbox_atomic: bool = True
    dispatch_semantics: str = "at-least-once with idempotent projection"

    def __post_init__(self) -> None:
        for field in ("migration_store", "migration_ledger", "outbox_table",
                      "migration_scope", "dispatch_semantics"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        if not self.state_and_outbox_atomic:
            raise DomainOwnershipError(
                "state-owning domains require an atomic state and outbox write"
            )


@dataclass(frozen=True)
class DomainDatabaseOwnership:
    """Complete ownership declaration for one persistent bounded domain."""

    domain: str
    database: str
    repository: str
    migration_store: str
    owned_tables: tuple[str, ...]
    transaction: TransactionBoundary
    integration: MigrationOutboxIntegration
    source_of_truth: bool = True
    legacy_databases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("domain", "database", "repository", "migration_store"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "owned_tables", _tuple_of_text(self.owned_tables, "owned_tables"))
        object.__setattr__(self, "legacy_databases", tuple(
            _required_text(value, "legacy_databases") for value in self.legacy_databases
        ))
        if not self.source_of_truth:
            raise DomainOwnershipError(
                "a persistent domain declaration must identify its source of truth"
            )
        if self.transaction.database != self.database:
            raise DomainOwnershipError("transaction database must equal owned database")
        if self.integration.migration_store != self.migration_store:
            raise DomainOwnershipError("migration store must equal ownership migration store")


def _ownership(
    domain: str,
    database: str,
    repository: str,
    store: str,
    tables: tuple[str, ...],
    *,
    legacy: tuple[str, ...] = (),
) -> DomainDatabaseOwnership:
    return DomainDatabaseOwnership(
        domain=domain,
        database=database,
        repository=repository,
        migration_store=store,
        owned_tables=tables,
        transaction=TransactionBoundary(
            database=database,
            atomic_operations=("domain state mutation", "outbox event append"),
        ),
        integration=MigrationOutboxIntegration(migration_store=store),
        legacy_databases=legacy,
    )


def default_domain_ownership() -> Mapping[str, DomainDatabaseOwnership]:
    """Return the canonical ownership inventory for existing SQLite adapters.

    ``autopilot.db`` and ``fleet.db`` are retained as legacy inputs; their
    epoch-2 owner is the unified automation adapter.  ``operations.db`` is a
    projection and event-import owner, not a source-of-truth transaction
    participant for another domain.
    """
    entries = (
        _ownership("memory", "memory.db", "sqlite.memory", "memory",
                   ("interactions", "sessions", "memory_records")),
        _ownership("automation", "automation.db", "sqlite.automation", "automation",
                   ("automation_runs", "automation_steps", "task_events", "goals"),
                   legacy=("autopilot.db", "fleet.db")),
        _ownership("operations", "operations.db", "sqlite.operations", "operations",
                   ("operation_events",), legacy=("queued_actions.db",)),
        _ownership("selfmod", "selfmod.db", "sqlite.selfmod", "selfmod",
                   ("selfmod_runs", "selfmod_events")),
        _ownership("training", "training.db", "sqlite.training", "training",
                   ("training_runs", "training_events")),
        _ownership("updates", "updates.db", "sqlite.updates", "updates",
                   ("updates",),),
    )
    return MappingProxyType({entry.domain: entry for entry in entries})


def ownership_for(domain: str) -> DomainDatabaseOwnership:
    """Resolve one canonical domain, rejecting unknown or blank names."""
    name = _required_text(domain, "domain")
    try:
        return default_domain_ownership()[name]
    except KeyError as exc:
        raise DomainOwnershipError(f"unknown persistent domain: {name}") from exc


__all__ = [
    "DomainDatabaseOwnership",
    "DomainOwnershipError",
    "MigrationOutboxIntegration",
    "TransactionBoundary",
    "default_domain_ownership",
    "ownership_for",
]
