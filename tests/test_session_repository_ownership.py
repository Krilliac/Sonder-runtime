import sqlite3
from threading import Event, Thread

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository


def test_connection_context_closes_exact_handle(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    with repository._connect() as connection:
        connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert repository.close(timeout=0)


def test_close_retains_live_owner_thread_and_stops_new_admission(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    opened, release = Event(), Event()
    failures = []
    def worker():
        try:
            with repository._connect() as connection:
                opened.set()
                assert release.wait(5)
                assert connection.execute("SELECT 1").fetchone() == (1,)
        except BaseException as error:
            failures.append(error)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert opened.wait(5)
        assert repository.close(timeout=0) is False
        with pytest.raises(RuntimeError, match="closed"):
            repository.read_range("session")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not failures
    assert repository.close(timeout=0) is True


def test_failed_connection_setup_releases_only_nonexistent_handle(tmp_path, monkeypatch):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fixture")
    monkeypatch.setattr(sqlite3, "connect", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        repository.read_range("session")
    assert repository.close(timeout=0)
