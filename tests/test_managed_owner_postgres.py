"""Actual selected namespace admission against an explicitly disposable PG pair."""

from dataclasses import replace
import uuid

import pytest

from tests.test_postgres_child_storage_integration import storage_config
from sonder_runtime.adapters.persistence.postgres_binding import PostgresPrivateBinding
from sonder_runtime.adapters.persistence.postgres_child_migration import (
    PostgresChildMigrationStore,
)
from sonder_runtime.adapters.persistence.postgres_continuation import (
    PostgreSQLDurableContinuationRepository,
)
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)


def binding(config):
    return PostgresPrivateBinding(config.binding_file, writable_roots=lambda: ())


def test_exact_selected_policy_rejects_before_owner_claim(storage_config, tmp_path):
    store = PostgresChildMigrationStore(storage_config, binding(storage_config))
    from sonder_runtime.bootstrap.child_migration_host import (
        DisposableChildMigrationHost,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        stage_snapshot,
    )
    from sonder_runtime.platform.config import SonderConfig
    from contextlib import closing

    with closing(
        DisposableChildMigrationHost(tmp_path / "host", writable_roots=lambda: ())
    ) as host:
        host.start(SonderConfig())
        source = host.selected_store
        host.quiesce()
        with ChildMigrationBundle(
            tmp_path / "bundle", writable_roots=lambda: ()
        ) as bundle:
            export_snapshot(source, bundle, target_identity=store.identity)
            stage_snapshot(bundle, store)
            host.activate(bundle, source, store)
    identity = store.identity
    store = PostgresChildMigrationStore(
        storage_config, binding(storage_config), expected_storage_identity=identity
    )

    def observed(connection):
        return connection.execute(
            "SELECT owner_id,incarnation,clean FROM sonder_child.owner WHERE id=1"
        ).fetchone()

    original = store._transport.run(observed)
    store.close()
    for config, expected in (
        (storage_config, "0" * 64),
        (replace(storage_config, owner_id="different-owner"), identity),
        (replace(storage_config, durability="primary", required_standby=""), identity),
    ):
        private = binding(config)
        try:
            with pytest.raises(ContinuationStorageFailure):
                PostgreSQLDurableContinuationRepository(
                    config, private, expected_storage_identity=expected
                )
        finally:
            private.close()
        current = PostgresChildMigrationStore(
            storage_config, binding(storage_config), expected_storage_identity=identity
        )
        try:
            assert current._transport.run(observed) == original
        finally:
            current.close()
    repository = PostgreSQLDurableContinuationRepository(
        storage_config, binding(storage_config), expected_storage_identity=identity
    )
    assert repository.close(runners_stopped=True, timeout=5)
    import psycopg
    from sonder_runtime.application.subagents.child_migration import MigrationRefused

    private = binding(storage_config)
    try:
        with psycopg.connect(**private.connection_kwargs(storage_config)) as connection:
            settled_owner = observed(connection)
            saved = connection.execute(
                "SELECT * FROM sonder_child.migration WHERE id=1"
            ).fetchone()
            connection.commit()
            for change, restore in (
                (
                    "ALTER TABLE sonder_child.migration RENAME TO missing_migration",
                    "ALTER TABLE sonder_child.missing_migration RENAME TO migration",
                ),
                ("DELETE FROM sonder_child.migration WHERE id=1", None),
                (
                    "UPDATE sonder_child.migration SET phase='VERIFIED' WHERE id=1",
                    "UPDATE sonder_child.migration SET phase='ACTIVE' WHERE id=1",
                ),
            ):
                connection.execute(change)
                connection.commit()
                try:
                    for repository_type in (
                        PostgreSQLDurableContinuationRepository,
                        PostgresChildMigrationStore,
                    ):
                        candidate = binding(storage_config)
                        try:
                            with pytest.raises(
                                (ContinuationStorageFailure, MigrationRefused)
                            ):
                                repository_type(
                                    storage_config,
                                    candidate,
                                    expected_storage_identity=identity,
                                )
                        finally:
                            candidate.close()
                    assert observed(connection) == settled_owner
                    connection.commit()
                finally:
                    if restore is None:
                        connection.execute(
                            "INSERT INTO sonder_child.migration VALUES(%s,%s,%s,%s,%s)",
                            saved,
                        )
                    else:
                        connection.execute(restore)
                    connection.commit()
    finally:
        private.close()
    current = PostgresChildMigrationStore(
        storage_config, binding(storage_config), expected_storage_identity=identity
    )
    try:
        assert current._transport.run(observed)[0] == storage_config.owner_id
        assert current._transport.run(observed)[2] is True
        settled = current._transport.run(observed)

        def rotate(connection):
            connection.execute(
                "UPDATE sonder_child.migration_namespace SET identity=%s WHERE id=1",
                (uuid.uuid4().hex,),
            )

        current._transport.run(rotate)
        from sonder_runtime.application.subagents.child_migration import (
            MigrationRefused,
        )

        with pytest.raises((MigrationRefused, ContinuationStorageFailure)):
            current.read_snapshot(lambda snapshot: snapshot.metadata())
    finally:
        current.close()
    private = binding(storage_config)
    try:
        with pytest.raises(ContinuationStorageFailure):
            PostgreSQLDurableContinuationRepository(
                storage_config, private, expected_storage_identity=identity
            )
    finally:
        private.close()
    # Exact database namespace change is not silently adopted, even with the
    # same endpoint and otherwise unchanged policy/clean owner.
    changed = PostgresChildMigrationStore(storage_config, binding(storage_config))
    try:
        assert changed.identity != identity
        assert changed._transport.run(observed) == settled
    finally:
        changed.close()


