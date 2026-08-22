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
import os
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping


class DomainOwnershipError(ValueError):
    """Raised when a persistence ownership declaration is unsafe or ambiguous."""


def _canonical_sqlite_path(path: str | Path, *, root: str | Path | None = None) -> Path:
    """Return a stable path identity without opening, creating, or modifying it.

    ``resolve(strict=False)`` collapses ``..`` and existing symlink aliases, while
    ``normcase`` makes the identity case-insensitive on Windows.  SQLite URI
    names and in-memory databases are intentionally not accepted: they do not
    provide a filesystem identity that can be assigned to exactly one domain.
    """
    if isinstance(path, Path):
        raw = str(path)
    elif isinstance(path, str):
        raw = path.strip()
    else:
        raise DomainOwnershipError("database path must be a path string")
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise DomainOwnershipError("database path must be a filesystem SQLite path")
    candidate = Path(raw)
    if root is not None and not candidate.is_absolute():
        candidate = Path(root) / candidate
    try:
        resolved = candidate.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise DomainOwnershipError(f"database path cannot be resolved: {raw}") from exc
    if resolved.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise DomainOwnershipError("database path must have a SQLite suffix")
    return Path(os.path.normcase(str(resolved)))


@dataclass(frozen=True)
class DomainStoreBinding:
    """One concrete filesystem SQLite store and its sole domain owner."""

    domain: str
    path: str | Path
    repository: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _required_text(self.domain, "domain"))
        object.__setattr__(self, "repository", _required_text(self.repository, "repository"))
        if not isinstance(self.path, (str, Path)):
            raise DomainOwnershipError("store binding path must be a path string")


class DomainStoreRegistry:
    """Validated, read-only mapping between domains and concrete SQLite paths.

    The registry is deliberately metadata-only.  It never opens a database and
    never creates directories.  Construction is the validation gate: duplicate
    domains, duplicate canonical paths, and unknown/blank owners fail closed.
    """

    def __init__(
        self,
        bindings: Iterable[DomainStoreBinding],
        *,
        root: str | Path | None = None,
    ) -> None:
        by_domain: dict[str, DomainStoreBinding] = {}
        by_path: dict[Path, str] = {}
        for binding in tuple(bindings):
            if not isinstance(binding, DomainStoreBinding):
                raise DomainOwnershipError("registry entries must be DomainStoreBinding values")
            if binding.domain in by_domain:
                raise DomainOwnershipError(f"duplicate domain owner: {binding.domain}")
            canonical = _canonical_sqlite_path(binding.path, root=root)
            if canonical in by_path:
                owner = by_path[canonical]
                raise DomainOwnershipError(
                    f"SQLite path is shared by domains {owner!r} and {binding.domain!r}: {canonical}"
                )
            normalized = DomainStoreBinding(binding.domain, canonical, binding.repository)
            by_domain[normalized.domain] = normalized
            by_path[canonical] = normalized.domain
        if not by_domain:
            raise DomainOwnershipError("domain store registry must not be empty")
        self._by_domain = MappingProxyType(by_domain)
        self._by_path = MappingProxyType(by_path)

    @classmethod
    def from_paths(
        cls,
        paths: Mapping[str, str | Path],
        *,
        repositories: Mapping[str, str] | None = None,
        root: str | Path | None = None,
    ) -> "DomainStoreRegistry":
        repo_map = repositories or {}
        return cls(
            (DomainStoreBinding(domain, path, repo_map.get(domain, f"sqlite.{domain}"))
             for domain, path in paths.items()),
            root=root,
        )

    def binding_for(self, domain: str) -> DomainStoreBinding:
        name = _required_text(domain, "domain")
        try:
            return self._by_domain[name]
        except KeyError as exc:
            raise DomainOwnershipError(f"unknown persistent domain: {name}") from exc

    def owner_for(self, path: str | Path, *, root: str | Path | None = None) -> str:
        canonical = _canonical_sqlite_path(path, root=root)
        try:
            return self._by_path[canonical]
        except KeyError as exc:
            raise DomainOwnershipError(f"SQLite path has no registered owner: {canonical}") from exc

    @property
    def domain_to_path(self) -> Mapping[str, Path]:
        return MappingProxyType({domain: binding.path for domain, binding in self._by_domain.items()})

    @property
    def path_to_domain(self) -> Mapping[Path, str]:
        return self._by_path

    def validate_ownership(
        self, declarations: Mapping[str, "DomainDatabaseOwnership"]
    ) -> None:
        """Prove every registered store agrees with its domain declaration."""
        if set(declarations) != set(self._by_domain):
            raise DomainOwnershipError("registry and ownership declarations have different domains")
        for domain, declaration in declarations.items():
            binding = self.binding_for(domain)
            if binding.repository != declaration.repository:
                raise DomainOwnershipError(f"repository owner mismatch for domain: {domain}")
            if Path(declaration.database).name != binding.path.name:
                raise DomainOwnershipError(f"database filename mismatch for domain: {domain}")


def default_domain_store_registry(
    root: str | Path,
) -> DomainStoreRegistry:
    """Build the canonical six-domain registry below *root* without I/O."""
    return DomainStoreRegistry.from_paths(
        {domain: declaration.database for domain, declaration in default_domain_ownership().items()},
        repositories={domain: declaration.repository for domain, declaration in default_domain_ownership().items()},
        root=root,
    )


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
    "DomainStoreBinding",
    "DomainStoreRegistry",
    "MigrationOutboxIntegration",
    "TransactionBoundary",
    "default_domain_ownership",
    "default_domain_store_registry",
    "ownership_for",
]
