"""Private state is owner-only on POSIX: home 0700, SQLite/JSONL stores 0600.

Finding #13: ``memory.db`` (password hashes, account sessions, conversations)
and ``goals.db``/``sessions.db``/``approvals.db`` were created ``0644`` in a
``0755`` home, while only a few stores chmod'ed themselves. These tests pin
the shared choke points: the state-home constructor, the SQLite connection
factory every packaged store uses, the legacy goal store, and the tool-audit
JSONL writer.
"""
from __future__ import annotations

import os
import stat

import pytest

from sonder_runtime.platform import paths, private_files

pytestmark = pytest.mark.skipif(
    not private_files.supported(), reason="POSIX permission bits only"
)


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture()
def loose_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_new_state_home_is_owner_only(tmp_path, monkeypatch, loose_umask):
    home = tmp_path / "state" / "sonder"
    monkeypatch.setenv("SONDER_HOME", str(home))
    paths.reset_home()
    assert paths.ensure_home() == home
    assert _mode(home) == 0o700


def test_existing_owned_state_home_is_tightened(tmp_path, monkeypatch, loose_umask):
    home = tmp_path / "sonder-home"
    home.mkdir(mode=0o755)
    os.chmod(home, 0o755)
    monkeypatch.setenv("SONDER_HOME", str(home))
    paths.reset_home()
    paths.state_path("goals.db")
    assert _mode(home) == 0o700


def test_configured_home_is_owner_only(tmp_path, loose_umask):
    home = tmp_path / "configured"
    paths.configure_home(home)
    try:
        paths.state_path("x.db")
    finally:
        paths.reset_home()
    assert _mode(home) == 0o700


def test_shared_sticky_directory_is_never_tightened(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    assert private_files.restrict_to_owner(shared) is False
    assert _mode(shared) == 0o1777


def test_symlink_is_never_followed(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    os.chmod(target, 0o644)
    link = tmp_path / "link"
    link.symlink_to(target)
    assert private_files.restrict_to_owner(link) is False
    assert _mode(target) == 0o644


def test_restrict_never_widens(tmp_path):
    target = tmp_path / "f"
    target.write_text("x")
    os.chmod(target, 0o400)
    private_files.restrict_to_owner(target)
    assert _mode(target) == 0o400


def test_memory_store_database_and_sidecars_are_owner_only(tmp_path, loose_umask):
    from sonder_runtime.adapters import memory_store

    db = tmp_path / "memory.db"
    conn = memory_store.connect(str(db))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (x)")
        conn.execute("INSERT INTO probe VALUES (1)")
        conn.commit()
        assert _mode(db) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = str(db) + suffix
            if os.path.exists(sidecar):
                assert _mode(sidecar) == 0o600, suffix
    finally:
        conn.close()


def test_existing_world_readable_store_is_tightened_on_open(tmp_path, loose_umask):
    from sonder_runtime.adapters.persistence.owned_sqlite import connect

    db = tmp_path / "sessions.db"
    for path in (db, tmp_path / "sessions.db-wal", tmp_path / "sessions.db-shm"):
        path.write_bytes(b"")
        os.chmod(path, 0o644)
    connect(str(db)).close()
    assert _mode(db) == 0o600
    assert _mode(str(db) + "-wal") == 0o600
    assert _mode(str(db) + "-shm") == 0o600


def test_read_only_uri_never_creates_a_store(tmp_path):
    import sqlite3

    from sonder_runtime.adapters.persistence.owned_sqlite import connect

    db = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        connect("%s?mode=ro" % db.as_uri(), uri=True)
    assert not db.exists()


def test_session_repository_store_is_owner_only(tmp_path, loose_umask):
    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )

    db = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(db)
    repo.append("s1", "probe", {"x": 1})
    repo.close()
    assert _mode(db) == 0o600


def test_goal_store_database_is_owner_only(tmp_path, monkeypatch, loose_umask):
    import goal_store

    db = tmp_path / "goals.db"
    monkeypatch.setenv("SONDER_GOAL_DB", str(db))
    goal_store._connection()
    assert _mode(db) == 0o600


def test_prepare_private_file_creates_and_tightens(tmp_path, loose_umask):
    path = tmp_path / "audit" / "tool-receipts.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    private_files.prepare_private_file(path)
    assert _mode(path) == 0o600
    fresh = tmp_path / "audit" / "fresh.jsonl"
    private_files.prepare_private_file(fresh)
    assert _mode(fresh) == 0o600
