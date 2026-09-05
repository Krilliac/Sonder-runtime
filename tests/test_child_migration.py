from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.application.ports.continuation_records import (
    DurableChildSession,
    ChildSessionLineage,
)
from sonder_runtime.application.ports.subagents import (
    SubagentRequest,
    SubagentBudget,
    SubagentStatus,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint


def seed(path, count=5, repository=None):
    repository = repository or SQLiteDurableContinuationRepository(path)
    from tests.test_child_migration_host import add_root

    add_root(repository)
    for index in range(count):
        child = f"child-{index:04}"
        record = DurableChildSession(
            SubagentRequest(
                "parent", "Unicode Ω fixture", SubagentBudget(max_steps=3), child
            ),
            ChildSessionLineage("parent"),
        )
        repository.create(record)
        repository.save_checkpoint(
            ContinuableCheckpoint(child, 0, {"value": index}), expected_sequence=-1
        )
        repository.update(child, status=SubagentStatus.FAILED, recovery_required=True)
    return repository


def test_sqlite_bundle_preserves_sparse_order_and_exact_history(tmp_path):
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

    source_path, target_path = tmp_path / "source.db", tmp_path / "target.db"
    original = seed(source_path, 107)
    import sqlite3

    with sqlite3.connect(source_path) as connection:
        connection.execute("UPDATE durable_child_session SET rowid=rowid+1000")
        connection.execute("UPDATE continuation_intent SET position=position+5000")
        connection.execute(
            "UPDATE sqlite_sequence SET seq=25000 WHERE name='continuation_intent'"
        )
    source = SQLiteChildMigrationStore(source_path)
    target = SQLiteChildMigrationStore(target_path)
    with ChildMigrationBundle(
        tmp_path / "private-bundle", writable_roots=lambda: ()
    ) as bundle:
        manifest = export_snapshot(source, bundle, target_identity=target.identity)
        stage_snapshot(bundle, target)
        assert verify_snapshot(bundle, target)["verified"]
        assert manifest["streams"]["children"]["count"] == 108
        assert manifest["streams"]["intents"]["count"] == 322
        assert manifest["streams"]["receipts"]["count"] == 322
        assert manifest["source"]["intents_high_water"] == 25000
        assert manifest["streams"]["children"]["last_position"] == 1108
        assert stage_snapshot(bundle, target)["phase"] == "VERIFIED"
        # Staging must never accidentally enable the normal application writer.
        with pytest.raises(Exception, match="migration"):
            SQLiteDurableContinuationRepository(target_path)


def test_unknown_pending_history_refuses_stage_without_truncation(tmp_path):
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        stage_snapshot,
        MigrationRefused,
    )
    from sonder_runtime.application.ports.continuation_mutations import prepare_call
    import sqlite3

    path = tmp_path / "source.db"
    repository = seed(path, 1)
    pending = prepare_call("request_cancel", "child-0000", reason="pending fixture")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO continuation_intent(operation_id,child_id,kind,digest,payload) VALUES(?,?,?,?,?)",
            (
                pending.operation_id,
                pending.child_id,
                pending.kind,
                pending.request_sha256,
                pending.payload,
            ),
        )
    target = SQLiteChildMigrationStore(tmp_path / "target.db")
    with ChildMigrationBundle(
        tmp_path / "private-bundle", writable_roots=lambda: ()
    ) as bundle:
        manifest = export_snapshot(
            SQLiteChildMigrationStore(path), bundle, target_identity=target.identity
        )
        assert manifest["unresolved"] == 1
        with pytest.raises(MigrationRefused, match="unresolved"):
            stage_snapshot(bundle, target)
        assert not (tmp_path / "target.db").exists()


