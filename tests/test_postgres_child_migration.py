"""Real-store tests; only the disposable PostgreSQL harness enables these."""

from pathlib import Path
from dataclasses import replace

import pytest

from tests.test_child_migration import seed
from tests.test_postgres_child_storage_integration import storage_config
from sonder_runtime.adapters.persistence.postgres_binding import PostgresPrivateBinding
from sonder_runtime.adapters.persistence.postgres_child_migration import (
    PostgresChildMigrationStore,
)
from sonder_runtime.adapters.persistence.child_migration import (
    SQLiteChildMigrationStore,
)
from sonder_runtime.adapters.filesystem.child_migration_bundle import (
    ChildMigrationBundle,
)
from sonder_runtime.application.subagents.child_migration import (
    export_snapshot,
    stage_snapshot,
    verify_snapshot,
)
from sonder_runtime.bootstrap.child_migration_host import DisposableChildMigrationHost
from sonder_runtime.platform.config import SonderConfig
from tests.test_child_migration_host import add_terminal
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)

PAIR_CONTROL = None


def test_real_pair_two_direction_exact_history_and_interrupted_resume(
    tmp_path, storage_config, monkeypatch
):
    binding = PostgresPrivateBinding(
        Path(storage_config.binding_file), writable_roots=lambda: ()
    )
    target = PostgresChildMigrationStore(storage_config, binding)
    host = DisposableChildMigrationHost(tmp_path / "owned", writable_roots=lambda: ())
    reverse_source = None
    try:
        host.start(SonderConfig())
        source = host.selected_store
        seed(source.path, 107, repository=host._repository)
        with host.tracked_connection() as connection:
            connection.execute("UPDATE durable_child_session SET rowid=rowid+1000")
            connection.execute("UPDATE continuation_intent SET position=position+5000")
            connection.execute(
                "UPDATE sqlite_sequence SET seq=25000 WHERE name='continuation_intent'"
            )
        host.quiesce()
        with ChildMigrationBundle(
            tmp_path / "outbound", writable_roots=lambda: ()
        ) as bundle:
            manifest = export_snapshot(source, bundle, target_identity=target.identity)
            from sonder_runtime.application.ports.continuation_mutations import (
                PreparedContinuationMutation,
            )
            from sonder_runtime.application.subagents.child_migration import unbinary

            intents = iter(bundle.records("intents"))
            next(intents)  # provider-root creation
            original_intent = next(intents)
            replay = PreparedContinuationMutation(
                original_intent["kind"],
                original_intent["child_id"],
                original_intent["key"],
                unbinary(original_intent["payload"]),
                original_intent["digest"],
            )
            receipts = iter(bundle.records("receipts"))
            next(receipts)
            original_result = unbinary(next(receipts)["result"])
            target.prepare(manifest)
            first_page = next(bundle.pages("children"))
            target.copy_page(manifest, "children", 0, first_page)
            # Discard the importer and its dedicated session after one committed
            # page. Reopening must retain that exact migration and page receipt.
            assert target.close()
            target = PostgresChildMigrationStore(
                storage_config,
                PostgresPrivateBinding(
                    Path(storage_config.binding_file), writable_roots=lambda: ()
                ),
            )
            stage_snapshot(bundle, target)
            assert verify_snapshot(bundle, target)["verified"]
            assert stage_snapshot(bundle, target)["phase"] == "VERIFIED"
            assert manifest["streams"]["children"]["count"] == 108
            assert manifest["streams"]["receipts"]["count"] == 322
            if PAIR_CONTROL is not None:
                stop, restore = PAIR_CONTROL
                activate = target.activate

                def lose_standby_at_activation(*args):
                    stop()
                    return activate(*args)

                monkeypatch.setattr(target, "activate", lose_standby_at_activation)
                try:
                    with pytest.raises(ContinuationStorageFailure):
                        host.activate(bundle, source, target)
                    assert source.path.is_dir()
                    assert bundle.has_phase("SOURCE_RETIRED", manifest)
                    assert not bundle.has_phase("TARGET_READY", manifest)
                finally:
                    monkeypatch.setattr(target, "activate", activate)
                    restore()
            assert host.activate(bundle, source, target)["phase"] == "COMPLETE"
        host.start(replace(SonderConfig(), child_storage=storage_config))
        assert host._repository.mutate(replay).result_bytes == original_result
        assert host._repository.get("child-0000").checkpoint.state == {"value": 0}
        add_terminal(host._repository, "new-pg-write")
        host.quiesce()
        reverse_source = PostgresChildMigrationStore(
            storage_config,
            PostgresPrivateBinding(
                Path(storage_config.binding_file), writable_roots=lambda: ()
            ),
        )
        reverse = SQLiteChildMigrationStore(host.path / "fresh-reverse.db")
        with ChildMigrationBundle(
            tmp_path / "inbound", writable_roots=lambda: ()
        ) as bundle:
            reverse_manifest = export_snapshot(
                reverse_source, bundle, target_identity=reverse.identity
            )
            stage_snapshot(bundle, reverse)
            assert verify_snapshot(bundle, reverse)["verified"]
            assert reverse_manifest["streams"]["children"]["count"] == 109
            assert reverse_manifest["streams"]["receipts"]["count"] == 324
            assert reverse_manifest["source"]["intents_high_water"] == 25002
            assert host.activate(bundle, reverse_source, reverse)["phase"] == "COMPLETE"
        owner = reverse_source._transport.run(
            lambda connection: connection.execute(
                "SELECT clean FROM sonder_child.owner WHERE id=1"
            ).fetchone()
        )
        assert owner == (False,)
        assert reverse_source.close()
        application = host.start(SonderConfig())
        assert host._repository.get("new-pg-write").recovery_required
        assert host._repository.get("child-0000").checkpoint.state == {"value": 0}
        add_terminal(host._repository, "new-sqlite-write")
        from sonder_runtime.application.context import local_owner_context

        service = application.delegation_service()._provider._local_service

        def bounded_resume(state, checkpoint, cancellation):
            assert state == {"value": 0}
            checkpoint({"value": 1}, "resumed-after-migration")
            return "bounded resumed result"

        result = service.resume(
            "child-0000",
            local_owner_context(correlation_id="migration-acceptance"),
            bounded_resume,
        ).result(3)
        assert result.output == "bounded resumed result"
        assert host._repository.get("child-0000").checkpoint.sequence == 1
    finally:
        host.close()
        if reverse_source is not None:
            assert reverse_source.close()
        assert target.close()
