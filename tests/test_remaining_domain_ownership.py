from __future__ import annotations

import pytest

from sonder_runtime.application.persistence.domain_ownership import (
    DomainDatabaseOwnership,
    DomainOwnershipError,
    DomainStoreBinding,
    DomainStoreRegistry,
    MigrationOutboxIntegration,
    TransactionBoundary,
    default_domain_ownership,
    default_domain_store_registry,
    ownership_for,
)


def test_inventory_gives_each_domain_one_database_repository_and_migration_store():
    inventory = default_domain_ownership()
    assert {"memory", "automation", "operations", "selfmod", "training", "updates"} <= set(inventory)
    assert len(inventory) == len(set(inventory))
    for domain, declaration in inventory.items():
        assert declaration.domain == domain
        assert declaration.database.endswith(".db")
        assert declaration.repository.startswith("sqlite.")
        assert declaration.transaction.database == declaration.database
        assert declaration.integration.migration_store == declaration.migration_store
        assert declaration.integration.migration_ledger == "schema_migrations"
        assert declaration.integration.outbox_table == "outbox_events"


def test_automation_explicitly_absorbs_legacy_fleet_and_autopilot_stores():
    automation = ownership_for("automation")
    assert automation.database == "automation.db"
    assert automation.legacy_databases == ("autopilot.db", "fleet.db")
    assert ownership_for("operations").source_of_truth is True


def test_transaction_boundary_forbids_cross_database_atomicity_claims():
    with pytest.raises(DomainOwnershipError, match="cross_database"):
        TransactionBoundary(
            database="memory.db",
            atomic_operations=("memory write", "operations projection"),
            cross_database=True,
        )


def test_ownership_rejects_mismatched_transaction_or_migration_metadata():
    with pytest.raises(DomainOwnershipError, match="transaction database"):
        DomainDatabaseOwnership(
            domain="memory", database="memory.db", repository="sqlite.memory",
            migration_store="memory", owned_tables=("sessions",),
            transaction=TransactionBoundary("operations.db", ("write",)),
            integration=MigrationOutboxIntegration("memory"),
        )
    with pytest.raises(DomainOwnershipError, match="migration store"):
        DomainDatabaseOwnership(
            domain="memory", database="memory.db", repository="sqlite.memory",
            migration_store="memory", owned_tables=("sessions",),
            transaction=TransactionBoundary("memory.db", ("write",)),
            integration=MigrationOutboxIntegration("operations"),
        )


def test_state_and_outbox_are_declared_atomic_and_migration_scope_is_local():
    for declaration in default_domain_ownership().values():
        assert declaration.integration.state_and_outbox_atomic
        assert declaration.integration.migration_scope == "one migration per database transaction"
        assert declaration.transaction.cross_database is False
        assert declaration.transaction.coordination == "application workflow or durable event"


def test_unknown_domains_and_non_atomic_outboxes_fail_closed():
    with pytest.raises(DomainOwnershipError, match="unknown persistent domain"):
        ownership_for("unknown")
    with pytest.raises(DomainOwnershipError, match="atomic state and outbox"):
        MigrationOutboxIntegration("memory", state_and_outbox_atomic=False)


def test_registry_proves_one_to_one_domain_and_path_owner_mapping(tmp_path):
    registry = default_domain_store_registry(tmp_path)
    assert set(registry.domain_to_path) == set(default_domain_ownership())
    assert len(registry.path_to_domain) == len(registry.domain_to_path)
    for domain, path in registry.domain_to_path.items():
        assert registry.owner_for(path) == domain
        assert registry.binding_for(domain).path == path
    registry.validate_ownership(default_domain_ownership())


def test_registry_rejects_shared_path_even_through_relative_alias(tmp_path):
    with pytest.raises(DomainOwnershipError, match="shared by domains"):
        DomainStoreRegistry.from_paths(
            {"memory": "memory.db", "automation": "nested/../memory.db"},
            root=tmp_path,
        )


def test_registry_rejects_ambiguous_sqlite_targets_and_unknown_paths(tmp_path):
    registry = DomainStoreRegistry((DomainStoreBinding("memory", tmp_path / "memory.db", "sqlite.memory"),))
    with pytest.raises(DomainOwnershipError, match="filesystem SQLite path"):
        DomainStoreRegistry.from_paths({"memory": ":memory:"}, root=tmp_path)
    with pytest.raises(DomainOwnershipError, match="SQLite suffix"):
        DomainStoreRegistry.from_paths({"memory": "memory.data"}, root=tmp_path)
    with pytest.raises(DomainOwnershipError, match="no registered owner"):
        registry.owner_for(tmp_path / "other.db")


def test_registry_rejects_declaration_mapping_drift(tmp_path):
    registry = default_domain_store_registry(tmp_path)
    altered = dict(default_domain_ownership())
    altered["memory"] = DomainDatabaseOwnership(
        domain="memory", database="memory.db", repository="sqlite.other",
        migration_store="memory", owned_tables=("sessions",),
        transaction=TransactionBoundary("memory.db", ("write",)),
        integration=MigrationOutboxIntegration("memory"),
    )
    with pytest.raises(DomainOwnershipError, match="repository owner mismatch"):
        registry.validate_ownership(altered)
