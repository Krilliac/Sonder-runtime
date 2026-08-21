"""Provider-neutral production boundary for domain persistence.

The facade owns no connections and performs no migration.  It binds the
application ownership registry to typed repository ports, so callers cannot
silently write an unknown domain or claim a cross-database transaction.
Adapters remain responsible for transaction, migration, and durability truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..artifacts.immutable_manifest import ArtifactManifest, ArtifactManifestBuilder, ArtifactRecord
from .cross_domain import CoordinationResult, CrossDomainCoordinator, CrossDomainWrite
from .domain_ownership import DomainDatabaseOwnership, DomainStoreRegistry, ownership_for
from .outbox_cas import OutboxCASRepository, OutboxEvent, TransactionNeutralRecord


class PersistenceBoundaryError(ValueError):
    """A persistence operation is outside the composed ownership boundary."""


class CrossDatabaseTransactionError(PersistenceBoundaryError):
    """The requested atomic write spans separately owned SQLite stores."""


@dataclass(frozen=True)
class DomainPersistence:
    """One domain's immutable ownership metadata and typed repository port."""

    ownership: DomainDatabaseOwnership
    repository: OutboxCASRepository


class PersistenceFacade:
    """Small provider-neutral boundary for production persistence calls.

    [any thread, adapter-defined safety] Repository methods retain their
    adapter's transaction semantics.  The facade only validates ownership and
    preserves the result; it never opens a database or performs a partial
    multi-store operation.
    """

    def __init__(
        self,
        registry: DomainStoreRegistry,
        repositories: Mapping[str, OutboxCASRepository],
        *,
        coordinator: CrossDomainCoordinator | None = None,
    ) -> None:
        if not isinstance(registry, DomainStoreRegistry):
            raise TypeError("registry must be a DomainStoreRegistry")
        expected = set(registry.domain_to_path)
        supplied = set(repositories)
        if supplied != expected:
            missing = sorted(expected - supplied)
            extra = sorted(supplied - expected)
            raise PersistenceBoundaryError(
                f"repository graph does not match ownership registry; missing={missing}, extra={extra}"
            )
        if any(repository is None for repository in repositories.values()):
            raise PersistenceBoundaryError("repository graph contains a null provider")
        self._registry = registry
        self._domains = {
            domain: DomainPersistence(ownership_for(domain), repositories[domain])
            for domain in sorted(expected)
        }
        # Validate the graph's declarations against the concrete path registry
        # before any caller can perform a write.
        self._registry.validate_ownership({domain: item.ownership for domain, item in self._domains.items()})
        self._coordinator = coordinator

    @property
    def registry(self) -> DomainStoreRegistry:
        return self._registry

    def domain(self, domain: str) -> DomainPersistence:
        try:
            return self._domains[domain]
        except KeyError as exc:
            raise PersistenceBoundaryError(f"unknown persistent domain: {domain!r}") from exc

    def get(self, domain: str, aggregate_id: str) -> TransactionNeutralRecord | None:
        return self.domain(domain).repository.get(aggregate_id)

    def append(
        self,
        domain: str,
        record: TransactionNeutralRecord,
        event: OutboxEvent,
        *,
        expected_revision: int,
    ) -> TransactionNeutralRecord | None:
        """Atomically append one domain record and its matching outbox event."""
        return self.domain(domain).repository.append(
            record, event, expected_revision=expected_revision
        )

    def coordinate(
        self, operation_id: str, writes: tuple[CrossDomainWrite, ...]
    ) -> CoordinationResult:
        """Run only an adapter-supported same-store coordination operation.

        Separate domain-owned SQLite files are never joined by this facade.
        Cross-domain business work must use an application workflow or the
        already-committed outbox; callers get a hard failure before the
        coordinator is invoked.
        """
        if self._coordinator is None:
            raise PersistenceBoundaryError("cross-domain coordinator is not composed")
        if not writes:
            raise PersistenceBoundaryError("at least one coordinated write is required")
        paths = {self.registry.binding_for(write.domain).path for write in writes}
        if len(paths) != 1:
            raise CrossDatabaseTransactionError(
                "cross-database transactions are forbidden; use an application workflow or durable outbox"
            )
        for write in writes:
            self.domain(write.domain)  # reject unknown domains before adapter work
        return self._coordinator.coordinate(operation_id, writes)

    @staticmethod
    def artifact_manifest(
        entries: tuple[ArtifactRecord, ...], *, version: str = "1", metadata: Mapping[str, str] | None = None
    ) -> ArtifactManifest:
        """Create the immutable, deterministic artifact-hash inventory."""
        return ArtifactManifestBuilder(version=version, metadata=metadata).build(entries)


__all__ = [
    "CrossDatabaseTransactionError", "DomainPersistence", "PersistenceBoundaryError",
    "PersistenceFacade",
]