def test_contained_application_selected_pg_and_reverse(storage_config, tmp_path):
    import urllib.request
    from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        stage_snapshot,
    )
    from tests.test_managed_runtime_owner import port

    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    target = None
    try:
        reference = owner.register_configuration(port=port())
        owner.execute(owner.prepare("initial", "select", {"config": reference}))
        assert (
            owner.execute(owner.prepare("initial-launch", "launch", {}))["state"]
            == "RUNNING"
        )
        assert (
            owner.execute(owner.prepare("initial-stop", "stop", {}))["state"]
            == "STOPPED_CLEAN"
        )
        source = owner.selected_store
        target = PostgresChildMigrationStore(storage_config, binding(storage_config))
        with ChildMigrationBundle(
            tmp_path / "outbound", writable_roots=lambda: ()
        ) as bundle:
            export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            pg_reference = owner.register_configuration(port=port(), target=target)
            operation = owner.prepare_activation("to-pg", bundle, target, pg_reference)
            original_namespace = target._namespace_identity

            def namespace(value):
                def write(connection):
                    connection.execute(
                        "UPDATE sonder_child.migration_namespace SET identity=%s WHERE id=1",
                        (value,),
                    )
                    connection.commit()

                target._transport.run(write)

            namespace(uuid.uuid4().hex)
            try:
                from sonder_runtime.application.subagents.child_migration import (
                    MigrationRefused,
                )

                with pytest.raises((MigrationRefused, ContinuationStorageFailure)):
                    owner.execute(operation)
                assert owner._selection is source
                assert not bundle.has_phase("COMPLETE", bundle.manifest())
                assert not bundle.has_phase("SOURCE_RETIRED", bundle.manifest())
            finally:
                namespace(original_namespace)
            assert owner.execute(operation)["state"] == "STOPPED_CLEAN"
            assert (
                owner.execute(owner.prepare("pg-launch", "launch", {}))["state"]
                == "RUNNING"
            )
            with urllib.request.urlopen(
                f"http://127.0.0.1:{owner._config(pg_reference)['port']}/live",
                timeout=3,
            ) as response:
                assert response.status == 200
            stopped = owner.execute(owner.prepare("pg-stop", "stop", {}))
            assert stopped["state"] == "STOPPED_CLEAN", (
                stopped,
                [p.read_text() for p in owner.path.glob("runtime-pg-launch.json")],
            )
        current = owner.selected_store
        current.close()
        # Host-owned disposable fixture writes canonical state after PG runtime
        # completion; its exact repository closes before fresh export admission.
        from sonder_runtime.application.ports.continuation_records import (
            DurableChildSession,
            ChildSessionLineage,
        )
        from sonder_runtime.application.ports.subagents import (
            SubagentRequest,
            SubagentBudget,
        )

        root_id = "fresh-pg-root-" + uuid.uuid4().hex
        record = DurableChildSession(
            SubagentRequest(
                root_id,
                "local provider root",
                SubagentBudget(max_steps=1),
                root_id,
                (("provider_root", "true"),),
            ),
            ChildSessionLineage(root_id),
        )
        writer = PostgreSQLDurableContinuationRepository(
            storage_config,
            binding(storage_config),
            expected_storage_identity=current.identity,
        )
        try:
            writer.create(record)
        finally:
            assert writer.close(runners_stopped=True, timeout=5)
        current = owner.selected_store
        fresh_rows = current.read_snapshot(
            lambda snapshot: tuple(snapshot.records("children"))
        )
        assert any(row["key"] == root_id for row in fresh_rows)
        reverse = SQLiteChildMigrationStore(owner.path / "reverse.sqlite")
        with ChildMigrationBundle(
            tmp_path / "reverse-bundle", writable_roots=lambda: ()
        ) as bundle:
            export_snapshot(current, bundle, target_identity=reverse.identity)
            stage_snapshot(bundle, reverse)
            assert root_id in reverse.read_snapshot(
                lambda snapshot: tuple(
                    row["key"] for row in snapshot.records("children")
                )
            )
            reference = owner.register_configuration(port=port(), target=reverse)
            operation = owner.prepare_activation(
                "to-sqlite", bundle, reverse, reference
            )
            original_retire = current.retire

            def lost_retirement_response(manifest, guard):
                original_retire(manifest, guard)
                raise RuntimeError("injected lost retirement response")

            current.retire = lost_retirement_response
            with pytest.raises(RuntimeError, match="lost retirement"):
                owner.execute(operation)
            assert not bundle.has_phase("SOURCE_RETIRED", bundle.manifest())
            assert not bundle.has_phase("COMPLETE", bundle.manifest())
            current.retire = original_retire
            assert owner.execute(operation)["state"] == "STOPPED_CLEAN"
            assert (
                owner.execute(owner.prepare("sqlite-launch", "launch", {}))["state"]
                == "RUNNING"
            )
            assert (
                owner.execute(owner.prepare("sqlite-stop", "stop", {}))["state"]
                == "STOPPED_CLEAN"
            )
            assert (
                owner.selected_store.read_snapshot(
                    lambda snapshot: tuple(snapshot.records("children"))
                )
                == fresh_rows
            )
    finally:
        owner.close()
        assert all(store._closed for store in owner._pg_stores)
        if target is not None:
            target.close()