def test_export_reuses_sealed_backup_and_retained_operation_after_interruption(
    tmp_path, monkeypatch
):
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import export_snapshot

    source = SQLiteChildMigrationStore(tmp_path / "source.db")
    seed(source.path, 3)
    target = SQLiteChildMigrationStore(tmp_path / "target.db")
    with ChildMigrationBundle(tmp_path / "bundle", writable_roots=lambda: ()) as bundle:
        original = bundle.write_stream

        def interrupt(kind, records, **kwargs):
            if kind == "intents":
                raise RuntimeError("fixture interruption")
            return original(kind, records, **kwargs)

        monkeypatch.setattr(bundle, "write_stream", interrupt)
        with pytest.raises(RuntimeError):
            export_snapshot(source, bundle, target_identity=target.identity)
        plan = bundle.begin(source.identity, target.identity)
        monkeypatch.setattr(bundle, "write_stream", original)
        manifest = export_snapshot(source, bundle, target_identity=target.identity)
        assert manifest["migration_id"] == plan["migration_id"]
        assert manifest["streams"]["children"]["count"] == 4
        assert manifest["source_backup_sha256"]


def test_history_capacity_and_manifest_types_are_strict(tmp_path):
    from copy import deepcopy
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        validate_manifest,
        digest,
        MigrationRefused,
    )

    source = SQLiteChildMigrationStore(tmp_path / "source.db")
    seed(source.path, 1)
    with ChildMigrationBundle(tmp_path / "bundle", writable_roots=lambda: ()) as bundle:
        manifest = export_snapshot(source, bundle, target_identity="a" * 64)
        malformed = deepcopy(manifest)
        malformed["source"]["active"] = False
        with pytest.raises(MigrationRefused):
            validate_manifest(malformed)
        oversized = deepcopy(manifest)
        oversized["streams"]["receipts"]["binary_bytes"] = 64 * 1024 * 1024
        oversized["aggregate_sha256"] = digest(oversized["streams"])
        with pytest.raises(MigrationRefused, match="capacity"):
            validate_manifest(oversized)


def test_missing_and_changed_workspace_exclusion_fails_before_read(tmp_path):
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import MigrationRefused

    roots = []
    with ChildMigrationBundle(
        tmp_path / "bundle", writable_roots=lambda: tuple(roots)
    ) as bundle:
        roots.append(tmp_path)
        with pytest.raises(MigrationRefused, match="overlaps"):
            bundle.has_manifest()


def test_copy_page_survives_process_exit_and_resumes_exactly_once(tmp_path):
    import subprocess
    import sys
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

    source, target = SQLiteChildMigrationStore(
        tmp_path / "source.db"
    ), SQLiteChildMigrationStore(tmp_path / "target.db")
    seed(source.path, 107)
    bundle_path = tmp_path / "bundle"
    with ChildMigrationBundle(bundle_path, writable_roots=lambda: ()) as bundle:
        manifest = export_snapshot(source, bundle, target_identity=target.identity)
    script = """
import sys, os
from sonder_runtime.adapters.persistence.child_migration import SQLiteChildMigrationStore
from sonder_runtime.adapters.filesystem.child_migration_bundle import ChildMigrationBundle
with ChildMigrationBundle(sys.argv[1],writable_roots=lambda: ()) as bundle:
    target = SQLiteChildMigrationStore(sys.argv[2])
    manifest = bundle.manifest()
    target.prepare(manifest)
    target.copy_page(manifest,'children',0,next(bundle.pages('children')))
os._exit(0)
"""
    run = subprocess.run(
        [sys.executable, "-c", script, str(bundle_path), str(target.path)],
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    with ChildMigrationBundle(bundle_path, writable_roots=lambda: ()) as bundle:
        stage_snapshot(bundle, SQLiteChildMigrationStore(target.path))
        assert verify_snapshot(bundle, target)["verified"]
        assert bundle.manifest()["migration_id"] == manifest["migration_id"]


def test_missing_durable_parent_refuses_export(tmp_path):
    import sqlite3
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        MigrationRefused,
    )

    source = SQLiteChildMigrationStore(tmp_path / "source.db")
    seed(source.path, 1)
    with sqlite3.connect(source.path) as connection:
        connection.execute("DELETE FROM durable_child_session WHERE child_id='parent'")
    with ChildMigrationBundle(tmp_path / "bundle", writable_roots=lambda: ()) as bundle:
        with pytest.raises(MigrationRefused, match="missing durable parent"):
            export_snapshot(source, bundle, target_identity="a" * 64)
        assert not bundle.has_manifest()
