"""SQLite composition for the provider-neutral persistence facade."""
from __future__ import annotations

from pathlib import Path

from ....application.persistence.domain_ownership import default_domain_store_registry
from ....application.persistence.facade import PersistenceFacade
from .cas import SQLiteOutboxCASRepository


def build_sqlite_persistence_facade(root: str | Path) -> PersistenceFacade:
    """Compose one domain-scoped CAS/outbox adapter per canonical store."""
    registry = default_domain_store_registry(root)
    repositories = {
        domain: SQLiteOutboxCASRepository(path)
        for domain, path in registry.domain_to_path.items()
    }
    return PersistenceFacade(registry, repositories)


__all__ = ["build_sqlite_persistence_facade"]
