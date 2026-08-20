from __future__ import annotations

import pytest

from sonder_runtime.application.persistence.domain_ownership import (
    DomainDatabaseOwnership,
    DomainOwnershipError,
    MigrationOutboxIntegration,
    TransactionBoundary,
    default_domain_ownership,
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
