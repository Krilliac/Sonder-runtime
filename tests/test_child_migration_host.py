import sqlite3

import pytest

from sonder_runtime.bootstrap.child_migration_host import DisposableChildMigrationHost
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
    MigrationUnsupported,
)
from sonder_runtime.platform.config import SonderConfig
from sonder_runtime.application.ports.continuation_records import (
    DurableChildSession,
    ChildSessionLineage,
)
from sonder_runtime.application.ports.subagents import (
    SubagentRequest,
    SubagentBudget,
    SubagentStatus,
)


def add_root(repository):
    if repository.get("parent") is None:
        repository.create(
            DurableChildSession(
                SubagentRequest(
                    "parent",
                    "local provider root",
                    SubagentBudget(max_steps=10000),
                    "parent",
                    (("provider_root", "true"),),
                ),
                ChildSessionLineage("parent"),
            )
        )


def add_terminal(repository, identity):
    add_root(repository)
    row = DurableChildSession(
        SubagentRequest("parent", "fixture", SubagentBudget(max_steps=3), identity),
        ChildSessionLineage("parent"),
    )
    repository.create(row)
    repository.update(identity, status=SubagentStatus.FAILED, recovery_required=True)


def test_owned_application_cleanup_tombstone_and_exact_activation_retry(tmp_path):
    host = DisposableChildMigrationHost(tmp_path / "owned", writable_roots=lambda: ())
    try:
        application = host.start(SonderConfig())
        add_terminal(host._repository, "original")
        with host.tracked_connection():
            with pytest.raises(MigrationRefused, match="connections remain"):
                host.quiesce()
        host.quiesce()
        source = host.selected_store
        target = SQLiteChildMigrationStore(host.path / "next.sqlite")
        with ChildMigrationBundle(
            tmp_path / "bundle", writable_roots=lambda: ()
        ) as bundle:
            manifest = export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            with pytest.raises(MigrationUnsupported):
                target.activate(manifest, True)
            result = host.activate(bundle, source, target)
            assert result["phase"] == "COMPLETE"
            assert host.activate(bundle, source, target) == result
            assert source.path.is_dir()
            with pytest.raises(sqlite3.OperationalError):
                sqlite3.connect(source.path)
        host.start(SonderConfig())
        assert host._repository.get("original").recovery_required
        add_terminal(host._repository, "after-activation")
    finally:
        host.close()


def test_host_never_adopts_existing_namespace(tmp_path):
    with pytest.raises(MigrationUnsupported, match="cannot adopt"):
        DisposableChildMigrationHost(tmp_path, writable_roots=lambda: ())


def test_stale_snapshot_cannot_retire_current_source(tmp_path):
    host = DisposableChildMigrationHost(tmp_path / "owned", writable_roots=lambda: ())
    try:
        host.start(SonderConfig())
        add_terminal(host._repository, "original")
        source = host.selected_store
        target = SQLiteChildMigrationStore(host.path / "next.sqlite")
        with ChildMigrationBundle(
            tmp_path / "bundle", writable_roots=lambda: ()
        ) as bundle:
            export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            add_terminal(host._repository, "after-export")
            with pytest.raises(MigrationRefused, match="source changed"):
                host.activate(bundle, source, target)
            assert source.path.is_file()
    finally:
        host.close()


def test_lost_target_response_keeps_source_retired_and_same_id_resumes(
    tmp_path, monkeypatch
):
    host = DisposableChildMigrationHost(tmp_path / "owned", writable_roots=lambda: ())
    try:
        host.start(SonderConfig())
        add_terminal(host._repository, "original")
        host.quiesce()
        source = host.selected_store
        target = SQLiteChildMigrationStore(host.path / "next.sqlite")
        with ChildMigrationBundle(
            tmp_path / "bundle", writable_roots=lambda: ()
        ) as bundle:
            manifest = export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            original = target.activate

            def lost_response(*args):
                original(*args)
                raise TimeoutError("fixture response lost after commit")

            monkeypatch.setattr(target, "activate", lost_response)
            with pytest.raises(TimeoutError):
                host.activate(bundle, source, target)
            assert source.path.is_dir()
            assert bundle.has_phase("SOURCE_RETIRED", manifest)
            assert not bundle.has_phase("TARGET_READY", manifest)
            monkeypatch.setattr(target, "activate", original)
            assert host.activate(bundle, source, target)["phase"] == "COMPLETE"
    finally:
        host.close()


def test_same_logical_rows_in_replaced_source_file_do_not_prove_identity(tmp_path):
    import shutil
    import os

    host = DisposableChildMigrationHost(tmp_path / "owned", writable_roots=lambda: ())
    try:
        host.start(SonderConfig())
        add_terminal(host._repository, "original")
        host.quiesce()
        source = host.selected_store
        target = SQLiteChildMigrationStore(host.path / "next.sqlite")
        with ChildMigrationBundle(
            tmp_path / "bundle", writable_roots=lambda: ()
        ) as bundle:
            export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            replacement = host.path / "replacement.sqlite"
            shutil.copyfile(source.path, replacement)
            os.replace(replacement, source.path)
            with pytest.raises(MigrationRefused, match="file or sidecar changed"):
                host.activate(bundle, source, target)
            assert source.path.is_file()
    finally:
        host.close()
