"""Regression coverage for bootstrap composition findings (spill path, locking,
release race, recovery eviction, retirement reconciliation)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from tests import test_app_managed_authority as _authority_fixtures
from tests.test_app_recovery_registry import _registry

managed = _authority_fixtures.managed
control = _authority_fixtures.control


def test_spill_store_does_not_reuse_jobs_database_override(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.platform import paths

    jobs = tmp_path / "jobs.db"
    spill = tmp_path / "spill.db"
    monkeypatch.setattr(paths, "_configured_home", lambda: None)
    monkeypatch.setenv("SONDER_JOBS_DB", str(jobs))
    monkeypatch.setenv("SONDER_SPILL_DB", str(spill))
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(tmp_path / "sessions.db"))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        application.process_job_provider()
        assert spill.exists()
        assert jobs.exists()
        assert spill.resolve() != jobs.resolve()
    finally:
        bootstrap_app.reset_for_tests()


def test_concurrent_first_use_builds_one_job_registry(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app as bootstrap_app

    monkeypatch.setenv("SONDER_JOBS_DB", str(tmp_path / "jobs.db"))
    built = []
    real = bootstrap_app.SQLiteDurableJobRegistry

    def slow(*args, **kwargs):
        built.append(1)
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(bootstrap_app, "SQLiteDurableJobRegistry", slow)
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: application.job_registry(), range(4)))
        assert len({id(item) for item in results}) == 1
        assert len(built) == 1
    finally:
        bootstrap_app.reset_for_tests()


def test_cleanup_treats_selection_released_by_request_owner_as_released():
    from sonder_runtime.bootstrap.app_managed_work import (
        AppManagedWorkDispatcher,
        _Run,
    )

    class Authority:
        def release_retained(self, lease):
            pass

        def release_selection(self, selection):
            raise PermissionError("private app selection issuer required")

        def selection_issued(self, selection):
            return False

    dispatcher = AppManagedWorkDispatcher(
        Authority(),
        object(),
        lifetime_factory=lambda *a: None,
        authorize_dispatch=lambda *a: None,
        terminal_eligibility=lambda *a: None,
    )
    try:
        record = SimpleNamespace(prepared=SimpleNamespace(work_id="w"))
        entry = _Run(object(), object(), record, finished=True)
        assert dispatcher._slots.acquire(blocking=False)
        dispatcher._runs["w"] = entry
        assert dispatcher._cleanup(entry) is True
        assert "w" not in dispatcher._runs
        assert dispatcher._slots.acquire(blocking=False)
    finally:
        dispatcher.close()


def test_cleanup_keeps_slot_when_selection_is_still_issued():
    from sonder_runtime.bootstrap.app_managed_work import (
        AppManagedWorkDispatcher,
        _Run,
    )

    class Authority:
        def release_retained(self, lease):
            pass

        def release_selection(self, selection):
            raise PermissionError("app selection still has active references")

        def selection_issued(self, selection):
            return True

    dispatcher = AppManagedWorkDispatcher(
        Authority(),
        object(),
        lifetime_factory=lambda *a: None,
        authorize_dispatch=lambda *a: None,
        terminal_eligibility=lambda *a: None,
    )
    try:
        record = SimpleNamespace(prepared=SimpleNamespace(work_id="w"))
        entry = _Run(object(), object(), record, finished=True)
        dispatcher._runs["w"] = entry
        assert dispatcher._cleanup(entry) is False
        assert "w" in dispatcher._runs
    finally:
        dispatcher._runs.clear()
        dispatcher.close()


def _wait_idle(entry):
    deadline = time.monotonic() + 10
    while entry.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not entry.busy


def test_closed_recovery_entries_do_not_exhaust_attempt_limit(managed):
    registry, _ = _registry(managed)
    registry._limit = 2
    selection = managed[1]
    closed = {
        f"old{index}": SimpleNamespace(
            phase="closed", released=True, busy=False, selection=selection
        )
        for index in range(2)
    }
    registry._entries.update(closed)
    try:
        result = registry.prepare(
            selection,
            work_id="fresh",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
        entry = registry._entries[result["attempt_id"]]
        _wait_idle(entry)
        assert "old0" not in registry._entries
        assert "old1" in registry._entries
    finally:
        registry._entries.pop("old1", None)
        registry.close()


def test_crash_between_rename_and_mkdir_is_reconciled_on_retry(tmp_path, monkeypatch):
    from sonder_runtime.adapters.filesystem.child_migration_bundle import (
        ChildMigrationBundle,
    )
    from sonder_runtime.adapters.persistence.child_migration import (
        SQLiteChildMigrationStore,
    )
    from sonder_runtime.application.subagents.child_migration import (
        export_snapshot,
        stage_snapshot,
    )
    from sonder_runtime.bootstrap.child_migration_host import (
        DisposableChildMigrationHost,
    )
    from sonder_runtime.platform.config import SonderConfig
    from tests.test_child_migration_host import add_terminal

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
            source_path = source.path
            real_mkdir = Path.mkdir

            def crash(self, *args, **kwargs):
                if self == source_path:
                    raise OSError("fixture crash before mkdir")
                return real_mkdir(self, *args, **kwargs)

            monkeypatch.setattr(Path, "mkdir", crash)
            with pytest.raises(OSError):
                host.activate(bundle, source, target)
            monkeypatch.setattr(Path, "mkdir", real_mkdir)
            assert not source_path.exists()
            assert host.activate(bundle, source, target)["phase"] == "COMPLETE"
            assert source_path.is_dir()
    finally:
        host.close()
